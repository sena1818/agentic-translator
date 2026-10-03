"""
FastAPI 应用入口
"""

import asyncio
import logging
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from ..utils.config_loader import get_config
from .database.db import Database
from .routes import files, glossary, translation
from .worker import TaskWorker

# 配置日志
Path("logs").mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("logs/translation.log", encoding="utf-8")],
)


@asynccontextmanager
async def lifespan(_app):
    await startup_event()
    try:
        yield
    finally:
        await shutdown_event()


# 创建应用
app = FastAPI(
    title="AI 翻译系统 API",
    description="面向多领域专业文本的智能翻译系统，支持自定义术语表",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ValueError)
async def invalid_request(_request, exc):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(FileExistsError)
async def duplicate_resource(_request, _exc):
    return JSONResponse(status_code=409, content={"detail": "同名术语表已存在，请修改名称"})


# CORS 配置 (允许前端调用)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("TRANSLATION_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(
        ","
    ),  # 通过环境变量添加明确允许的开发来源
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(translation.router)
app.include_router(glossary.router)
app.include_router(files.router)


# 启动事件
async def startup_event():
    """初始化数据库"""
    config = get_config()
    port = int(os.getenv("TRANSLATION_SERVER_PORT", str(config.server_port)))
    db = Database()
    await db.initialize()
    print("✅ 数据库初始化完成")

    inline_worker_enabled = (
        os.getenv(
            "TRANSLATION_INLINE_WORKER",
            "1" if config.inline_worker_enabled else "0",
        )
        != "0"
    )

    if inline_worker_enabled:
        app.state.worker = TaskWorker()
        app.state.worker_task = asyncio.create_task(app.state.worker.run_forever())
        print("🎯 已启动内联任务 worker")
    else:
        app.state.worker = None
        app.state.worker_task = None
        print("⏸️  内联 worker 已禁用，请单独运行 run_worker.py")

    print("✅ FastAPI 服务器启动成功")
    print(f"📖 API 文档: http://localhost:{port}/docs")


async def shutdown_event():
    """优雅关闭内联 worker"""
    worker = getattr(app.state, "worker", None)
    worker_task = getattr(app.state, "worker_task", None)
    if worker:
        await worker.stop()
    if worker_task:
        await worker_task


# 健康检查
@app.get("/api/health")
async def health_check():
    return {"status": "ok", "message": "翻译系统运行中"}


@app.get("/api/system")
async def system_status():
    db = Database()
    return {
        "workers_online": await db.online_workers(),
        "formats": ["md"]
        + (["epub"] if shutil.which("pandoc") else [])
        + (["pdf"] if shutil.which("marker_single") else []),
        "max_upload_bytes": 50 * 1024 * 1024,
    }


# 静态文件服务 (优先使用 Vite 最新构建产物)
frontend_build_dir = None
for candidate in [Path("frontend/dist"), Path("frontend_dist")]:
    if candidate.exists():
        frontend_build_dir = candidate
        break

if frontend_build_dir:
    app.mount("/", StaticFiles(directory=str(frontend_build_dir), html=True), name="frontend")
