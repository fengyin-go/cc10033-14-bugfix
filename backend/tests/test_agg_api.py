"""HTTP 路由层契约测试：不引入 httpx，直接调用资源路由函数，
锁定「校验失败保留输入、两入口共享同一份定义、粒度不符回退事实分区」的响应结构。
"""
from __future__ import annotations

import pytest

from app.routers.resource import agg_summary_definition, create_entry, star_query_plan
from app.schemas import EntryPayload


@pytest.fixture
def reset_store(monkeypatch: pytest.MonkeyPatch):
    """每个用例使用独立 store，并把各服务模块指向它。"""
    from app.store import Store
    import app.services.base as base_mod
    import app.services.agg_summary as agg_mod
    import app.services.fact_design as fact_mod
    import app.services.star_query as star_mod

    fresh = Store()
    for module in (base_mod, agg_mod, fact_mod, star_mod):
        monkeypatch.setattr(module, "store", fresh)
    return fresh


def test_dim_missing_response_retains_form_input(reset_store) -> None:
    result = create_entry("agg_summary", EntryPayload(values={
        "汇总名称": "DIM_MISSING接口汇总",
        "源事实表": "销售明细事实",
        "汇总维度": "DIM_TIME.DIM_GHOST",
        "汇总度量": "gmv_amt",
        "汇总粒度": "每日",
    }))
    assert result.ok is False
    assert result.code == "DIM_MISSING"
    assert result.details["missing_dims"] == ["DIM_GHOST"]
    # 提交失败后原样回显输入，前端可直接回填表单、不清空
    assert result.retained["汇总维度"] == "DIM_TIME.DIM_GHOST"
    assert result.entry is None
    assert all(row["汇总名称"] != "DIM_MISSING接口汇总" for row in reset_store.rows("agg_summary"))


def test_two_entries_read_same_summary_definition(reset_store) -> None:
    created = create_entry("agg_summary", EntryPayload(values={
        "汇总名称": "双入口共享定义",
        "源事实表": "销售明细事实",
        "汇总维度": "DIM_TIME.DIM_STORE",
        "汇总度量": "gmv_amt",
        "汇总粒度": "每日",
    }))
    assert created.ok is True
    entry_id = created.entry["id"]

    # 入口一：汇总登记页的定义回查
    definition_view = agg_summary_definition(entry_id=entry_id)
    assert definition_view["definition"]["group_dims"] == ["DIM_TIME", "DIM_STORE"]
    assert definition_view["validation"]["code"] == "VALID"

    # 入口二：星型查询选源读到同一份定义（同 fact_version、同分组键）
    query = create_entry("star_query", EntryPayload(values={
        "查询名称": "双入口一致性查询",
        "事实表": "销售明细事实",
        "关联维度": "DIM_TIME.DIM_STORE",
        "度量选择": "gmv_amt",
        "分组维度": "DIM_TIME.DIM_STORE",
        "过滤条件": "ds=最近7天",
    })).entry
    plan = star_query_plan(entry_id=query["id"])
    source = plan["selected_source"]
    assert source["type"] == "summary"
    assert source["summary_id"] == entry_id
    assert source["group_dims"] == definition_view["definition"]["group_dims"]
    assert source["fact_version"] == definition_view["definition"]["fact_version"]


def test_finer_grain_query_reports_fact_partitions(reset_store) -> None:
    create_entry("agg_summary", EntryPayload(values={
        "汇总名称": "粗粒度门店汇总",
        "源事实表": "销售明细事实",
        "汇总维度": "DIM_TIME.DIM_STORE",
        "汇总度量": "gmv_amt",
        "汇总粒度": "每日",
    }))
    query = create_entry("star_query", EntryPayload(values={
        "查询名称": "更细粒度产品查询",
        "事实表": "销售明细事实",
        "关联维度": "DIM_TIME.DIM_STORE.DIM_PRODUCT",
        "度量选择": "gmv_amt",
        "分组维度": "DIM_TIME.DIM_STORE.DIM_PRODUCT",
        "过滤条件": "ds=昨天",
    })).entry
    plan = star_query_plan(entry_id=query["id"])
    source = plan["selected_source"]
    assert source["type"] == "fact"
    assert source["scanned_partitions"]["partition_field"] == "dt DATE"
    assert source["scanned_partitions"]["mode"] == "partition_pruned"
    assert any("昨天" in part for part in source["scanned_partitions"]["partitions"])
    rejected = {(r["agg_name"], r["reason"]) for r in plan["rejected_summaries"]}
    assert ("粗粒度门店汇总", "GRAIN_TOO_COARSE") in rejected
