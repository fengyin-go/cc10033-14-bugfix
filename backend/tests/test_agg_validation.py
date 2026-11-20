"""汇总字段定义校验与星型查询选源测试。

覆盖：
- DIM_MISSING：不能上卷的维度被指出、原始输入保留、定义不生效、不落库
- 单次校验按同一个源事实版本核对维度和粒度
- 提交失败不清空输入，失效字段不会进入新汇总的分组键
- 星型查询按校验过的汇总定义选源；粒度不符时回退原事实分区，细粒度不错用粗汇总
- 源事实更正升版后历史汇总失效、适用日期收口，历史查询回退原事实并留痕
- 两个入口回查同一份汇总定义
"""
from __future__ import annotations

import pytest

from app.errors import BusinessValidationError
from app.services.agg_summary import (
    VALIDATION_DIM_MISSING,
    VALIDATION_GRAIN_MISMATCH,
    AggSummaryService,
    stamp_legacy_seed_summaries,
)
from app.services.star_query import build_query_plan
from app.store import Store


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def agg_service(store: Store, monkeypatch: pytest.MonkeyPatch) -> AggSummaryService:
    # 服务方法内部使用模块级 store；测试统一把各服务模块指向用例内独立 store
    import app.services.agg_summary as agg_mod
    import app.services.base as base_mod
    import app.services.fact_design as fact_mod
    import app.services.star_query as star_mod

    for module in (base_mod, agg_mod, fact_mod, star_mod):
        monkeypatch.setattr(module, "store", store)
    return AggSummaryService()


def _make_store_sales_summary(agg_service: AggSummaryService) -> dict:
    entry, missing = agg_service.create_entry({
        "汇总名称": "门店日销售汇总测试",
        "源事实表": "销售明细事实",
        "汇总维度": "DIM_TIME.DIM_STORE",
        "汇总度量": "gmv_amt",
        "汇总粒度": "每日",
        "增量标记": "是",
    })
    assert missing == []
    return entry


class TestSummaryDefinitionValidation:
    """汇总字段定义校验：保留原字段、指出不能上卷的维度并阻止定义生效。"""

    def test_dim_missing_blocks_definition_and_keeps_input(self, store: Store, agg_service: AggSummaryService) -> None:
        before = len(store.rows("agg_summary"))
        submitted = {
            "汇总名称": "DIM_MISSING验证汇总",
            "源事实表": "销售明细事实",
            "汇总维度": "DIM_TIME.DIM_GHOST",
            "汇总度量": "gmv_amt",
            "汇总粒度": "每日",
        }
        with pytest.raises(BusinessValidationError) as exc_info:
            agg_service.create_entry(dict(submitted))

        err = exc_info.value
        assert err.code == VALIDATION_DIM_MISSING
        # 明确指出不能上卷的维度
        assert err.details["missing_dims"] == ["DIM_GHOST"]
        assert err.details["rollup_dims"] == ["DIM_TIME"]
        # 单次校验锚定同一个源事实版本
        assert err.details["fact_version"] == 1
        assert err.details["source_fact"] == "销售明细事实"
        # 原始输入完整保留，未被静默裁剪
        assert err.payload["汇总维度"] == "DIM_TIME.DIM_GHOST"
        # 定义不生效、不落库
        assert len(store.rows("agg_summary")) == before

    def test_valid_definition_is_stamped_with_fact_version(self, store: Store, agg_service: AggSummaryService) -> None:
        entry = _make_store_sales_summary(agg_service)
        assert entry["_validation"]["code"] == "VALID"
        assert entry["_validation"]["valid"] is True
        assert entry["_definition"]["group_dims"] == ["DIM_TIME", "DIM_STORE"]
        assert entry["_definition"]["fact_version"] == 1
        assert entry["_applicable_from"] and entry["_applicable_to"] is None

    def test_unknown_fact_is_rejected(self, store: Store, agg_service: AggSummaryService) -> None:
        with pytest.raises(BusinessValidationError) as exc_info:
            agg_service.create_entry({
                "汇总名称": "无源事实汇总",
                "源事实表": "不存在的事实",
                "汇总维度": "DIM_TIME",
                "汇总粒度": "每日",
            })
        assert exc_info.value.code == "FACT_NOT_FOUND"

    def test_invalid_granularity_is_rejected_without_rewrite(self, store: Store, agg_service: AggSummaryService) -> None:
        with pytest.raises(BusinessValidationError) as exc_info:
            agg_service.create_entry({
                "汇总名称": "粒度非法汇总",
                "源事实表": "销售明细事实",
                "汇总维度": "DIM_TIME",
                "汇总粒度": "每纳秒",
            })
        assert exc_info.value.code == VALIDATION_GRAIN_MISMATCH
        assert exc_info.value.payload["汇总粒度"] == "每纳秒"

    def test_failed_agg_cannot_be_built(self, store: Store, agg_service: AggSummaryService) -> None:
        entry = _make_store_sales_summary(agg_service)
        agg_service.run_action(entry["id"], "执行构建")
        # 手工让定义失效后，构建必须被拦截
        entry["_validation"] = {
            "code": VALIDATION_DIM_MISSING,
            "valid": False,
            "missing_dims": ["DIM_STORE"],
            "source_fact": "销售明细事实",
            "fact_version": 2,
        }
        result, message = agg_service.run_action(entry["id"], "执行构建")
        assert result is None
        assert "DIM_STORE" in message


