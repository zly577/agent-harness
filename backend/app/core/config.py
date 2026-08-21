"""全局配置：从 .env / 环境变量读取（pydantic-settings）。

P2 起新增：
- MYSQL_URL：业务数据持久化（MySQL 8.0）
- SQLITE_PATH：langgraph checkpoint/store 消息历史（SQLite 单文件）

MySQL 迁移到 PostgreSQL 时只需改 MYSQL_URL 并替换驱动，对上层无耦合。
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend 目录的绝对路径（用于默认 SQLite 位置）
_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 应用
    app_name: str = "Agent Harness"
    app_version: str = "0.1.0"
    app_env: str = "development"

    # DeepSeek / LLM
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    # ===== P2：持久化 =====
    # 业务数据（MySQL）
    mysql_url: str = "mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/agent_harness?charset=utf8mb4"
    # langgraph 消息历史（SQLite 单文件）
    sqlite_path: str = str(_BACKEND_DIR / "data" / "harness.sqlite")

    # ===== P2：上下文管理 =====
    # 触发截断/摘要的消息条数阈值（超过则处理）
    context_max_messages: int = 12
    # 摘要后保留的最近消息条数
    context_keep_messages: int = 6

    # ===== P7：MCP =====
    # 是否尝试加载外部 MCP 工具（需先装 langchain_mcp_adapters + 网络可达）
    mcp_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
