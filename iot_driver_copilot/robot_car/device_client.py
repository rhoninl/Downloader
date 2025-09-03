# device_client.py
import json
import logging
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import base64
import http.cookiejar
from typing import Any, Dict, Optional


class DeviceClient:
    def __init__(self, base_url: str, timeout_s: float, username: Optional[str], password: Optional[str]):
        self._base_url = base_url.rstrip('/')
        self._timeout = timeout_s
        self._username = username
        self._password = password
        self._opener_lock = threading.Lock()
        self._opener = self._build_opener()
        self._last_status_code = None

    def _build_opener(self):
        cj = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(cj)]
        opener = urllib.request.build_opener(*handlers)
        return opener

    def _headers(self) -> Dict[str, str]:
        headers = {
            'Accept': 'application/json, text/plain, */*',
        }
        if self._username and self._password:
            creds = f"{self._username}:{self._password}".encode('utf-8')
            b64 = base64.b64encode(creds).decode('ascii')
            headers['Authorization'] = f"Basic {b64}"
        return headers

    def _url(self, path: str) -> str:
        if not path.startswith('/'):
            path = '/' + path
        return self._base_url + path

    def _request(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._url(path)
        data = None
        headers = self._headers()
        if json_body is not None:
            body = json.dumps(json_body).encode('utf-8')
            headers['Content-Type'] = 'application/json'
            data = body
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener_lock:
                with self._opener.open(req, timeout=self._timeout) as resp:
                    self._last_status_code = resp.getcode()
                    content_type = resp.headers.get('Content-Type', '')
                    raw = resp.read()
                    if 'application/json' in content_type:
                        try:
                            return json.loads(raw.decode('utf-8'))
                        except Exception:
                            # Malformed json; return text payload
                            return {'raw': raw.decode('utf-8', errors='replace')}
                    else:
                        # assume text or empty
                        text = raw.decode('utf-8', errors='replace')
                        return {'raw': text}
        except urllib.error.HTTPError as e:
            self._last_status_code = e.code
            raw = e.read().decode('utf-8', errors='replace') if e.fp else ''
            logging.warning("Device HTTP error %s on %s %s: %s", e.code, method, url, raw)
            raise
        except urllib.error.URLError as e:
            logging.warning("Device URL error on %s %s: %s", method, url, e)
            raise

    # Device operations
    def connect(self) -> Dict[str, Any]:
        logging.info("Calling device /connect")
        return self._request('POST', '/connect', json_body=None)

    def move_forward(self, speed: float, duration: Optional[float]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {'speed': speed}
        if duration is not None:
            payload['duration'] = duration
        logging.info("Calling device /move/forward speed=%s duration=%s", speed, duration)
        return self._request('POST', '/move/forward', json_body=payload)

    def move_backward(self, speed: float, duration: Optional[float]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {'speed': speed}
        if duration is not None:
            payload['duration'] = duration
        logging.info("Calling device /move/backward speed=%s duration=%s", speed, duration)
        return self._request('POST', '/move/backward', json_body=payload)

    def stop(self) -> Dict[str, Any]:
        logging.info("Calling device /stop")
        return self._request('POST', '/stop', json_body=None)

    def fetch_status(self) -> Dict[str, Any]:
        # Many devices expose a status endpoint; we attempt it.
        # If not available, the caller will handle errors gracefully.
        logging.debug("Polling device /status")
        return self._request('GET', '/status', json_body=None)

    @property
    def last_status_code(self) -> Optional[int]:
        return self._last_status_code
