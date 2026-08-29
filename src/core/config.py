"""Configuration management"""

from pathlib import Path
from typing import Any
import yaml
from pydantic import BaseModel, Field


class DatabaseConfig(BaseModel):
    """数据库配置"""

    path: str = Field(default="data/literature.db")


class CrossrefConfig(BaseModel):
    """Crossref API 配置"""

    email: str = Field(description="用于 Polite Pool 的邮箱")
    rate_limit: float = Field(default=10.0, description="每秒请求数")
    timeout: float = Field(default=30.0, description="请求超时（秒）")


class LLMConfig(BaseModel):
    """LLM API 配置"""

    provider: str = Field(default="openai", description="openai, anthropic, etc.")
    api_key: str = Field(description="API Key")
    model_cheap: str = Field(default="gpt-4o-mini", description="L1 等简单任务")
    model_balanced: str = Field(default="gpt-4o", description="L2 等中等任务")
    model_strong: str = Field(default="gpt-4o", description="L3, Archive Builder 等复杂任务")


class MonitorConfig(BaseModel):
    """日常监控配置"""

    enabled: bool = Field(default=True)
    check_interval_hours: int = Field(default=24)
    sources: list[str] = Field(default_factory=list, description="监控源：pubmed, xmol, etc.")


class Config(BaseModel):
    """全局配置"""

    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    crossref: CrossrefConfig
    llm: LLMConfig
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)


def load_config(config_path: str | Path = "config.yaml") -> Config:
    """
    加载配置文件

    Args:
        config_path: 配置文件路径

    Returns:
        Config 对象
    """
    path = Path(config_path)

    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    return Config(**data)


def create_default_config(output_path: str | Path = "config.yaml") -> None:
    """
    创建默认配置文件模板

    Args:
        output_path: 输出路径
    """
    default_config = {
        "database": {
            "path": "data/literature.db",
        },
        "crossref": {
            "email": "your.email@example.com",
            "rate_limit": 10.0,
            "timeout": 30.0,
        },
        "llm": {
            "provider": "openai",
            "api_key": "sk-...",
            "model_cheap": "gpt-4o-mini",
            "model_balanced": "gpt-4o",
            "model_strong": "gpt-4o",
        },
        "monitor": {
            "enabled": True,
            "check_interval_hours": 24,
            "sources": ["crossref", "pubmed"],
        },
    }

    path = Path(output_path)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(default_config, f, default_flow_style=False, allow_unicode=True)

    print(f"Default config created: {path}")
    print("Please edit the config file with your settings.")
