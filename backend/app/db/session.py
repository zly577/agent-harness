"""MySQL 业务库：引擎与会话管理（P2 启用，持久化会话元信息等业务数据）。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

# 业务数据：MySQL
engine = create_engine(settings.mysql_url, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """FastAPI 依赖：每请求一个业务库会话，请求结束关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
