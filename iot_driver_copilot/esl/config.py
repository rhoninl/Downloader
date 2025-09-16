import os

def _get_env(name, default=None, required=False, cast=str):
    val = os.getenv(name, default)
    if required and (val is None or (isinstance(val, str) and val.strip() == "")):
        raise RuntimeError(f"Missing required environment variable: {name}")
    if val is None:
        return None
    try:
        return cast(val)
    except Exception as e:
        raise RuntimeError(f"Invalid value for {name}: {val} ({e})")


class Config:
    # HTTP server
    HTTP_HOST = _get_env("HTTP_HOST", default="0.0.0.0")
    HTTP_PORT = _get_env("HTTP_PORT", default="8080", cast=int)

    # Client authentication token for our driver endpoints
    AUTH_TOKEN = _get_env("AUTH_TOKEN", required=True)

    # Device connection
    DEVICE_BASE_URL = _get_env("DEVICE_BASE_URL", required=True)
    DEVICE_USERNAME = _get_env("DEVICE_USERNAME", required=True)
    DEVICE_PASSWORD_MD5 = _get_env("DEVICE_PASSWORD_MD5", required=True)

    # Device request tuning
    DEVICE_TIMEOUT_SEC = _get_env("DEVICE_TIMEOUT_SEC", default="5", cast=float)
    DEVICE_RETRY_MAX = _get_env("DEVICE_RETRY_MAX", default="5", cast=int)
    DEVICE_BACKOFF_INITIAL_MS = _get_env("DEVICE_BACKOFF_INITIAL_MS", default="500", cast=int)
    DEVICE_BACKOFF_MAX_MS = _get_env("DEVICE_BACKOFF_MAX_MS", default="8000", cast=int)

    # Background login refresh
    DEVICE_LOGIN_REFRESH_SEC = _get_env("DEVICE_LOGIN_REFRESH_SEC", default="300", cast=int)
