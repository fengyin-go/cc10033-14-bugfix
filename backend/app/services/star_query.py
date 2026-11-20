"""星型查询服务：定义基于星型模型的维度查询，包括事实与维度的关联和度量聚合。

增强能力：
- 消费 fact.published 和 dim.published 通道校验查询组件
- 自动校验关联维度和度量是否可用
- 星型查询SQL生成（SELECT/FROM/JOIN/WHERE/GROUP BY）
- 查询结果限制与分页建议
- 汇总路由：按校验过的汇总字段定义决定能否使用现成汇总数据，
  选不到与请求粒度相符的汇总时回退到基于原事实的查询来源，
  细粒度查询绝不允许错用粗粒度汇总
"""
from __future__ import annotations

from typing import Any

from app.services.agg_summary import (
    VALIDATION_DIM_MISSING,
    VALIDATION_LEGACY,
    split_fields,
)
from app.services.base import BaseModuleService
from app.store import store

# 时间粒度从细到粗排序，用于防止用粗粒度汇总回答细粒度请求
TIME_GRAIN_ORDER = ["每小时", "每日", "每周", "每月", "每季度", "每年"]
TIME_DIM_HINTS = ("日期", "月份", "周", "时间", "date", "month", "week", "hour", "年", "季")


def _grain_rank(granularity: str) -> int | None:
    granularity = str(granularity or "").strip()
    return TIME_GRAIN_ORDER.index(granularity) if granularity in TIME_GRAIN_ORDER else None


def _requested_group_dims(entry: dict[str, Any]) -> list[str]:
    """查询请求的分组键：优先取分组维度，缺省回退到关联维度。"""
    group_raw = str(entry.get("分组维度", "")).strip()
    if group_raw:
        return split_fields(group_raw)
    return split_fields(entry.get("关联维度", ""))


def _has_time_grain(dims: list[str]) -> bool:
    return any(any(hint.lower() in dim.lower() for hint in TIME_DIM_HINTS) for dim in dims)


