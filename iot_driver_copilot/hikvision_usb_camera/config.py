# config.py
import os
import sys
from typing import Optional


def _require_env(name: str) -> str:
    v = os.environ.get(name)
    if v is None or v == "":
        print(f"[config] Missing required environment variable: {name}", file=sys.stderr)
        sys.exit(1)
    return v


def _get_int(name: str, default: Optional[int] = None) -> Optional[int]:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        print(f"[config] Invalid integer for {name}: {v}", file=sys.stderr)
        sys.exit(1)


def _get_float(name: str, default: Optional[float] = None) -> Optional[float]:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        print(f"[config] Invalid float for {name}: {v}", file=sys.stderr)
        sys.exit(1)


def _get_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        # Required runtime HTTP settings
        self.http_host: str = _require_env("HTTP_HOST")
        self.http_port: int = _get_int("HTTP_PORT") or 0
        if self.http_port <= 0 or self.http_port > 65535:
            print("[config] HTTP_PORT must be 1-65535", file=sys.stderr)
            sys.exit(1)

        # Required camera device identifier (index or path)
        self.camera_device: str = _require_env("CAM_DEVICE")

        # Optional camera settings
        self.frame_width: Optional[int] = _get_int("FRAME_WIDTH")
        self.frame_height: Optional[int] = _get_int("FRAME_HEIGHT")
        self.frame_rate: Optional[float] = _get_float("FRAME_RATE")
        self.jpeg_quality: Optional[int] = _get_int("JPEG_QUALITY")
        if self.jpeg_quality is not None and not (1 <= self.jpeg_quality <= 100):
            print("[config] JPEG_QUALITY must be 1-100", file=sys.stderr)
            sys.exit(1)

        # Optional ingest behavior
        self.auto_connect: bool = _get_bool("AUTO_CONNECT", default=False)
        self.reconnect_min_ms: int = _get_int("RECONNECT_MIN_MS", 500)  # minimum backoff (ms)
        self.reconnect_max_ms: int = _get_int("RECONNECT_MAX_MS", 5000)  # maximum backoff (ms)
        self.read_timeout_ms: int = _get_int("READ_TIMEOUT_MS", 2000)  # if no frame arrives in this time, reconnect
        self.stream_fps_limit: Optional[float] = _get_float("STREAM_FPS_LIMIT")  # limit client stream rate if set

        # Sanity checks
        if self.reconnect_min_ms <= 0 or self.reconnect_max_ms < self.reconnect_min_ms:
            print("[config] invalid reconnect backoff values", file=sys.stderr)
            sys.exit(1)
        if self.read_timeout_ms <= 0:
            print("[config] READ_TIMEOUT_MS must be > 0", file=sys.stderr)
            sys.exit(1)


def load_config() -> Config:
    return Config()
