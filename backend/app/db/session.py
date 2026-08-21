"""数据库引擎与会话管理（P2 起真正使用；create_engine 为惰性连接，未启动 PG 也不会报错）。"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_db():
    """FastAPI 依赖：每请求一个会话，请求结束关闭。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