def build_query_plan(entry: dict[str, Any], target_store: Any | None = None) -> dict[str, Any]:
    """为一条星型查询决定实际扫描的数据来源。

    只认可 agg_summary 中带 _definition 戳记且校验通过的汇总字段定义：
    - 汇总分组维度必须与请求分组维度同粒度（完全一致）才可使用现成汇总
    - 请求更细时，粗粒度汇总不满足粒度，直接排除
    - 时间粒度上汇总不得粗于请求粒度
    选不到相符汇总时，生成基于原事实（含事实分区字段）的查询来源。
    """
    repo = target_store or store
    fact_name = str(entry.get("事实表", "")).strip()
    requested_dims = _requested_group_dims(entry)
    requested_set = set(requested_dims)

    fact_version = repo.get_fact_version(fact_name)
    if fact_version is None:
        # 未发布事实：回退行内信息构造版本快照
        fact_row = next(
            (row for row in repo.rows("fact_design") if row.get("事实名称") == fact_name),
            None,
        )
        fact_version = {
            "fact_name": fact_name,
            "version": 1,
            "related_dims": str((fact_row or {}).get("关联维度", "")),
            "grain": str((fact_row or {}).get("粒度声明", "")),
            "partition_field": str((fact_row or {}).get("分区字段", "")),
        }

    request_grain_rank = _grain_rank(entry.get("汇总粒度", "")) if entry.get("汇总粒度") else None
    request_has_time = _has_time_grain(requested_dims)

    candidates: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for agg in repo.rows("agg_summary"):
        definition = agg.get("_definition")
        validation = agg.get("_validation") or {}
        if not definition:
            continue
        if definition.get("source_fact") != fact_name:
            continue
        # 只使用校验通过的定义；DIM_MISSING 的失效汇总直接排除
        if not validation.get("valid") or validation.get("code") == VALIDATION_DIM_MISSING:
            rejected.append({
                "agg_id": agg.get("id"),
                "agg_name": agg.get("汇总名称", ""),
                "reason": "INVALID_DEFINITION",
                "missing_dims": validation.get("missing_dims", []),
            })
            continue

        agg_dims = list(definition.get("group_dims", []))
        agg_set = set(agg_dims)
        reason: str | None = None

        # 规则 1：汇总分组键必须与请求分组维度完全同粒度才允许使用现成数据。
        # 汇总更粗（agg ⊊ 请求）时无法还原细粒度，直接排除，回退原事实；
        # 汇总更细（请求 ⊊ agg）时分组键对不上请求粒度，同样不允许。
        if agg_set != requested_set:
            reason = "GRAIN_TOO_COARSE" if agg_set.issubset(requested_set) else "GRAIN_NOT_MATCH"
        elif request_has_time:
            # 规则 2：同维度集合下，时间粒度上汇总不得粗于请求粒度
            agg_rank = _grain_rank(definition.get("granularity", ""))
            if agg_rank is not None and request_grain_rank is not None and agg_rank > request_grain_rank:
                reason = "TIME_GRAIN_TOO_COARSE"
            elif agg_rank is not None and request_grain_rank is None and agg_rank > TIME_GRAIN_ORDER.index("每日"):
                # 请求按日期等细时间维度分组，却只有月/季/年汇总
                reason = "TIME_GRAIN_TOO_COARSE"

        if reason:
            rejected.append({
                "agg_id": agg.get("id"),
                "agg_name": agg.get("汇总名称", ""),
                "agg_dims": agg_dims,
                "reason": reason,
            })
            continue

        candidates.append({
            "agg_id": agg.get("id"),
            "agg_name": agg.get("汇总名称", ""),
            "agg_dims": agg_dims,
            "granularity": definition.get("granularity", ""),
            "fact_version": definition.get("fact_version"),
            "exact_match": agg_set == requested_set,
            "dim_count": len(agg_set),
            "applicable_from": agg.get("_applicable_from"),
            "applicable_to": agg.get("_applicable_to"),
            "legacy": definition.get("legacy", False),
        })

    if candidates:
        # 能进入候选的汇总都与请求粒度精确相符；同粒度下取维度最全、ID最小者
        candidates.sort(key=lambda item: (-item["dim_count"], item["agg_id"]))
        chosen = candidates[0]
        source = {
            "type": "summary",
            "summary_id": chosen["agg_id"],
            "summary_name": chosen["agg_name"],
            "source_fact": fact_name,
            "fact_version": chosen["fact_version"],
            "group_dims": chosen["agg_dims"],
            "granularity": chosen["granularity"],
            "exact_grain_match": True,
            "applicable_from": chosen["applicable_from"],
            "applicable_to": chosen["applicable_to"],
            "scanned_partitions": None,
        }
        match_note = "汇总字段定义与请求粒度相符，使用现成汇总数据"
    else:
        # 选不到相符汇总：基于原事实生成查询来源，并给出实际扫描的事实分区
        partition_field = str(fact_version.get("partition_field", "")).strip()
        partition_desc = _fact_partition_desc(fact_name, partition_field, entry)
        source = {
            "type": "fact",
            "summary_id": None,
            "summary_name": None,
            "source_fact": fact_name,
            "fact_version": fact_version.get("version"),
            "group_dims": requested_dims,
            "granularity": fact_version.get("grain", ""),
            "exact_grain_match": False,
            "applicable_from": fact_version.get("published_at"),
            "applicable_to": None,
            "scanned_partitions": partition_desc,
        }
        match_note = "无粒度相符的现成汇总，回退到原事实扫描"

    return {
        "query_id": entry.get("id"),
        "query_name": entry.get("查询名称", ""),
        "requested_dims": requested_dims,
        "source_fact": fact_name,
        "fact_version": fact_version.get("version"),
        "fact_grain": fact_version.get("grain", ""),
        "selected_source": source,
        "candidate_summaries": candidates,
        "rejected_summaries": rejected,
        "match_note": match_note,
        "generated_sql": _render_plan_sql(entry, source),
    }


def _fact_partition_desc(fact_name: str, partition_field: str, entry: dict[str, Any]) -> dict[str, Any]:
    """描述回退到原事实时实际扫描的分区。"""
    filters = str(entry.get("过滤条件", "")).strip()
    if not partition_field:
        return {"partition_field": None, "mode": "full_scan", "partitions": [], "filter": filters}
    # 从过滤条件里提取分区裁剪线索（模拟分区裁剪，不做真实SQL解析）
    hinted = [token.strip() for token in filters.replace("，", ",").split(",") if partition_field in token or "ds=" in token or "ds>" in token or "ds>=" in token]
    return {
        "partition_field": partition_field,
        "mode": "partition_pruned" if hinted else "full_scan",
        "partitions": hinted,
        "filter": filters or "1=1",
    }


