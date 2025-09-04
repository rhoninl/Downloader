# config.py
import os
from dataclasses import dataclass


def _get_env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None and v != "" else default


def _get_env_int(name: str, default: int) -> int:
    try:
        return int(_get_env(name, str(default)))
    except ValueError:
        return default


def _get_env_float(name: str, default: float) -> float:
    try:
        return float(_get_env(name, str(default)))
    except ValueError:
        return default


@dataclass
class Config:
    http_host: str
    http_port: int
    device_host: str
    device_port: int
    connect_timeout_sec: float
    read_timeout_sec: float
    reconnect_backoff_sec: float
    max_backoff_sec: float
    default_linear_velocity: float


def load_config() -> Config:
    return Config(
        http_host=_get_env("HTTP_HOST", "0.0.0.0"),
        http_port=_get_env_int("HTTP_PORT", 8000),
        device_host=_get_env("DEVICE_HOST", "127.0.0.1"),
        device_port=_get_env_int("DEVICE_PORT", 9000),
        connect_timeout_sec=_get_env_float("CONNECT_TIMEOUT_SEC", 5.0),
        read_timeout_sec=_get_env_float("READ_TIMEOUT_SEC", 5.0),
        reconnect_backoff_sec=_get_env_float("RECONNECT_BACKOFF_SEC", 1.0),
        max_backoff_sec=_get_env_float("MAX_BACKOFF_SEC", 30.0),
        default_linear_velocity=_get_env_float("DEFAULT_LINEAR_VELOCITY", 0.2),
    )
