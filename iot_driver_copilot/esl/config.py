import os
import ssl
import logging
from dataclasses import dataclass


def _get_env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and (val is None or val == ""):
        raise EnvironmentError(f"Missing required environment variable: {name}")
    return val if val is not None else ""


def _get_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except ValueError:
        raise EnvironmentError(f"Invalid integer for {name}: {raw}")
    if min_value is not None and v < min_value:
        v = min_value
    if max_value is not None and v > max_value:
        v = max_value
    return v


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    raw = raw.strip().lower()
    return raw in ("1", "true", "t", "yes", "y", "on")


@dataclass
class Config:
    http_host: str
    http_port: int

    device_base_url: str
    device_username: str
    device_password_md5: str

    request_timeout_sec: int
    retry_max: int
    retry_base_delay_ms: int
    retry_max_delay_ms: int

    token_refresh_interval_sec: int
    token_wait_timeout_sec: int

    device_tls_verify: bool
    log_level: int

    def ssl_context(self):
        # For HTTPS connections respecting verify flag
        if self.device_base_url.lower().startswith("https") and not self.device_tls_verify:
            return ssl._create_unverified_context()
        return None


LOG_LEVELS = {
    "CRITICAL": logging.CRITICAL,
    "ERROR": logging.ERROR,
    "WARNING": logging.WARNING,
    "INFO": logging.INFO,
    "DEBUG": logging.DEBUG,
}


def load_config() -> Config:
    http_host = _get_env("HTTP_HOST", "0.0.0.0")
    http_port = _get_int("HTTP_PORT", 8000, 1, 65535)

    device_base_url = _get_env("DEVICE_BASE_URL", required=True).rstrip("/")
    device_username = _get_env("DEVICE_USERNAME", required=True)
    device_password_md5 = _get_env("DEVICE_PASSWORD_MD5", required=True)

    request_timeout_sec = _get_int("REQUEST_TIMEOUT", 5, 1, 300)
    retry_max = _get_int("RETRY_MAX", 5, 0, 100)
    retry_base_delay_ms = _get_int("RETRY_BASE_DELAY_MS", 500, 10, 60000)
    retry_max_delay_ms = _get_int("RETRY_MAX_DELAY_MS", 8000, 100, 600000)

    token_refresh_interval_sec = _get_int("TOKEN_REFRESH_INTERVAL_SEC", 300, 10, 86400)
    token_wait_timeout_sec = _get_int("REQUEST_TOKEN_WAIT_SEC", 5, 0, 60)

    device_tls_verify = _get_bool("DEVICE_TLS_VERIFY", True)
    ll = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = LOG_LEVELS.get(ll, logging.INFO)

    return Config(
        http_host=http_host,
        http_port=http_port,
        device_base_url=device_base_url,
        device_username=device_username,
        device_password_md5=device_password_md5,
        request_timeout_sec=request_timeout_sec,
        retry_max=retry_max,
        retry_base_delay_ms=retry_base_delay_ms,
        retry_max_delay_ms=retry_max_delay_ms,
        token_refresh_interval_sec=token_refresh_interval_sec,
        token_wait_timeout_sec=token_wait_timeout_sec,
        device_tls_verify=device_tls_verify,
        log_level=log_level,
    )
