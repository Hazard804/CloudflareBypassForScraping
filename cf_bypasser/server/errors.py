"""Typed HTTP errors so callers can tell *why* a bypass failed.

`detail` stays byte-identical to the historical message (existing clients compare on it); the
extra `error_code` field is additive and machine-readable, e.g.:

    {"detail": "Failed to bypass Cloudflare protection", "error_code": "challenge_timeout"}

Codes: blocked | challenge_timeout | cookie_missing_clearance | navigation_failed |
       job_timeout | browser_error | bypass_failed
"""

from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class BypassError(HTTPException):
    """HTTPException plus a stable reason code."""

    def __init__(self, *, status_code: int = 500, detail: str, code: str = ""):
        super().__init__(status_code=status_code, detail=detail)
        self.code = str(code or "").strip() or "bypass_failed"


async def bypass_error_handler(request: Request, exc: BypassError) -> JSONResponse:
    headers: Optional[dict] = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "error_code": exc.code},
        headers=headers,
    )
