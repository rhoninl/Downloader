# config.py
# Configuration loader for the robot driver. All values taken from environment variables.

import os
from dataclasses import dataclass
from typing import Optional


def _get_env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.getenv(name)
    if val is None:
        return default
    val = val.strip()
    return val if val != "" else default


def _get_env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _get_env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


@dataclass
class DriverConfig:
    http_host: str
    http_port: int
    device_base_url: str
    device_username: Optional[str]
    device_password: Optional[str]
    request_timeout: float
    max_retries: int
    retry_backoff_initial: float
    retry_backoff_max: float
    ping_interval: float
    shutdown_grace: float
    stream_heartbeat_interval: float


def load_config() -> DriverConfig:
    return DriverConfig(
        http_host=_get_env_str("HTTP_HOST", "0.0.0.0"),
        http_port=_get_env_int("HTTP_PORT", 8080),
        device_base_url=_get_env_str("DEVICE_BASE_URL", ""),
        device_username=_get_env_str("DEVICE_USERNAME", None),
        device_password=_get_env_str("DEVICE_PASSWORD", None),
        request_timeout=_get_env_float("REQUEST_TIMEOUT", 5.0),
        max_retries=_get_env_int("MAX_RETRIES", 3),
        retry_backoff_initial=_get_env_float("RETRY_BACKOFF_INITIAL", 0.5),
        retry_backoff_max=_get_env_float("RETRY_BACKOFF_MAX", 5.0),
        ping_interval=_get_env_float("PING_INTERVAL", 5.0),
        shutdown_grace=_get_env_float("SHUTDOWN_GRACE", 3.0),
        stream_heartbeat_interval=_get_env_float("STREAM_HEARTBEAT_INTERVAL", 15.0),
    )
