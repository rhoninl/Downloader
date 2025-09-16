import os
import logging
from dataclasses import dataclass


def _get_env(name: str, default: str | None = None, required: bool = False) -> str | None:
    val = os.environ.get(name, default)
    if required and (val is None or str(val).strip() == ""):
        raise ValueError(f"Missing required environment variable: {name}")
    return val


def _to_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Invalid integer for {name}: {raw}")


def _to_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Invalid float for {name}: {raw}")


def _to_loglevel(name: str, default: str = "INFO") -> int:
    raw = os.environ.get(name, default).upper()
    levels = {
        "CRITICAL": logging.CRITICAL,
        "ERROR": logging.ERROR,
        "WARNING": logging.WARNING,
        "INFO": logging.INFO,
        "DEBUG": logging.DEBUG,
        "NOTSET": logging.NOTSET,
    }
    return levels.get(raw, logging.INFO)


@dataclass(frozen=True)
class Config:
    http_host: str
    http_port: int
    device_base_url: str
    device_username: str | None
    device_password_md5: str | None
    device_timeout_seconds: float
    device_retry_max: int
    device_retry_backoff_initial: float
    device_retry_backoff_max: float
    login_refresh_seconds: int
    log_level: int


def load_config() -> Config:
    http_host = _get_env("HTTP_HOST", "0.0.0.0") or "0.0.0.0"
    http_port = _to_int("HTTP_PORT", 8080)

    device_base_url = _get_env("DEVICE_BASE_URL", required=True)  # e.g., http://192.168.2.97
    if device_base_url.endswith('/'):
        device_base_url = device_base_url[:-1]

    device_username = _get_env("DEVICE_USERNAME")
    device_password_md5 = _get_env("DEVICE_PASSWORD_MD5")

    device_timeout_seconds = _to_float("DEVICE_TIMEOUT_SECONDS", 5.0)
    device_retry_max = _to_int("DEVICE_RETRY_MAX", 3)
    device_retry_backoff_initial = _to_float("DEVICE_RETRY_BACKOFF_INITIAL", 0.5)
    device_retry_backoff_max = _to_float("DEVICE_RETRY_BACKOFF_MAX", 8.0)

    login_refresh_seconds = _to_int("LOGIN_REFRESH_SECONDS", 600)

    log_level = _to_loglevel("LOG_LEVEL", "INFO")

    return Config(
        http_host=http_host,
        http_port=http_port,
        device_base_url=device_base_url,
        device_username=device_username,
        device_password_md5=device_password_md5,
        device_timeout_seconds=device_timeout_seconds,
        device_retry_max=device_retry_max,
        device_retry_backoff_initial=device_retry_backoff_initial,
        device_retry_backoff_max=device_retry_backoff_max,
        login_refresh_seconds=login_refresh_seconds,
        log_level=log_level,
    )
