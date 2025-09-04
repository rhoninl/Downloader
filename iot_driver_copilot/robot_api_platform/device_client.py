# device_client.py
# Robot device client using the device's native HTTP API for movement commands.

from urllib import request, parse, error
import base64
import time
from typing import Optional, Tuple
from urllib.parse import urlparse, urljoin


class DeviceHTTPClient:
    def __init__(
        self,
        base_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: float = 5.0,
        max_retries: int = 3,
        backoff_initial: float = 0.5,
        backoff_max: float = 5.0,
    ) -> None:
        if not base_url:
            raise ValueError("DEVICE_BASE_URL is required")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max

        # Parse host and port for reachability checks (optional)
        parsed = urlparse(self.base_url)
        self.scheme = parsed.scheme or "http"
        self.host = parsed.hostname or ""
        if parsed.port:
            self.port = parsed.port
        else:
            if self.scheme == "https":
                self.port = 443
            else:
                self.port = 80

    def _auth_header(self) -> Optional[Tuple[str, str]]:
        if self.username is not None and self.password is not None:
            token = f"{self.username}:{self.password}".encode("utf-8")
            b64 = base64.b64encode(token).decode("ascii")
            return ("Authorization", f"Basic {b64}")
        return None

    def _build_url(self, path: str, query: Optional[dict] = None) -> str:
        # Ensure trailing slash in base when joining a relative path
        base = self.base_url + ("/" if not self.base_url.endswith("/") else "")
        url = urljoin(base, path.lstrip("/"))
        if query:
            return url + "?" + parse.urlencode(query)
        return url

    def _do_get(self, url: str) -> Tuple[int, bytes]:
        headers = {
            "Accept": "application/json, */*;q=0.1",
            "User-Agent": "robot-driver/1.0",
        }
        auth = self._auth_header()
        if auth:
            headers[auth[0]] = auth[1]
        req = request.Request(url=url, method="GET", headers=headers)

        attempt = 0
        backoff = self.backoff_initial
        while True:
            try:
                with request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.getcode()
                    data = resp.read() or b""
                    return status, data
            except (error.URLError, error.HTTPError) as e:
                attempt += 1
                if attempt > self.max_retries:
                    if isinstance(e, error.HTTPError):
                        try:
                            body = e.read()  # type: ignore[attr-defined]
                        except Exception:
                            body = b""
                        return e.code if hasattr(e, "code") else 599, body  # type: ignore[attr-defined]
                    return 599, str(e).encode("utf-8")
                time.sleep(min(backoff, self.backoff_max))
                backoff = min(backoff * 2.0, self.backoff_max)

    def move_forward(self, linear_velocity: float) -> Tuple[int, bytes, str]:
        url = self._build_url("/move/forward", {"linear_velocity": str(linear_velocity)})
        status, body = self._do_get(url)
        return status, body, url

    def move_backward(self, linear_velocity: float) -> Tuple[int, bytes, str]:
        url = self._build_url("/move/backward", {"linear_velocity": str(linear_velocity)})
        status, body = self._do_get(url)
        return status, body, url
