"""Alembic 运行环境（P18）。

## 两个关键选择

1. **URL 从 app 配置读，不写进 alembic.ini**
   `alembic.ini` 是入库文件，把带密码的 `mysql+pymysql://root:...` 写进去等于把
   数据库口令提交到仓库。这里统一从 `app.core.config.settings.mysql_url` 取，
   与运行时用的是同一个来源（也就不会再出现"迁移连 A 库、服务连 B 库"）。
   所以 `alembic.ini` 里的 `sqlalchemy.url` 留空。

2. **`prepend_sys_path = %(here)s`**（见 alembic.ini）
   让 `import app` 与当前工作目录无关：从仓库根执行
   `alembic -c backend/alembic.ini upgrade head` 也能跑通。

## 为什么是同步 engine

langgraph 用的 aiosqlite 是异步的，但**业务库（MySQL）这一侧全程是同步的**
（`app/db/session.py` 用 `create_engine`）。迁移跟着业务库走，用同步 driver，
不需要 asyncio，报错栈也短得多。

⚠️ 但**离线模式（`--sql`）不支持**，原因见 `run_migrations_offline` 的说明。

## ⚠️ 不要用 autogenerate 生成迁移

老部署的库里**已经存在** `sessions` / `tool_call_logs` / `audit_logs` 三张表，
但没有任何迁移历史。autogenerate 会把这三张表当成"该新建"而生成重复的
CREATE TABLE，直接撞表。所以本项目的迁移脚本一律**手写 + 幂等判断**
（`versions/0001_baseline.py` 里有详细说明）。`target_metadata` 只作为
`alembic check` 之类的比对基准保留。
"""
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# backend/ 目录（本文件位于 backend/alembic/env.py）
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402
import app.db.models  # noqa: E402,F401  确保所有模型都注册进 Base.metadata

config = context.config

# `configure_logger` 由调用方决定：应用启动时是进程内调用（app/main.py 的
# _run_migrations 会把它设为 False），此时**不能**执行 fileConfig ——
# alembic.ini 的 [loggers] 段会把 root logger 改成 WARN，把 uvicorn 与应用的
# INFO 日志一起吞掉。命令行执行时不传这个标记，保持 alembic 的默认行为。
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

# 连接串统一来自应用配置（见模块头注释）
config.set_main_option("sqlalchemy.url", settings.mysql_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式（`alembic upgrade head --sql`）：**本项目不支持，直接拒绝**。

    为什么不做：0001/0002 都要先看数据库现状再决定做什么
    （`sa.inspect(bind).get_table_names()` 判断表/列/索引是否存在）。
    离线模式没有连接，`op.get_bind()` 只是个 mock，拿不到任何现状 ——
    要么生成错误的 SQL，要么抛一句看不懂的 AttributeError。

    与其让人在"以为能预览 SQL"的地方踩坑，不如现在就说清楚。
    想先看会执行什么：`alembic upgrade head` 会在执行每一步时打印
    `[0001] / [0002] ...` 的说明（配合 mysqldump 备份即可安全预览）。
    """
    raise RuntimeError(
        "本项目的迁移依赖数据库现状判断（幂等守卫），不支持离线 --sql 模式。"
        "请直接执行 `alembic upgrade head`（先备份：mysqldump agent_harness > backup.sql）。"
    )


def run_migrations_online() -> None:
    """在线模式：连上 MySQL 执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