def _render_plan_sql(entry: dict[str, Any], source: dict[str, Any]) -> str:
    """按选源结果渲染模拟SQL：命中汇总走汇总表，否则走原事实并带分区裁剪。"""
    measures = str(entry.get("度量选择", "")).strip()
    filters = str(entry.get("过滤条件", "")).strip() or "1=1"
    group = ".".join(source.get("group_dims") or []) or entry.get("分组维度", "")
    if source["type"] == "summary":
        table = f"agg_summary#{source['summary_id']}({source['summary_name']})"
        joins = ""
    else:
        table = f"{source['source_fact']} AS f"
        dim_list = [d.strip() for d in str(entry.get("关联维度", "")).split(".") if d.strip()]
        joins = "\n" + "\n".join(f"LEFT JOIN {d} ON f.{d}_id = {d}.id" for d in dim_list) if dim_list else ""
        partition = source.get("scanned_partitions") or {}
        if partition.get("mode") == "full_scan" and partition.get("partition_field"):
            filters = f"{filters} /* 无分区裁剪，全量扫描 {partition['partition_field']} */"
    return f"SELECT {group}, SUM({measures}) FROM {table}{joins}\nWHERE {filters}\nGROUP BY {group}"


class StarQueryService(BaseModuleService):
    MODULE = "star_query"
    ENTITY = "查询定义"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["查询名称", "事实表", "关联维度", "度量选择", "过滤条件", "分组维度", "结果限制"]
    REQUIRED_FIELDS = ["查询名称", "事实表", "关联维度"]
    STATUS_ORDER = ["草稿", "已验证", "已发布", "已过期"]
    ACTION_RULES = {"验证查询": "草稿", "发布查询": "已验证"}
    NEGATIVE_ACTIONS = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        fact_name = str(values.get("事实表", "")).strip()
        related_dims = str(values.get("关联维度", "")).strip()
        measures = str(values.get("度量选择", "")).strip()

        # 消费事实和维度通道校验
        fact_msgs = store.channel.consume("fact.published", "star_query")
        for msg in fact_msgs:
            if msg["payload"].get("fact_name") == fact_name:
                values["_fact_confirmed"] = True

        dim_msgs = store.channel.consume("dim.published", "star_query")
        for msg in dim_msgs:
            dim_code = msg["payload"].get("dim_code", "")
            if dim_code in related_dims:
                values[f"_dim_{dim_code}_confirmed"] = True

        # 如果未填度量，从channel消费
        if not measures:
            measure_msgs = store.channel.consume("measure.ready", "star_query")
            for m in measure_msgs:
                if m["payload"].get("fact_name") == fact_name:
                    measures = m["payload"].get("measure_name", "")
            if measures:
                values["度量选择"] = measures

        if not values.get("结果限制"):
            values["结果限制"] = "1000"

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        store.channel.publish("query.defined", {
            "query_name": entry["查询名称"],
            "fact_name": fact_name,
            "related_dims": related_dims,
            "measures": entry.get("度量选择"),
            "entry_id": entry["id"],
        })

        store.artifacts.register(
            "star_query", "star_query_def",
            f"query_{entry['id']}",
            {"query_name": entry["查询名称"], "fact_name": fact_name},
            ["fact_design", "dim_definition", "star_query"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "验证查询":
            # 按校验过的汇总字段定义决定选源，并把计划留在查询条目上供回查
            plan = build_query_plan(entry)
            entry["_query_plan"] = plan
            store.channel.publish("query.validated", {
                "query_name": entry["查询名称"],
                "generated_sql": plan["generated_sql"],
                "selected_source": plan["selected_source"],
                "entry_id": entry_id,
            })
        elif action == "发布查询":
            store.channel.publish("query.published", {
                "query_name": entry["查询名称"],
                "entry_id": entry_id,
            })
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["consumed_facts"] = len(store.channel.consume("fact.published", "star_query"))
        base["consumed_dims"] = len(store.channel.consume("dim.published", "star_query"))
        return base


def _generate_sql(entry: dict[str, Any]) -> str:
    """根据查询定义生成模拟SQL（保留兼容入口，内部走选源计划）。"""
    return build_query_plan(entry)["generated_sql"]
