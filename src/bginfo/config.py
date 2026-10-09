"""Конфигурация: секреты из env/.env (Settings) и константы из config.ini (AppConfig)."""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Секреты и параметры окружения. Значения никогда не логируются."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                              env_ignore_empty=True)

    TELEGRAM_BOT_TOKEN: SecretStr | None = None
    OPENROUTER_API_KEY: SecretStr | None = None
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: SecretStr | None = None
    ADMIN_SESSION_SECRET: SecretStr | None = None
    CONSULT_CHAT_ID: int | None = None
    DB_PATH: str = "data/app.db"
    APP_MODE: Literal["production", "demo", "test"] = "production"
    ADMIN_COOKIE_SECURE: bool = True
    # Переопределение host админки (в Docker задаётся в compose); пусто -> [admin] host
    ADMIN_HOST: str | None = None


@dataclass
class AiConfig:
    jev_model: str = "~typesafe/jev-latest"
    llm_model: str = "openai/gpt-4.1-mini"
    base_url: str = "https://openrouter.ai/api/v1"
    timeout_s: float = 20.0
    retries: int = 2
    explain_max_tokens: int = 900
    jev_recognize_threshold: float = 0.3
    jev_negation_threshold: float = 0.75
    app_title: str = "BGINFO bot"
    http_referer: str = "https://bginfo.eu"


@dataclass
class LimitsConfig:
    user_text_per_day: int = 30
    user_explain_per_day: int = 10
    daily_budget_usd: float = 2.0
    unknown_cost_usd: float = 0.01


@dataclass
class PrivacyConfig:
    retention_days: int = 90
    backup_retention_days: int = 14


@dataclass
class BotConfig:
    consult_contact: str = ""
    timezone: str = "Europe/Sofia"


@dataclass
class AdminConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    login_max_attempts: int = 5
    login_window_min: int = 15
    session_ttl_hours: int = 12
    # Учитывать X-Forwarded-For (только если админка стоит за доверенным reverse proxy)
    trust_proxy: bool = False


@dataclass
class AppConfig:
    ai: AiConfig = field(default_factory=AiConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)


def _apply_section(obj, parser: configparser.ConfigParser, section: str) -> None:
    """Перезаписать поля dataclass значениями из секции ini, приводя к типу дефолта."""
    if not parser.has_section(section):
        return
    for name, default in list(vars(obj).items()):
        if not parser.has_option(section, name):
            continue
        raw = parser.get(section, name).strip()
        try:
            if isinstance(default, bool):
                value = raw.lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                value = int(raw)
            elif isinstance(default, float):
                value = float(raw)
            else:
                value = raw
        except ValueError:
            log.warning("config.ini: bad value for [%s] %s, default kept", section, name)
            continue
        setattr(obj, name, value)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Прочитать config.ini (путь: аргумент, env CONFIG_INI или ./config.ini); нет файла -> дефолты."""
    ini = Path(path) if path else Path(os.environ.get("CONFIG_INI") or "config.ini")
    cfg = AppConfig()
    if not ini.is_file():
        log.info("config.ini not found at %s, defaults used", ini)
        return cfg
    parser = configparser.ConfigParser(inline_comment_prefixes=None)
    parser.read(ini, encoding="utf-8")
    for section in ("ai", "limits", "privacy", "bot", "admin"):
        _apply_section(getattr(cfg, section), parser, section)
    return cfg


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return load_config()


def reset_config_cache() -> None:
    """Сбросить кэши (для тестов)."""
    get_settings.cache_clear()
    get_config.cache_clear()
