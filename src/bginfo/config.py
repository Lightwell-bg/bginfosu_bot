"""Конфигурация: секреты из env/.env (Settings) и константы из config.ini (AppConfig)."""

from __future__ import annotations

import configparser
import logging
import os
import re
import tempfile
import threading
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
    # Ссылка на бота для кнопки «Открыть бота» в админке
    bot_link: str = "https://t.me/bginfosu_bot"


@dataclass
class AdminConfig:
    host: str = "0.0.0.0"
    port: int = 8091
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


ADMIN_HOSTS = ("0.0.0.0", "127.0.0.1")
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]")
_WRITE_LOCK = threading.Lock()
NEWLINE_CRLF = "\r\n"
NEWLINE_LF = "\n"


def config_path() -> Path:
    """Путь к config.ini: CONFIG_INI или ./config.ini."""
    return Path(os.environ.get("CONFIG_INI") or "config.ini")


def update_admin_access(path: str | Path | None, host: str, port: int, trust_proxy: bool | None = None) -> None:
    """Записать host и port (и trust_proxy, если передан) в секцию [admin] config.ini.

    Комментарии и другие секции не трогаются. Чтение-изменение-запись сериализованы замком;
    результат проверяется configparser-ом до записи.
    """
    if host not in ADMIN_HOSTS:
        raise ValueError(f"host должен быть один из: {', '.join(ADMIN_HOSTS)}")
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("порт должен быть целым числом от 1024 до 65535")
    ini = Path(path) if path else config_path()
    values = {"host": host, "port": str(port)}
    if trust_proxy is not None:
        values["trust_proxy"] = "true" if trust_proxy else "false"
    with _WRITE_LOCK:
        text = ini.read_bytes().decode("utf-8") if ini.is_file() else ""
        data = _render_admin_access(text, values)
        _check_ini(text, data)
        _write_ini(ini, data)
    reset_config_cache()


def _render_admin_access(text: str, values: dict[str, str]) -> str:
    nl = NEWLINE_CRLF if NEWLINE_CRLF in text else NEWLINE_LF
    out: list[str] = []
    in_admin = seen_admin = False
    done: set[str] = set()
    key_re = re.compile(r"^\s*(" + "|".join(map(re.escape, values)) + r")\s*[=:]")

    def flush_missing() -> None:
        for k, v in values.items():
            if k not in done:
                out.append(f"{k} = {v}")
                done.add(k)

    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            if in_admin:
                flush_missing()
            in_admin = m.group(1).strip() == "admin"
            seen_admin = seen_admin or in_admin
            out.append(line)
            continue
        k = key_re.match(line) if in_admin else None
        if k and k.group(1) not in done:
            out.append(f"{k.group(1)} = {values[k.group(1)]}")
            done.add(k.group(1))
        else:
            out.append(line)
    if in_admin:
        flush_missing()
    if not seen_admin:
        out += ["", "[admin]"]
        flush_missing()
    return nl.join(out) + nl


def _check_ini(old: str, new: str) -> None:
    """Новое содержимое должно разбираться и сохранять все прежние секции; иначе ничего не пишем."""
    try:
        before = configparser.ConfigParser(inline_comment_prefixes=None)
        before.read_string(old)
        after = configparser.ConfigParser(inline_comment_prefixes=None)
        after.read_string(new)
    except configparser.Error as e:
        raise ValueError(f"config.ini после изменения не разбирается: {e}") from e
    missing = set(before.sections()) - set(after.sections())
    if missing:
        raise ValueError(f"в новом config.ini пропали секции: {', '.join(sorted(missing))}")


def _write_ini(ini: Path, data: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=ini.name + ".", suffix=".tmp", dir=ini.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, ini)
        except OSError:
            # В Docker config.ini смонтирован как файл (bind-mount): rename поверх точки монтирования
            # даёт EBUSY. Тогда пишем прямо в файл (inode сохраняется) одним write.
            with open(ini, "w", encoding="utf-8", newline="") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
    finally:
        tmp.unlink(missing_ok=True)
