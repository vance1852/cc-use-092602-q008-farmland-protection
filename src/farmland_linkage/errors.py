"""耕地保护与宅基地退出联动服务向 API 和 CLI 暴露的稳定错误。"""

from __future__ import annotations

from typing import Any, Mapping


class LinkageError(RuntimeError):
    code = "linkage_error"
    status = 400

    def __init__(self, message: str = "", *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details) if details else None


class NotFound(LinkageError):
    code = "not_found"
    status = 404


class Conflict(LinkageError):
    code = "conflict"
    status = 409


class Forbidden(LinkageError):
    code = "forbidden"
    status = 403


class InvalidState(LinkageError):
    code = "invalid_state"
    status = 409


class ValidationFailed(LinkageError):
    code = "validation_failed"
    status = 422
