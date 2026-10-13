"""Independent public transport setup; storage and business APIs remain unavailable."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

MESSAGE = "公网后端已启动，S3 和公网账号尚未配置，暂不能登录、上传或标注。"


def public_origin(value: str, *, api: bool = False) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.port not in ({None, 8443} if api else {None})
            or value != "https://" + parsed.hostname + (":8443" if parsed.port == 8443 else "") or "." not in parsed.hostname
            or any(not label or not label[0].isalnum() or not label[-1].isalnum()
                   or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
                   for label in parsed.hostname.split("."))):
        raise ValueError("Public setup requires a plain HTTPS frontend origin and an API origin on 443 or 8443")
    return value


def create_app() -> FastAPI:
    frontend = public_origin(os.getenv("DATAMARK_PUBLIC_ORIGIN", ""))
    api = public_origin(os.getenv("DATAMARK_PUBLIC_API_ORIGIN", ""), api=True)
    if urlsplit(frontend).hostname == urlsplit(api).hostname:
        raise ValueError("Public frontend and API must use separate hostnames")
    app = FastAPI(title="DataMark public setup", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(api).hostname, "127.0.0.1", "localhost"])

    @app.middleware("http")
    async def headers(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") not in {frontend, api}:
            response = JSONResponse(status_code=403, content={"detail": "仅允许已配置的标注页面访问。"})
        else:
            response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"})
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok", "application": "datamark", "mode": "public-setup",
                "ready": False, "storage_backend": "unconfigured", "storage_ready": False,
                "accounts_ready": False, "browser_lifetime": False,
                "capabilities": ["public-api-setup", "browser-connection"], "detail": MESSAGE}

    @app.websocket("/api/browser/connection")
    async def browser_connection(websocket: WebSocket):
        if websocket.headers.get("origin") not in {frontend, api}:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass

    @app.api_route("/api/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    def unavailable(path: str):
        return JSONResponse(status_code=503, content={"detail": MESSAGE, "code": "public_setup_required"})

    app.add_middleware(CORSMiddleware, allow_origins=[frontend], allow_credentials=True,
                       allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Content-Type", "X-CSRF-Token", "Range"])
    return app
