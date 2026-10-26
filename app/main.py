"""FastAPI 应用装配 + 静态资源托管。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api, db, repo, scheduler, security, settings

log = logging.getLogger("cdt-management")


def _static_dir() -> Path | None:
    candidates = [
        Path(__file__).resolve().parent / "static",
        Path(__file__).resolve().parent.parent / "web" / "dist",
    ]
    for path in candidates:
        if (path / "index.html").exists():
            return path
    return None


def create_app() -> FastAPI:
    settings.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    db.init_db()
    _bootstrap_admin()

    app = FastAPI(
        title=settings.BRAND,
        version=settings.VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    api.register(app)

    static = _static_dir()
    if static is not None:
        assets = static / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):  # noqa: ANN202
            if full_path.startswith("api/"):
                return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "接口不存在"}})
            candidate = (static / full_path).resolve()
            if full_path and candidate.is_file() and str(candidate).startswith(str(static)):
                return FileResponse(candidate)
            return FileResponse(static / "index.html")
    else:

        @app.get("/")
        def placeholder():  # noqa: ANN202
            return JSONResponse(
                {
                    "message": "前端资源未构建。开发时请先 npm run build，或直接用 Vite dev server。",
                    "hint": "cd web && npm install && npm run build",
                },
                status_code=503,
            )

    @app.on_event("startup")
    def _startup() -> None:  # noqa: ANN202
        scheduler.start()
        log.info("%s v%s 已启动，数据目录 %s", settings.BRAND, settings.VERSION, settings.DATA_DIR)

    @app.on_event("shutdown")
    def _shutdown() -> None:  # noqa: ANN202
        scheduler.stop()

    return app


def _bootstrap_admin() -> None:
    """支持用环境变量跳过初始化向导（适合无人值守部署）。"""
    if repo.get_setting("initialized") == "1":
        return
    username = settings.BOOTSTRAP_USERNAME
    password = settings.BOOTSTRAP_PASSWORD
    if not username or not password:
        return
    if len(password) < 10:
        log.warning("CDT_ADMIN_PASSWORD 少于 10 个字符，忽略自动初始化")
        return
    repo.set_settings(
        {
            "admin_username": username,
            "admin_password": security.hash_password(password),
            "initialized": "1",
        }
    )
    log.info("已根据环境变量创建管理员 %s", username)


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.LISTEN_HOST,
        port=settings.LISTEN_PORT,
        log_level=os.environ.get("CDT_LOG_LEVEL", "info").lower(),
        access_log=False,
    )


if __name__ == "__main__":
    run()
