import os
import logging
from typing import Optional


def _get_required(name: str) -> str:
    val = os.getenv(name)
    if val is None or val == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _get_optional(name: str) -> Optional[str]:
    val = os.getenv(name)
    if val is None or val == "":
        return None
    return val


def _parse_float(name: str, required: bool) -> Optional[float]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return None
    try:
        return float(raw)
    except Exception as e:
        raise RuntimeError(f"Invalid float for {name}: {raw}") from e


def _parse_int(name: str, required: bool) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        if required:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return None
    try:
        return int(raw)
    except Exception as e:
        raise RuntimeError(f"Invalid int for {name}: {raw}") from e


class Config:
    def __init__(self) -> None:
        # HTTP server bind
        self.http_host: str = _get_required("HTTP_HOST")
        self.http_port: int = _parse_int("HTTP_PORT", required=True)  # type: ignore

        # Device base URL, e.g., http://192.168.2.97
        base = _get_required("DEVICE_BASE_URL").rstrip("/")
        self.device_base_url: str = base

        # Credentials (optional if clients supply token header)
        self.device_username: Optional[str] = _get_optional("DEVICE_USERNAME")
        self.device_password: Optional[str] = _get_optional("DEVICE_PASSWORD")
        self.device_password_md5: Optional[str] = _get_optional("DEVICE_PASSWORD_MD5")

        # Timeouts and retry/backoff
        self.request_timeout: float = _parse_float("REQUEST_TIMEOUT_SECONDS", required=True)  # type: ignore
        self.retry_base_delay: float = _parse_float("RETRY_BASE_DELAY_SECONDS", required=True)  # type: ignore
        self.retry_max_backoff: float = _parse_float("RETRY_MAX_BACKOFF_SECONDS", required=True)  # type: ignore
        self.token_refresh_interval: float = _parse_float("TOKEN_REFRESH_INTERVAL_SECONDS", required=True)  # type: ignore

        # Logging level
        lvl_str = _get_required("LOG_LEVEL").upper()
        if lvl_str not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise RuntimeError(f"Invalid LOG_LEVEL: {lvl_str}")
        self.log_level = getattr(logging, lvl_str)


config = Config()
