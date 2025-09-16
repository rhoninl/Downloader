import os

class ConfigError(Exception):
    pass


def _get_env(name: str, required: bool = True) -> str:
    v = os.getenv(name)
    if required and (v is None or v == ""):
        raise ConfigError(f"Missing required environment variable: {name}")
    return v


def _get_float(name: str, required: bool = True) -> float:
    v = _get_env(name, required)
    if v is None:
        return None  # type: ignore
    try:
        return float(v)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be a number, got: {v}")


def _get_int(name: str, required: bool = True) -> int:
    v = _get_env(name, required)
    if v is None:
        return None  # type: ignore
    try:
        return int(v)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be an integer, got: {v}")


class Config:
    def __init__(self) -> None:
        # HTTP server configuration
        self.http_host: str = _get_env("HTTP_HOST")
        self.http_port: int = _get_int("HTTP_PORT")

        # Device configuration
        # Example: http://192.168.2.97
        self.device_base_url: str = _get_env("DEVICE_BASE_URL")

        # Optional credentials (if provided, background login thread will manage token)
        self.device_username: str | None = os.getenv("DEVICE_USERNAME")
        self.device_password: str | None = os.getenv("DEVICE_PASSWORD")

        # Networking and resilience configuration
        self.request_timeout: float = _get_float("REQUEST_TIMEOUT")
        self.backoff_initial: float = _get_float("BACKOFF_INITIAL")
        self.backoff_max: float = _get_float("BACKOFF_MAX")
        self.backoff_factor: float = _get_float("BACKOFF_FACTOR")
        self.login_refresh_interval: float = _get_float("LOGIN_REFRESH_INTERVAL")

        # Sanity checks
        if not (self.device_base_url.startswith("http://") or self.device_base_url.startswith("https://")):
            raise ConfigError("DEVICE_BASE_URL must start with http:// or https://")

        if (self.device_username is None) != (self.device_password is None):
            raise ConfigError("Both DEVICE_USERNAME and DEVICE_PASSWORD must be set together or both unset.")


def load_config() -> Config:
    return Config()
