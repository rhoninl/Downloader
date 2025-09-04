# config.py
import os
from dataclasses import dataclass


def _getenv(name: str, default: str):
    return os.environ.get(name, default)


def _getenv_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _getenv_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    # HTTP server configuration
    http_host: str = _getenv("HTTP_HOST", "0.0.0.0")
    http_port: int = _getenv_int("HTTP_PORT", 8080)

    # Device connectivity
    device_host: str = _getenv("DEVICE_HOST", "127.0.0.1")
    device_ctrl_port: int = _getenv_int("DEVICE_CTRL_PORT", 9000)
    device_tel_port: int = _getenv_int("DEVICE_TEL_PORT", 9001)

    # Timeouts and retry/backoff
    connect_timeout: float = _getenv_float("CONNECT_TIMEOUT", 5.0)
    read_timeout: float = _getenv_float("READ_TIMEOUT", 5.0)
    reconnect_delay_min: float = _getenv_float("RECONNECT_DELAY_MIN", 0.5)
    reconnect_delay_max: float = _getenv_float("RECONNECT_DELAY_MAX", 5.0)

    # Movement defaults
    linear_velocity_default: float = _getenv_float("LINEAR_VELOCITY_DEFAULT", 0.2)

    # Logging
    log_level: str = _getenv("LOG_LEVEL", "INFO").upper()
