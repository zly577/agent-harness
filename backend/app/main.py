"""FastAPI 应用入口（P0：项目骨架 + 健康检查；P2：启动建表）。"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_router
from app.core.config import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时自动建业务表（MySQL）。P4 前后应换为 alembic 迁移。
    from app.db.base import Base
    from app.db.session import engine
    import app.db.models  # noqa: F401  确保模型注册

    Base.metadata.create_all(bind=engine)
    logger.info("[P2] MySQL 业务表 ready")
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# 开发阶段放开跨域；上线前收紧（P9 前后端联调时再细化）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 业务路由
app.include_router(api_router)


@app.get("/")
def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
    }
