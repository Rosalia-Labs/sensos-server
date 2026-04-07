# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Rosalia Labs LLC

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from admin_api import router as admin_router
from client_api import router as client_router

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request):
    if getattr(request.app.state, "schema_ready", False):
        return {"status": "ok"}
    return JSONResponse(status_code=503, content={"status": "starting"})


router.include_router(client_router)
router.include_router(admin_router)
