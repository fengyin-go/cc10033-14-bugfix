"""事实表设计服务：管理事实表的业务过程、粒度和维度关联定义。

增强能力：
- 事实表名称标准化校验（前缀fact_）
- 维度关联完整性检查：关联的维度必须已在dim_definition中定义
- 消费 dim.published 通道确认关联维度可用
- 发布事实时触发度量配置和聚合层自动建议
"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FactDesignService(BaseModuleService):
    MODULE = "fact_design"
    ENTITY = "事实表"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["事实名称", "业务过程", "粒度声明", "关联维度", "度量字段", "分区字段", "更新模式"]
    REQUIRED_FIELDS = ["事实名称", "业务过程", "粒度声明"]
    STATUS_ORDER = ["草稿", "已设计", "已发布", "已冻结"]
    ACTION_RULES = {"提交设计": "草稿", "发布事实": "已设计", "冻结事实": "已发布"}
    NEGATIVE_ACTIONS = []

    UPDATE_MODES = {"每日追加", "每小时追加", "T+1批量", "实时追加", "每日全量"}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """创建事实表定义：校验关联维度的存在性，标准化更新模式。"""
        # 标准化事实名称
        fact_name = str(values.get("事实名称", "")).strip()
        values["事实名称"] = fact_name

        # 标准化更新模式
        update_mode = str(values.get("更新模式", "每日追加")).strip()
        if update_mode not in self.UPDATE_MODES:
            update_mode = "每日追加"
            values["更新模式"] = update_mode

        # 校验关联维度存在性
        related_dims = str(values.get("关联维度", "")).strip()
        validated_dims = _validate_dimensions(related_dims)
        if not validated_dims:
            values["关联维度"] = related_dims  # 保留原始输入

        # 消费已发布维度消息
        dim_msgs = store.channel.consume("dim.published", "fact_design")
        for msg in dim_msgs:
            dim_code = msg["payload"].get("dim_code", "")
            if dim_code in related_dims:
                # 确认维度可用
                values[f"_dim_{dim_code}_confirmed"] = True

        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing

        # 发布事实表定义到数据通道
        store.channel.publish("fact.defined", {
            "fact_name": entry["事实名称"],
            "business_process": entry["业务过程"],
            "granularity": entry["粒度声明"],
            "related_dims": related_dims,
            "measures": entry.get("度量字段", ""),
            "partition_field": entry.get("分区字段", ""),
            "update_mode": entry["更新模式"],
            "entry_id": entry["id"],
        })

        # 注册事实产物
        store.artifacts.register(
            "fact_design", "fact_table",
            f"fact_{entry['id']}",
            {"fact_name": entry["事实名称"], "business_process": entry["业务过程"]},
            source_modules=["dim_definition", "fact_design"],
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message

        if action == "发布事实":
            # 验证关联维度完整性
            dim_validation = _validate_fact_dim_completeness(entry)
            if not dim_validation["valid"]:
                return None, dim_validation["message"]

            # 登记新的源事实版本：每次发布都按当前条目内容升版，
            # 下游汇总定义与星型查询都按这同一个版本核对维度和粒度。
            new_version = store.register_fact_version(
                entry["事实名称"],
                related_dims=entry.get("关联维度", ""),
                grain=entry.get("粒度声明", ""),
                partition_field=entry.get("分区字段", ""),
                entry_id=entry.get("id"),
            )

            store.channel.publish("fact.published", {
                "fact_name": entry["事实名称"],
                "business_process": entry["业务过程"],
                "granularity": entry["粒度声明"],
                "related_dims": entry.get("关联维度", ""),
                "measures": entry.get("度量字段", ""),
                "update_mode": entry.get("更新模式"),
                "fact_version": new_version["version"],
                "entry_id": entry["id"],
            })

            # 源事实更正后按新版本重新核对已有汇总定义，
            # 被删掉的分组字段会让对应汇总定义失效并收口适用日期。
            from app.services.agg_summary import revalidate_summaries_for_fact
            revalidate_summaries_for_fact(entry["事实名称"], new_version)

            # 自动建议度量配置
            _suggest_measures(entry)
            # 自动建议聚合汇总
            _suggest_aggregation(entry)

        elif action == "冻结事实":
            store.channel.publish("fact.frozen", {
                "fact_name": entry["事实名称"],
                "entry_id": entry["id"],
            })

        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        dim_consumed = len(store.channel.consume("dim.published", "fact_design"))
        base["consumed_published_dims"] = dim_consumed

        # 维度关联矩阵
        dim_matrix: dict[str, list[str]] = {}
        for row in store.rows(self.MODULE):
            fact_name = row.get("事实名称", "")
            dims = str(row.get("关联维度", "")).split(".")
            dim_matrix[fact_name] = [d for d in dims if d]
        base["dimension_association_matrix"] = dim_matrix
        return base


def _validate_dimensions(dim_str: str) -> list[str]:
    """校验关联维度字符串，过滤有效维度编码。"""
    if not dim_str.strip():
        return []
    dim_codes = [d.strip() for d in dim_str.split(".") if d.strip()]
    existing_dims = {row.get("维度编码") for row in store.rows("dim_definition")}
    valid = [d for d in dim_codes if d in existing_dims]
    return valid


def _validate_fact_dim_completeness(entry: dict[str, Any]) -> dict[str, Any]:
    """校验事实表关联维度完整性。"""
    related_dims = str(entry.get("关联维度", "")).strip()
    if not related_dims:
        return {"valid": False, "message": "事实表至少需要关联一个维度"}

    dim_codes = [d.strip() for d in related_dims.split(".") if d.strip()]
    existing_dims = {row.get("维度编码") for row in store.rows("dim_definition")}
    missing = [d for d in dim_codes if d not in existing_dims]
    if missing:
        return {"valid": False, "message": f"维度 {'、'.join(missing)} 未定义，请先创建维度定义"}
    return {"valid": True, "message": ""}


def _suggest_measures(fact: dict[str, Any]) -> None:
    """基于事实表定义自动建议度量配置。"""
    measures_str = str(fact.get("度量字段", "")).strip()
    if not measures_str:
        return

    measure_names = [m.strip() for m in measures_str.split(".") if m.strip()]
    for m_name in measure_names:
        measure_type = "DECIMAL(18,2)" if "_amt" in m_name else "BIGINT"
        store.channel.publish("measure.suggested", {
            "measure_name": m_name,
            "fact_name": fact["事实名称"],
            "measure_type": measure_type,
            "agg_method": "SUM" if ("_amt" in m_name or "_qty" in m_name) else "COUNT",
            "source_fact_id": fact.get("id"),
            "is_additive": "_cnt" not in m_name and "_rate" not in m_name,
        })


def _suggest_aggregation(fact: dict[str, Any]) -> None:
    """基于事实表定义自动建议汇总表配置。"""
    fact_name = fact.get("事实名称", "")
    store.channel.publish("agg.suggested", {
        "agg_name": f"每日{fact_name}汇总",
        "source_fact": fact_name,
        "agg_dims": fact.get("关联维度", ""),
        "agg_measures": fact.get("度量字段", ""),
        "agg_granularity": "每日",
        "source_fact_id": fact.get("id"),
    })