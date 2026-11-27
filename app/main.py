"""FastAPI 应用装配 + 静态资源托管。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api, db, dns, repo, scheduler, security, settings

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
        # 两套皮肤各自带 assets：classic 在 static/assets，glass 在 static/glass/assets。
        # 都挂出来，index.html 按皮肤引用各自那份（/assets/... 或 /glass/assets/...）
        for prefix, folder in (("/assets", static / "assets"), ("/glass/assets", static / "glass" / "assets")):
            if folder.exists():
                app.mount(prefix, StaticFiles(directory=str(folder)), name=prefix.strip("/").replace("/", "_"))

        def _boot_script() -> str:
            """把后端真实设置注入 index.html，首屏直接用它，避免刷新时主题/皮肤闪一下。"""
            try:
                config = repo.get_settings()
                theme = config.get("theme") or "system"
                skin = config.get("skin") or "classic"
                if theme not in ("light", "dark", "system"):
                    theme = "system"
                if skin not in ("classic", "glass"):
                    skin = "classic"
            except Exception:
                theme, skin = "system", "classic"
            return (
                "<script>window.__BOOT_THEME__=" + json.dumps(theme)
                + ";window.__BOOT_SKIN__=" + json.dumps(skin) + ";</script>"
            )

        @app.get("/{full_path:path}")
        def spa(full_path: str):  # noqa: ANN202
            if full_path.startswith("api/"):
                return JSONResponse(status_code=404, content={"error": {"code": "not_found", "message": "接口不存在"}})
            candidate = (static / full_path).resolve()
            # is_relative_to 才是真正的目录包含判断；字符串 startswith 会放过 static2/ 这类同前缀目录
            if full_path and candidate.is_file() and candidate.is_relative_to(static.resolve()):
                return FileResponse(candidate)
            # SPA 入口：按后端 skin 设置选 classic 或 glass 那份 index.html，
            # 并注入 __BOOT_THEME__ / __BOOT_SKIN__ 供首屏防闪
            try:
                skin = repo.get_settings().get("skin") or "classic"
            except Exception:
                skin = "classic"
            entry = static / "glass" / "index.html" if skin == "glass" else static / "index.html"
            if not entry.exists():
                entry = static / "index.html"
            html = entry.read_text(encoding="utf-8")
            return HTMLResponse(html.replace("<head>", "<head>" + _boot_script(), 1))
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
        # 老版本把域名直接写在换班组上，启动时接进绑定表（只接还没绑过的）
        try:
            config = repo.get_settings()
            dns.sync_registry(config)
            dns.adopt_legacy(config)
        except Exception:  # noqa: BLE001
            log.exception("DNS 绑定初始化失败，等待下次对账重试")
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

    # 直接传 app 对象，避免 uvicorn 再 import 一次 app.main（那样会重复建 app、重复初始化）
    uvicorn.run(
        app,
        host=settings.LISTEN_HOST,
        port=settings.LISTEN_PORT,
        log_level=os.environ.get("CDT_LOG_LEVEL", "info").lower(),
        access_log=False,
    )


if __name__ == "__main__":
    run()