class TestFactCorrectionRevalidation:
    """源事实更正删去分组字段后，历史汇总定义失效并收口适用日期。"""

    def test_correction_bumps_version_and_invalidates_summary(
        self, store: Store, agg_service: AggSummaryService
    ) -> None:
        from app.services.fact_design import FactDesignService

        entry = _make_store_sales_summary(agg_service)
        fact_service = FactDesignService()

        fact = next(r for r in store.rows("fact_design") if r["事实名称"] == "销售明细事实")
        fact["关联维度"] = "DIM_TIME.DIM_PRODUCT"  # 删去 DIM_STORE
        fact["status"] = "已设计"
        fact_service.run_action(fact["id"], "发布事实")

        version = store.get_fact_version("销售明细事实")
        assert version["version"] == 2
        assert "DIM_STORE" not in version["related_dims"]

        agg_row = store.find("agg_summary", entry["id"])
        assert agg_row["_validation"]["code"] == VALIDATION_DIM_MISSING
        assert agg_row["_validation"]["missing_dims"] == ["DIM_STORE"]
        assert agg_row["_validation"]["fact_version"] == 2
        # 适用日期收口，历史区间仍可追溯
        assert agg_row["_applicable_to"] is not None
        assert agg_row["abnormal"] is True

        # 失效字段不允许再进入新汇总的分组键
        with pytest.raises(BusinessValidationError) as exc_info:
            agg_service.create_entry({
                "汇总名称": "再次尝试失效字段",
                "源事实表": "销售明细事实",
                "汇总维度": "DIM_TIME.DIM_STORE",
                "汇总粒度": "每日",
            })
        assert exc_info.value.code == VALIDATION_DIM_MISSING
        assert exc_info.value.payload["汇总维度"] == "DIM_TIME.DIM_STORE"

    def test_correction_keeps_compatible_summary_valid(self, store: Store, agg_service: AggSummaryService) -> None:
        from app.services.fact_design import FactDesignService

        entry = agg_service.create_entry({
            "汇总名称": "仅时间汇总",
            "源事实表": "销售明细事实",
            "汇总维度": "DIM_TIME",
            "汇总度量": "gmv_amt",
            "汇总粒度": "每日",
        })[0]

        fact_service = FactDesignService()
        fact = next(r for r in store.rows("fact_design") if r["事实名称"] == "销售明细事实")
        fact["关联维度"] = "DIM_TIME.DIM_PRODUCT"
        fact["status"] = "已设计"
        fact_service.run_action(fact["id"], "发布事实")

        agg_row = store.find("agg_summary", entry["id"])
        assert agg_row["_validation"]["valid"] is True
        assert agg_row["_definition"]["fact_version"] == 2


