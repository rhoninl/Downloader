# device_client.py
# REST client and background ingest for the robot device

import json
import logging
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from typing import Any, Dict, Optional


class DeviceClient:
    def __init__(
        self,
        base_url: str,
        state_path: str,
        cmd_move_path: str,
        user: Optional[str],
        passwd: Optional[str],
        timeout_s: float,
        poll_interval_s: float,
        retry_backoff_s: float,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self.state_url = self.base_url + (state_path if state_path.startswith('/') else '/' + state_path)
        self.move_url = self.base_url + (cmd_move_path if cmd_move_path.startswith('/') else '/' + cmd_move_path)
        self.user = user
        self.passwd = passwd
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.retry_backoff_s = retry_backoff_s

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._latest: Optional[Dict[str, Any]] = None
        self._last_update_ts: Optional[float] = None
        self._connected: bool = False
        self._samples_count: int = 0
        self._errors_count: int = 0
        self._start_monotonic = time.monotonic()

    @property
    def condition(self) -> threading.Condition:
        return self._cv

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._ingest_loop, name="device-ingest", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _auth_header(self) -> Optional[str]:
        if self.user:
            import base64
            creds = f"{self.user}:{self.passwd or ''}".encode('utf-8')
            return 'Basic ' + base64.b64encode(creds).decode('ascii')
        return None

    def _http_get_json(self, url: str) -> Dict[str, Any]:
        headers = {
            'Accept': 'application/json',
        }
        ah = self._auth_header()
        if ah:
            headers['Authorization'] = ah
        req = urllib.request.Request(url=url, headers=headers, method='GET')
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            charset = resp.headers.get_content_charset() or 'utf-8'
            data = resp.read().decode(charset)
            return json.loads(data)

    def _http_post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode('utf-8')
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        ah = self._auth_header()
        if ah:
            headers['Authorization'] = ah
        req = urllib.request.Request(url=url, data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            charset = resp.headers.get_content_charset() or 'utf-8'
            data = resp.read().decode(charset)
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return {"status": "ok", "raw": data}

    def _ingest_loop(self) -> None:
        log = logging.getLogger('DeviceClient')
        log.info(f"Starting ingest loop polling %s every %.3fs", self.state_url, self.poll_interval_s)
        next_delay = self.poll_interval_s
        while not self._stop_event.is_set():
            try:
                data = self._http_get_json(self.state_url)
                now = time.time()
                with self._lock:
                    self._latest = data
                    self._last_update_ts = now
                    self._samples_count += 1
                    self._connected = True
                    self._cv.notify_all()
                next_delay = self.poll_interval_s
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
                with self._lock:
                    self._errors_count += 1
                    # flag disconnected but keep last data
                    self._connected = False
                log.warning("Ingest error: %s", e)
                next_delay = max(self.retry_backoff_s, self.poll_interval_s)
            # Wait for either stop or next poll
            self._stop_event.wait(next_delay)
        log.info("Ingest loop stopped")

    def get_latest(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._latest is None:
                return None
            # return a shallow copy to avoid external mutation
            return dict(self._latest)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                'connected': self._connected,
                'last_update_ts': self._last_update_ts,
                'samples_count': self._samples_count,
                'errors_count': self._errors_count,
                'ingest_thread_alive': self._thread.is_alive() if self._thread else False,
                'uptime_s': time.monotonic() - self._start_monotonic,
                'device_state_url': self.state_url,
                'poll_interval_s': self.poll_interval_s,
                'timeout_s': self.timeout_s,
            }

    def move_velocity(self, linear_velocity: float, angular_velocity: float = 0.0) -> Dict[str, Any]:
        payload = {
            'linear_velocity': float(linear_velocity),
            'angular_velocity': float(angular_velocity),
        }
        try:
            resp = self._http_post_json(self.move_url, payload)
            with self._lock:
                # Assuming a successful command implies connectivity
                self._connected = True
            return {'ok': True, 'sent': payload, 'device_response': resp}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as e:
            with self._lock:
                self._errors_count += 1
                self._connected = False
            return {'ok': False, 'error': str(e), 'sent': payload}
