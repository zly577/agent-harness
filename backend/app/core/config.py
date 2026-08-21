"""全局配置：从 .env / 环境变量读取（pydantic-settings）。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # 数据库
    database_url: str = (
        "postgresql+psycopg2://postgres:postgres@localhost:5432/agent_harness"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
