"""SQLAlchemy ORM 基类（MySQL 业务库 agent_harness）。"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """所有业务 ORM 模型的基类。

    MySQL 中建表须声明 utf8mb4（在连接 URL 的 charset 里已指定）。
    """
