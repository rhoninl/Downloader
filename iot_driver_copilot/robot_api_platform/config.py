# config.py
import os
from dataclasses import dataclass


def _get_env(name: str, default=None):
    val = os.getenv(name)
    return val if val is not None else default


def _get_env_int(name: str, default: int) -> int:
    val = _get_env(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _get_env_float(name: str, default: float) -> float:
    val = _get_env(name)
    if val is None:
        return default
    try:
        return float(val)
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
    reconnect_initial_delay_sec: float
    reconnect_max_delay_sec: float

    default_linear_velocity: float
    sse_keepalive_interval_sec: float

    @staticmethod
    def load_from_env() -> "Config":
        return Config(
            http_host=_get_env("HTTP_HOST", "0.0.0.0"),
            http_port=_get_env_int("HTTP_PORT", 8000),
            device_host=_get_env("DEVICE_HOST", "127.0.0.1"),
            device_port=_get_env_int("DEVICE_PORT", 9000),
            connect_timeout_sec=_get_env_float("DEVICE_CONNECT_TIMEOUT_SEC", 3.0),
            read_timeout_sec=_get_env_float("DEVICE_READ_TIMEOUT_SEC", 5.0),
            reconnect_initial_delay_sec=_get_env_float("DEVICE_RECONNECT_INITIAL_DELAY_SEC", 1.0),
            reconnect_max_delay_sec=_get_env_float("DEVICE_RECONNECT_MAX_DELAY_SEC", 30.0),
            default_linear_velocity=_get_env_float("DEFAULT_LINEAR_VELOCITY", 0.5),
            sse_keepalive_interval_sec=_get_env_float("SSE_KEEPALIVE_INTERVAL_SEC", 15.0),
        )
