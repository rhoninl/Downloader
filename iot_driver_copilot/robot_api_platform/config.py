import os
from dataclasses import dataclass


def _get_env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip()


def _get_float(name: str, default: float) -> float:
    v = _get_env(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    v = _get_env(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


@dataclass
class Config:
    http_host: str
    http_port: int
    device_poll_url: str
    poll_interval_sec: float
    http_timeout_sec: float
    backoff_initial_sec: float
    backoff_max_sec: float
    log_level: str
    auth_bearer_token: str | None
    basic_auth_user: str | None
    basic_auth_pass: str | None


def load_config() -> Config:
    device_poll_url = _get_env("DEVICE_POLL_URL")
    if not device_poll_url:
        raise SystemExit("DEVICE_POLL_URL is required")

    return Config(
        http_host=_get_env("HTTP_HOST", "0.0.0.0"),
        http_port=_get_int("HTTP_PORT", 8000),
        device_poll_url=device_poll_url,
        poll_interval_sec=_get_float("POLL_INTERVAL_SEC", 0.5),
        http_timeout_sec=_get_float("DEVICE_HTTP_TIMEOUT_SEC", 5.0),
        backoff_initial_sec=_get_float("RETRY_BACKOFF_INITIAL_SEC", 1.0),
        backoff_max_sec=_get_float("RETRY_BACKOFF_MAX_SEC", 30.0),
        log_level=_get_env("LOG_LEVEL", "INFO"),
        auth_bearer_token=_get_env("AUTH_BEARER_TOKEN"),
        basic_auth_user=_get_env("BASIC_AUTH_USER"),
        basic_auth_pass=_get_env("BASIC_AUTH_PASS"),
    )
