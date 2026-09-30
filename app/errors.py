"""统一业务异常与处理器。"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, status_code: int, error: str, message: str, details=None):
        self.status_code = status_code
        self.error = error
        self.message = message
        self.details = details
        super().__init__(message)


def register_exception_handlers(app) -> None:
    @app.exception_handler(APIError)
    async def _handle_api_error(_: Request, exc: APIError) -> JSONResponse:
        body = {"error": exc.error, "message": exc.message}
        if exc.details is not None:
            body["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=body)
