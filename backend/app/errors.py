"""领域校验异常：校验不通过时携带结构化错误码与明细，路由层据此返回 ok=false，
而不是让服务把输入静默改写后照常登记。
"""
from __future__ import annotations

from typing import Any


class BusinessValidationError(Exception):
    """业务规则校验失败。

    code: 机器可读错误码（如 DIM_MISSING / FACT_NOT_FOUND / GRANULARITY_INVALID）
    message: 面向用户的完整说明
    details: 结构化明细（不能上卷的维度、源事实版本、回显的原始输入等）
    payload: 提交的原始字段，供前端原样回显，保证提交失败不清空输入
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.payload = payload or {}
