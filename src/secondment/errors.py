"""领域错误类型，携带稳定的错误码与 HTTP 状态。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误基类。"""

    code = "domain_error"
    status = 400

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFoundError(DomainError):
    code = "not_found"
    status = 404


class ValidationError(DomainError):
    code = "validation_error"
    status = 400


class PermissionDenied(DomainError):
    code = "permission_denied"
    status = 403


class ConflictError(DomainError):
    code = "conflict"
    status = 409


class StateError(DomainError):
    code = "invalid_state"
    status = 409
