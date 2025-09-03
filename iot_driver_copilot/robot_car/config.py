# config.py
import os
import sys
from dataclasses import dataclass


def _getenv_required(name: str) -> str:
    val = os.environ.get(name)
    if val is None or val == "":
        print(f"Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(2)
    return val


def _getenv_float(name: str) -> float:
    s = _getenv_required(name)
    try:
        return float(s)
    except ValueError:
        print(f"Invalid float for {name}: {s}", file=sys.stderr)
        sys.exit(2)


def _getenv_int(name: str) -> int:
    s = _getenv_required(name)
    try:
        return int(s)
    except ValueError:
        print(f"Invalid int for {name}: {s}", file=sys.stderr)
        sys.exit(2)


@dataclass(frozen=True)
class Config:
    http_host: str
    http_port: int

    device_base_url: str
    device_timeout_s: float
    device_poll_interval_s: float
    device_retry_backoff_s: float
    device_retry_limit: int

    device_username: str | None
    device_password: str | None


def load_config() -> Config:
    http_host = _getenv_required("DRIVER_HTTP_HOST")
    http_port = _getenv_int("DRIVER_HTTP_PORT")

    device_base_url = _getenv_required("DEVICE_BASE_URL").rstrip("/")
    device_timeout_s = _getenv_float("DEVICE_TIMEOUT_S")
    device_poll_interval_s = _getenv_float("DEVICE_POLL_INTERVAL_S")
    device_retry_backoff_s = _getenv_float("DEVICE_RETRY_BACKOFF_S")
    device_retry_limit = _getenv_int("DEVICE_RETRY_LIMIT")

    device_username = os.environ.get("DEVICE_USERNAME") or None
    device_password = os.environ.get("DEVICE_PASSWORD") or None

    return Config(
        http_host=http_host,
        http_port=http_port,
        device_base_url=device_base_url,
        device_timeout_s=device_timeout_s,
        device_poll_interval_s=device_poll_interval_s,
        device_retry_backoff_s=device_retry_backoff_s,
        device_retry_limit=device_retry_limit,
        device_username=device_username,
        device_password=device_password,
    )