class TestStarQueryRouting:
    """星型查询按校验过的汇总定义选源，粒度不符回退原事实。"""

    @pytest.fixture
    def queries(self, store: Store, agg_service: AggSummaryService):
        from app.services.star_query import StarQueryService

        summary = _make_store_sales_summary(agg_service)
        agg_service.run_action(summary["id"], "执行构建")
        agg_service.run_action(summary["id"], "刷新汇总")
        query_service = StarQueryService()

        matching = query_service.create_entry({
            "查询名称": "门店日粒度查询",
            "事实表": "销售明细事实",
            "关联维度": "DIM_TIME.DIM_STORE",
            "度量选择": "gmv_amt",
            "分组维度": "DIM_TIME.DIM_STORE",
            "过滤条件": "ds=最近30天",
        })[0]
        finer = query_service.create_entry({
            "查询名称": "产品细粒度查询",
            "事实表": "销售明细事实",
            "关联维度": "DIM_TIME.DIM_STORE.DIM_PRODUCT",
            "度量选择": "gmv_amt",
            "分组维度": "DIM_TIME.DIM_STORE.DIM_PRODUCT",
            "过滤条件": "ds=昨天",
        })[0]
        return summary, matching, finer

    def test_exact_grain_uses_summary(self, store: Store, queries) -> None:
        _summary, matching, _finer = queries
        plan = build_query_plan(matching, store)
        source = plan["selected_source"]
        assert source["type"] == "summary"
        assert source["summary_name"] == "门店日销售汇总测试"
        assert source["exact_grain_match"] is True
        assert source["applicable_from"] and source["applicable_to"] is None

    def test_finer_grain_never_uses_coarse_summary(self, store: Store, queries) -> None:
        _summary, _matching, finer = queries
        plan = build_query_plan(finer, store)
        source = plan["selected_source"]
        # 细粒度查询必须回退原事实
        assert source["type"] == "fact"
        assert source["source_fact"] == "销售明细事实"
        assert source["scanned_partitions"]["partition_field"] == "dt DATE"
        assert source["scanned_partitions"]["mode"] == "partition_pruned"
        # 粗粒度汇总出现在拒绝列表并说明原因
        rejected = {(r["agg_name"], r["reason"]) for r in plan["rejected_summaries"]}
        assert ("门店日销售汇总测试", "GRAIN_TOO_COARSE") in rejected
        assert "销售明细事实 AS f" in plan["generated_sql"]

    def test_historical_query_falls_back_after_fact_correction(self, store: Store, queries) -> None:
        from app.services.fact_design import FactDesignService

        summary, matching, _finer = queries
        applicable_before = summary["_applicable_from"]

        fact_service = FactDesignService()
        fact = next(r for r in store.rows("fact_design") if r["事实名称"] == "销售明细事实")
        fact["关联维度"] = "DIM_TIME.DIM_PRODUCT"
        fact["status"] = "已设计"
        fact_service.run_action(fact["id"], "发布事实")

        plan = build_query_plan(matching, store)
        source = plan["selected_source"]
        # 历史查询仍要求原粒度，但汇总已失效 -> 回退原事实
        assert source["type"] == "fact"
        assert source["fact_version"] == 2
        # 结果里保留实际数据来源与适用日期
        assert source["applicable_from"] is not None
        invalidated = [r for r in plan["rejected_summaries"] if r["reason"] == "INVALID_DEFINITION"]
        assert invalidated
        all_missing = {dim for item in invalidated for dim in item["missing_dims"]}
        assert "DIM_STORE" in all_missing

        agg_row = store.find("agg_summary", summary["id"])
        assert agg_row["_applicable_from"] == applicable_before
        assert agg_row["_applicable_to"] is not None

    def test_seed_legacy_summaries_share_one_definition_view(self, store: Store) -> None:
        stamp_legacy_seed_summaries(store)
        seed = next(r for r in store.rows("agg_summary") if r["汇总名称"] == "每日门店绩效")
        # 两个入口回查的是同一份戳记：登记条目与定义视图字段一致
        assert seed["_definition"]["group_dims"] == ["日期", "门店"]
        assert seed["_validation"]["source_fact"] == seed["源事实表"]
        plan_view = build_query_plan({
            "id": 999,
            "查询名称": "临时查询",
            "事实表": "销售明细事实",
            "关联维度": "DIM_TIME.DIM_STORE",
            "分组维度": "DIM_TIME.DIM_STORE",
            "度量选择": "gmv_amt",
            "过滤条件": "1=1",
        }, store)
        # 失效的种子汇总不会被选为数据源
        assert plan_view["selected_source"]["type"] in {"fact", "summary"}
        if plan_view["selected_source"]["type"] == "summary":
            assert plan_view["selected_source"]["summary_name"] != "每日门店绩效"
