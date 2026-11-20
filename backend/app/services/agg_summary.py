"""汇总构建服务：基于事实表构建多级汇总表，上卷维度、聚合度量并维护增量刷新。

增强能力：
- 消费 fact.published 和 measure.ready 通道，获取事实和度量信息
- 消费 agg.suggested 通道自动创建建议汇总
- 汇总字段定义校验：按同一个源事实版本核对维度与粒度，
  维度不能上卷时报 DIM_MISSING，保留原始输入、指出失效维度并阻止定义生效
- 源事实更正后重新核对已登记的汇总定义，失效字段不再允许出现在分组键里
- 增量标记管理：基于已刷新记录的metadata确定增量边界
- 构建失败自动记录追踪
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.errors import BusinessValidationError
from app.services.base import BaseModuleService
from app.store import store

# 校验通过/失效标记，写入汇总条目的 _validation 元数据
VALIDATION_OK = "VALID"
VALIDATION_DIM_MISSING = "DIM_MISSING"
VALIDATION_GRAIN_MISMATCH = "GRANULARITY_INVALID"
VALIDATION_FACT_MISSING = "FACT_NOT_FOUND"
VALIDATION_LEGACY = "LEGACY"


def split_fields(raw: str) -> list[str]:
    """汇总字段统一用「.」分隔；同时容忍种子数据里的「,」写法。"""
    return [part.strip() for part in str(raw or "").replace(",", ".").split(".") if part.strip()]


def join_fields(fields: list[str]) -> str:
    return ".".join(fields)


def resolve_fact_version(
    fact_name: str, target_store: Any | None = None
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """返回 (版本信息, 事实条目)。

    单次校验只按同一个源事实版本核对维度和粒度：
    优先取版本登记处的已发布版本；没有登记（如草稿事实）时回退到当前事实行，
    并把该行内容视作 v1 快照，避免在校验过程中混用不同版本的字段。
    """
    repo = target_store or store
    fact_name = str(fact_name or "").strip()
    if not fact_name:
        return None, None

    fact_row = next(
        (row for row in repo.rows("fact_design") if row.get("事实名称") == fact_name),
        None,
    )
    version = repo.get_fact_version(fact_name)
    if version is None and fact_row is not None:
        version = {
            "fact_name": fact_name,
            "version": 1,
            "related_dims": str(fact_row.get("关联维度", "")),
            "grain": str(fact_row.get("粒度声明", "")),
            "partition_field": str(fact_row.get("分区字段", "")),
            "published_at": datetime.now(timezone.utc).isoformat(),
            "entry_id": fact_row.get("id"),
        }
    return version, fact_row


def validate_summary_definition(
    values: dict[str, Any], target_store: Any | None = None
) -> dict[str, Any]:
    """按同一个源事实版本核对汇总字段定义。

    校验不通过时抛 BusinessValidationError，携带错误码、不能上卷的维度以及
    原始输入；不会改写 values，更不会让定义落库。
    """
    repo = target_store or store
    fact_name = str(values.get("源事实表", "")).strip()
    agg_dims_raw = str(values.get("汇总维度", "")).strip()
    agg_granularity = str(values.get("汇总粒度", "")).strip()
    submitted = {
        "源事实表": fact_name,
        "汇总维度": agg_dims_raw,
        "汇总粒度": agg_granularity,
    }

    if not fact_name:
        raise BusinessValidationError(
            VALIDATION_FACT_MISSING,
            "未指定源事实表，无法核对汇总字段定义",
            {"missing_fact": True},
            payload=submitted,
        )

    version, _fact_row = resolve_fact_version(fact_name, repo)
    if version is None:
        raise BusinessValidationError(
            VALIDATION_FACT_MISSING,
            f"源事实表「{fact_name}」不存在，无法按其维度登记汇总定义",
            {"source_fact": fact_name},
            payload=submitted,
        )

    if agg_granularity and agg_granularity not in AggSummaryService.VALID_GRANULARITIES:
        raise BusinessValidationError(
            VALIDATION_GRAIN_MISMATCH,
            f"汇总粒度「{agg_granularity}」不受支持，可选："
            f"{'、'.join(sorted(AggSummaryService.VALID_GRANULARITIES))}",
            {
                "source_fact": fact_name,
                "fact_version": version["version"],
                "submitted_granularity": agg_granularity,
                "allowed": sorted(AggSummaryService.VALID_GRANULARITIES),
            },
            payload=submitted,
        )

    requested_dims = split_fields(agg_dims_raw)
    fact_dims = split_fields(version["related_dims"])
    fact_dim_set = set(fact_dims)
    missing_dims = [dim for dim in requested_dims if dim not in fact_dim_set]

    if missing_dims:
        raise BusinessValidationError(
            VALIDATION_DIM_MISSING,
            "维度 "
            + "、".join(missing_dims)
            + f" 不属于源事实「{fact_name}」v{version['version']} 的关联维度，不能上卷；"
            "请移除这些分组字段后再提交，原始字段已保留",
            {
                "code": VALIDATION_DIM_MISSING,
                "source_fact": fact_name,
                "fact_version": version["version"],
                "fact_grain": version["grain"],
                "fact_dims": fact_dims,
                "requested_dims": requested_dims,
                # 不能上卷的维度：前端据此逐条标红，但不会自动从输入里删掉
                "missing_dims": missing_dims,
                "rollup_dims": [dim for dim in requested_dims if dim in fact_dim_set],
            },
            payload=submitted,
        )

    return {
        "source_fact": fact_name,
        "fact_version": version["version"],
        "fact_grain": version["grain"],
        "fact_dims": fact_dims,
        "requested_dims": requested_dims,
        "missing_dims": [],
        "partition_field": version.get("partition_field", ""),
    }


def stamp_definition(entry: dict[str, Any], validation: dict[str, Any], *, legacy: bool = False) -> None:
    """把校验过的汇总字段定义与源事实版本盖戳到条目上，作为星型查询选源的唯一依据。"""
    now = datetime.now(timezone.utc).isoformat()
    entry["_definition"] = {
        "source_fact": validation["source_fact"],
        "fact_version": validation["fact_version"],
        "group_dims": list(validation["requested_dims"]),
        "granularity": str(entry.get("汇总粒度", "")),
        "checked_at": now,
        "legacy": legacy,
    }
    entry["_validation"] = {
        "code": VALIDATION_LEGACY if legacy else VALIDATION_OK,
        "valid": True,
        "missing_dims": [],
        "source_fact": validation["source_fact"],
        "fact_version": validation["fact_version"],
        "fact_grain": validation.get("fact_grain", ""),
        "checked_at": now,
    }
    # 适用日期：定义校验通过之日起生效；失效时由 revalidate 收口
    entry["_applicable_from"] = now
    entry["_applicable_to"] = None


def stamp_legacy_seed_summaries(target_store: Any | None = None) -> None:
    """为种子汇总补盖定义戳记：沿用其原始分组键，不做静默改写。"""
    repo = target_store or store
    for row in repo.rows("agg_summary"):
        if row.get("_definition"):
            continue
        fact_name = str(row.get("源事实表", "")).strip()
        version, _ = resolve_fact_version(fact_name, repo)
        now = datetime.now(timezone.utc).isoformat()
        fact_version = version["version"] if version else 1
        fact_grain = version["grain"] if version else ""
        requested = split_fields(row.get("汇总维度", ""))
        fact_dims = split_fields(version["related_dims"]) if version else []
        missing = [dim for dim in requested if dim not in set(fact_dims)]
        row["_definition"] = {
            "source_fact": fact_name,
            "fact_version": fact_version,
            "group_dims": requested,
            "granularity": str(row.get("汇总粒度", "")),
            "checked_at": now,
            "legacy": True,
        }
        row["_validation"] = {
            "code": VALIDATION_LEGACY,
            "valid": not missing,
            "missing_dims": missing,
            "source_fact": fact_name,
            "fact_version": fact_version,
            "fact_grain": fact_grain,
            "checked_at": now,
        }
        row["_applicable_from"] = now
        row["_applicable_to"] = None


def revalidate_summaries_for_fact(
    fact_name: str, new_version: dict[str, Any], target_store: Any | None = None
) -> list[dict[str, Any]]:
    """源事实更正发布后，按新版本重新核对引用该事实的全部汇总定义。

    - 仍可上卷的定义：刷新版本戳记，继续可用
    - 分组键里出现失效字段的定义：标记 DIM_MISSING，收口适用日期，
      已失效字段不会进入任何新汇总的分组键（登记入口同样被 validate 拦截）
    """
    repo = target_store or store
    affected: list[dict[str, Any]] = []
    fact_dim_set = set(split_fields(new_version.get("related_dims", "")))
    now = datetime.now(timezone.utc).isoformat()

    for entry in repo.rows("agg_summary"):
        definition = entry.get("_definition")
        if not definition or definition.get("source_fact") != fact_name:
            continue

        group_dims = list(definition.get("group_dims", []))
        missing_dims = [dim for dim in group_dims if dim not in fact_dim_set]
        previous_validation = entry.get("_validation", {})

        if missing_dims:
            entry["_validation"] = {
                "code": VALIDATION_DIM_MISSING,
                "valid": False,
                "missing_dims": missing_dims,
                "source_fact": fact_name,
                "fact_version": new_version["version"],
                "fact_grain": new_version.get("grain", ""),
                "checked_at": now,
                "previous_fact_version": previous_validation.get("fact_version"),
            }
            # 适用日期收口到更正时刻，历史区间仍可追溯
            if not entry.get("_applicable_to"):
                entry["_applicable_to"] = now
            entry["abnormal"] = True
            repo.channel.publish("agg.invalidated", {
                "agg_name": entry.get("汇总名称", ""),
                "entry_id": entry.get("id"),
                "source_fact": fact_name,
                "fact_version": new_version["version"],
                "missing_dims": missing_dims,
                "invalidated_at": now,
            })
        else:
            entry["_validation"] = {
                "code": VALIDATION_OK,
                "valid": True,
                "missing_dims": [],
                "source_fact": fact_name,
                "fact_version": new_version["version"],
                "fact_grain": new_version.get("grain", ""),
                "checked_at": now,
            }
            definition["fact_version"] = new_version["version"]
            definition["checked_at"] = now

        affected.append({
            "entry_id": entry.get("id"),
            "agg_name": entry.get("汇总名称", ""),
            "valid": not missing_dims,
            "missing_dims": missing_dims,
            "fact_version": new_version["version"],
        })
    return affected


class AggSummaryService(BaseModuleService):
    MODULE = "agg_summary"
    ENTITY = "汇总表"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["汇总名称", "源事实表", "汇总维度", "汇总度量", "汇总粒度", "刷新周期", "增量标记"]
    REQUIRED_FIELDS = ["汇总名称", "源事实表", "汇总维度"]
    STATUS_ORDER = ["草稿", "已构建", "已刷新", "构建失败"]
    ACTION_RULES = {"执行构建": "草稿", "刷新汇总": "已刷新"}
    NEGATIVE_ACTIONS = []

    VALID_GRANULARITIES = {"每小时", "每日", "每周", "每月", "每季度", "每年"}

    def __init__(self) -> None:
        # 服务实例化发生在全部模块导入完成之后，此处补盖种子戳记无循环依赖
        stamp_legacy_seed_summaries()

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建汇总表：先做字段定义校验，校验不过直接报错，绝不落库、不改写输入。"""
        fact_name = str(values.get("源事实表", "")).strip()
        agg_granularity = str(values.get("汇总粒度", "")).strip()

        # 粒度为空时给默认值；显式传了非法粒度则交给校验报错（不清空、不静默替换）
        if not agg_granularity:
            agg_granularity = "每日"
            values["汇总粒度"] = agg_granularity

        # 关键：先按同一个源事实版本核对维度和粒度，失败抛 BusinessValidationError
        validation = validate_summary_definition(values)

        # 消费事实发布通道（仅用于补元数据，不再作为维度判定依据）
        fact_msgs = store.channel.consume("fact.published", "agg_summary")
        for msg in fact_msgs:
            if msg["payload"].get("fact_name") == fact_name:
                values["_source_granularity"] = msg["payload"].get("granularity", "")

        # 消费度量确认通道，获取可用度量列表
        available_measures: list[str] = []
        measure_msgs = store.channel.consume("measure.ready", "agg_summary")
        for m in measure_msgs:
            if m["payload"].get("fact_name") == fact_name:
                available_measures.append(m["payload"].get("measure_name", ""))

        if available_measures:
            # 汇总默认使用全部可用度量
            values["汇总度量"] = ".".join(available_measures) if not values.get("汇总度量") else values["汇总度量"]

        # 设置刷新周期默认值
        if not values.get("刷新周期"):
            granularity_map = {"每小时": "每小时 第10分钟", "每日": "T+1 03:00", "每周": "周一 06:00",
                               "每月": "月初T+3 04:00"}
            values["刷新周期"] = granularity_map.get(agg_granularity, "T+1 03:00")

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 盖戳：校验过的字段定义 + 源事实版本，星型查询只认这份定义
        stamp_definition(entry, validation)

        store.channel.publish("agg.defined", {
            "agg_name": entry["汇总名称"],
            "source_fact": fact_name,
            "agg_dims": entry.get("汇总维度", ""),
            "agg_measures": entry.get("汇总度量", ""),
            "agg_granularity": agg_granularity,
            "fact_version": validation["fact_version"],
            "refresh_cycle": entry.get("刷新周期", ""),
            "incremental": entry.get("增量标记") == "是",
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "agg_summary", "summary_table",
            f"agg_{entry['id']}",
            {
                "agg_name": entry["汇总名称"],
                "source_fact": fact_name,
                "granularity": agg_granularity,
                "fact_version": validation["fact_version"],
            },
            source_modules=["fact_design", "measure_config", "agg_summary"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        fact_name = entry.get("源事实表", "")

        if action == "执行构建":
            # 构建前再确认定义仍有效：失效定义不允许产出汇总数据
            validation_meta = entry.get("_validation") or {}
            if validation_meta.get("code") == VALIDATION_DIM_MISSING:
                entry["status"] = "构建失败"
                entry["abnormal"] = True
                return None, (
                    "汇总字段定义已失效：维度 "
                    + "、".join(validation_meta.get("missing_dims", []))
                    + " 已不在源事实分组键中，请重新登记汇总定义"
                )
            # 模拟汇总构建过程
            build_result = _simulate_agg_build(entry)
            if build_result["success"]:
                entry["status"] = "已构建"
                entry["pending"] = True
                store.channel.publish("agg.built", {
                    "agg_name": entry["汇总名称"],
                    "source_fact": fact_name,
                    "row_count": build_result.get("output_rows", 0),
                    "build_time": datetime.now(timezone.utc).isoformat(),
                    "incremental": entry.get("增量标记") == "是",
                    "entry_id": entry_id,
                })
            else:
                entry["status"] = "构建失败"
                entry["pending"] = True
                entry["abnormal"] = True
                store.channel.publish("agg.build_failed", {
                    "agg_name": entry["汇总名称"],
                    "error": build_result.get("error", ""),
                    "entry_id": entry_id,
                })
                # 记录失败到恢复管理器
                store.recovery.log_retry(
                    "agg_summary", f"build_{entry_id}", 1,
                    build_result.get("error", "未知错误"), False
                )

        elif action == "刷新汇总":
            # 增量刷新
            refresh_result = _simulate_agg_refresh(entry)
            if refresh_result["success"]:
                entry["status"] = "已刷新"
                entry["pending"] = False
                # 刷新成功后适用日期延续到当前时刻
                entry["_applicable_to"] = None
                store.channel.publish("agg.refreshed", {
                    "agg_name": entry["汇总名称"],
                    "new_rows": refresh_result.get("new_rows", 0),
                    "refresh_time": datetime.now(timezone.utc).isoformat(),
                    "entry_id": entry_id,
                })
            else:
                entry["pending"] = True
                entry["abnormal"] = True

        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        # 汇总粒度分布和构建状态统计
        granularity_dist: dict[str, int] = {}
        build_stats = {"success": 0, "failed": 0, "pending": 0}
        invalid_defs = 0
        for row in store.rows(self.MODULE):
            g = row.get("汇总粒度", "未指定")
            granularity_dist[g] = granularity_dist.get(g, 0) + 1
            status = row.get("status", "")
            if status == "已刷新":
                build_stats["success"] += 1
            elif status == "构建失败":
                build_stats["failed"] += 1
            else:
                build_stats["pending"] += 1
            if (row.get("_validation") or {}).get("code") == VALIDATION_DIM_MISSING:
                invalid_defs += 1
        base["granularity_distribution"] = granularity_dist
        base["build_stats"] = build_stats
        base["invalid_definitions"] = invalid_defs
        base["consumed_facts"] = len(store.channel.consume("fact.published", "agg_summary"))
        base["consumed_measures"] = len(store.channel.consume("measure.ready", "agg_summary"))
        return base


def _simulate_agg_build(entry: dict[str, Any]) -> dict[str, Any]:
    """模拟汇总构建过程。"""
    agg_dims = str(entry.get("汇总维度", "")).strip()
    agg_measures = str(entry.get("汇总度量", "")).strip()

    if not agg_dims or not agg_measures:
        return {"success": False, "error": "汇总维度或度量未配置"}

    dim_count = len(split_fields(agg_dims))
    measure_count = len(split_fields(agg_measures))
    estimated_rows = 100 * dim_count * measure_count
    cp_id = store.recovery.create_checkpoint("agg_summary", f"build_{entry.get('id')}", {
        "agg_name": entry.get("汇总名称"),
        "phase": "build_started",
        "dims_count": dim_count,
        "measures_count": measure_count,
    })
    store.recovery.mark_completed(cp_id)
    return {"success": True, "output_rows": estimated_rows, "checkpoint": cp_id}


def _simulate_agg_refresh(entry: dict[str, Any]) -> dict[str, Any]:
    """模拟汇总增量刷新过程。"""
    return {"success": True, "new_rows": 50, "refreshed_at": datetime.now(timezone.utc).isoformat()}
