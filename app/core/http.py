"""HTTP response defaults shared by the FastAPI application."""
from __future__ import annotations

from fastapi.responses import JSONResponse


class UTF8JSONResponse(JSONResponse):
    """Emit an explicit UTF-8 charset for Windows/PowerShell clients.

    JSON is UTF-8 by specification, but Windows PowerShell 5 may choose a legacy
    code page when the response omits a charset parameter.  Making it explicit
    keeps Korean operator messages readable without changing the response body.
    """

    media_type = "application/json; charset=utf-8"
