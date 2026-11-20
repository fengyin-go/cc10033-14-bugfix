"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.errors import BusinessValidationError
from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.services.agg_summary import stamp_legacy_seed_summaries
from app.services.star_query import build_query_plan
from app.store import store

router = APIRouter(prefix="/api", tags=["业务资源"])


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/star_query/query-plan", response_model=dict)
def star_query_plan(entry_id: int = Query(..., ge=1)) -> dict[str, Any]:
    """星型查询选源：按校验过的汇总字段定义返回实际数据来源与扫描分区。"""
    service = service_for("star_query")
    entry = service.get_entry(entry_id) if service else None
    if entry is None:
        raise HTTPException(status_code=404, detail=f"查询定义 {entry_id} 不存在或已归档")
    return build_query_plan(entry)


@router.get("/agg_summary/summary-definition", response_model=dict)
def agg_summary_definition(entry_id: int = Query(..., ge=1)) -> dict[str, Any]:
    """回查同一份汇总字段定义：登记页与星型查询入口共用此只读视图。"""
    service = service_for("agg_summary")
    entry = service.get_entry(entry_id) if service else None
    if entry is None:
        raise HTTPException(status_code=404, detail=f"汇总表 {entry_id} 不存在或已归档")
    return {
        "entry_id": entry.get("id"),
        "汇总名称": entry.get("汇总名称", ""),
        "源事实表": entry.get("源事实表", ""),
        "汇总维度": entry.get("汇总维度", ""),
        "汇总度量": entry.get("汇总度量", ""),
        "汇总粒度": entry.get("汇总粒度", ""),
        "definition": entry.get("_definition"),
        "validation": entry.get("_validation"),
        "applicable_from": entry.get("_applicable_from"),
        "applicable_to": entry.get("_applicable_to"),
        "status": entry.get("status"),
    }


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    try:
        entry, missing = service.create_entry(payload.values)
    except BusinessValidationError as exc:
        # 定义不生效：返回错误码、不能上卷的维度和原始输入，前端保留表单不清空
        return ActionResult(
            ok=False,
            message=exc.message,
            code=exc.code,
            details=exc.details,
            retained=exc.payload,
        )
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}",
                            retained=payload.values)
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
