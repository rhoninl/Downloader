import os
from dataclasses import dataclass


def _get_env(name: str) -> str:
    v = os.getenv(name)
    if v is None or v == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


def _get_int(name: str) -> int:
    v = _get_env(name)
    try:
        return int(v)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be an integer, got: {v}")


def _get_float(name: str) -> float:
    v = _get_env(name)
    try:
        return float(v)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be a float, got: {v}")


@dataclass
class Config:
    http_host: str
    http_port: int
    camera_device: str
    cap_width: int
    cap_height: int
    cap_fps: int
    jpeg_quality: int
    connect_timeout_sec: float
    read_timeout_sec: float
    reconnect_base_delay_ms: int
    reconnect_max_delay_ms: int
    stream_start_timeout_sec: float
    log_level: str


def load_config() -> Config:
    return Config(
        http_host=_get_env("HTTP_HOST"),
        http_port=_get_int("HTTP_PORT"),
        camera_device=_get_env("CAMERA_DEVICE"),
        cap_width=_get_int("CAP_WIDTH"),
        cap_height=_get_int("CAP_HEIGHT"),
        cap_fps=_get_int("CAP_FPS"),
        jpeg_quality=_get_int("JPEG_QUALITY"),
        connect_timeout_sec=_get_float("CONNECT_TIMEOUT_SEC"),
        read_timeout_sec=_get_float("READ_TIMEOUT_SEC"),
        reconnect_base_delay_ms=_get_int("RECONNECT_BASE_DELAY_MS"),
        reconnect_max_delay_ms=_get_int("RECONNECT_MAX_DELAY_MS"),
        stream_start_timeout_sec=_get_float("STREAM_START_TIMEOUT_SEC"),
        log_level=_get_env("LOG_LEVEL"),
    )
