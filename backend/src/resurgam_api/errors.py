import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from resurgam_api.schemas.errors import ErrorResponse

logger = logging.getLogger("resurgam_api.errors")


class ResurgamApiError(Exception):
    """Base class for domain errors that map to a typed API error response."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        super().__init__(message)


def _correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", "unknown")


def _field_name(loc: tuple) -> str:
    # ("body", "match_code") -> "match_code"; ("query", "limit") -> "limit"
    parts = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
    return ".".join(parts) or "request"


def _validation_errors(exc: RequestValidationError) -> list[dict]:
    """Field + reason only. The submitted value (`input`) is deliberately
    dropped: forms send passwords and API tokens, and an error response
    must never echo them back (or into logs)."""
    errors = []
    for err in exc.errors():
        message = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
        errors.append({"field": _field_name(tuple(err.get("loc", ()))), "message": message, "type": err.get("type")})
    return errors


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ResurgamApiError)
    async def handle_resurgam_error(request: Request, exc: ResurgamApiError) -> JSONResponse:
        body = ErrorResponse(
            code=exc.code,
            message=exc.message,
            correlation_id=_correlation_id(request),
            details=exc.details,
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    # FastAPI's default 422 body ({"detail": [...]}) has no code/message, so
    # the UI could only show "Unprocessable Entity" -- a real user couldn't
    # tell which field of the "create customer" form was wrong.
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = _validation_errors(exc)
        message = "; ".join(f"{e['field']}: {e['message']}" for e in errors) or "The request is invalid."
        body = ErrorResponse(
            code="validation_error",
            message=message,
            correlation_id=_correlation_id(request),
            details={"errors": errors},
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        body = ErrorResponse(
            code=f"http_{exc.status_code}",
            message=str(exc.detail),
            correlation_id=_correlation_id(request),
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        correlation_id = _correlation_id(request)
        logger.exception("unhandled error, correlation_id=%s", correlation_id)
        body = ErrorResponse(
            code="internal_error",
            message="An unexpected error occurred.",
            correlation_id=correlation_id,
        )
        return JSONResponse(status_code=500, content=body.model_dump())
