# device_client.py
import json
import time
import base64
from urllib import request, error
from urllib.parse import urljoin
from typing import Optional, Tuple


class DeviceClient:
    def __init__(self, base_url: str, timeout_sec: float, retry_max: int, retry_backoff_sec: float,
                 username: Optional[str] = None, password: Optional[str] = None):
        if not base_url.endswith('/'):
            base_url = base_url + '/'
        self.base_url = base_url
        self.timeout_sec = timeout_sec
        self.retry_max = retry_max
        self.retry_backoff_sec = retry_backoff_sec
        self._auth_header = None
        if username and password:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            self._auth_header = f"Basic {token}"

    def _request(self, path: str, payload: Optional[dict]) -> Tuple[int, bytes]:
        url = urljoin(self.base_url, path.lstrip('/'))
        data = None
        headers = {
            'Accept': 'application/json',
        }
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if self._auth_header:
            headers['Authorization'] = self._auth_header

        last_exc = None
        for attempt in range(self.retry_max + 1):
            try:
                req = request.Request(url=url, data=data, method='POST', headers=headers)
                with request.urlopen(req, timeout=self.timeout_sec) as resp:
                    status = resp.getcode()
                    body = resp.read()
                    return status, body
            except (error.HTTPError, error.URLError) as e:
                last_exc = e
                if attempt < self.retry_max:
                    sleep_for = self._compute_backoff(attempt)
                    time.sleep(sleep_for)
                else:
                    break
        if isinstance(last_exc, error.HTTPError):
            # propagate HTTP error body if available
            try:
                body = last_exc.read()
            except Exception:
                body = b''
            return last_exc.code, body
        raise last_exc  # URLError or other

    def _compute_backoff(self, attempt: int) -> float:
        # exponential backoff: base * 2^attempt
        return self.retry_backoff_sec * (2 ** attempt)

    def connect(self) -> Tuple[int, bytes]:
        return self._request('/connect', payload=None)

    def stop(self) -> Tuple[int, bytes]:
        return self._request('/stop', payload=None)

    def move_forward(self, speed: float, duration: Optional[float]) -> Tuple[int, bytes]:
        payload = {'speed': float(speed)}
        if duration is not None:
            payload['duration'] = float(duration)
        return self._request('/move/forward', payload=payload)

    def move_backward(self, speed: float, duration: Optional[float]) -> Tuple[int, bytes]:
        payload = {'speed': float(speed)}
        if duration is not None:
            payload['duration'] = float(duration)
        return self._request('/move/backward', payload=payload)
