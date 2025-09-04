import json
import logging
import threading
import time
import random
import base64
from datetime import datetime, timezone
from typing import Optional, Tuple, Any, Dict
from urllib import request, error


class DeviceClient:
    def __init__(self, config):
        self.cfg = config
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_sample: Optional[Dict[str, Any]] = None
        self._last_update_ts: Optional[float] = None
        self._connected: bool = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="telemetry-poller", daemon=True)
        self._thread.start()
        logging.info("DeviceClient: started telemetry poller")

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        logging.info("DeviceClient: stopped telemetry poller")

    def _auth_header(self) -> Optional[str]:
        if self.cfg.device_auth_token:
            return f"Bearer {self.cfg.device_auth_token}"
        if self.cfg.device_username is not None and self.cfg.device_password is not None:
            creds = f"{self.cfg.device_username}:{self.cfg.device_password}".encode("utf-8")
            token = base64.b64encode(creds).decode("ascii")
            return f"Basic {token}"
        return None

    def _headers(self, content_type_json: bool = False) -> dict:
        headers = {
            "Accept": "application/json",
            "User-Agent": "smartdonkey-driver/1.0",
        }
        if content_type_json:
            headers["Content-Type"] = "application/json"
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        return headers

    def _poll_loop(self):
        backoff = self.cfg.retry_initial_backoff_sec
        while not self._stop_event.is_set():
            try:
                req = request.Request(self.cfg.telemetry_url, headers=self._headers())
                with request.urlopen(req, timeout=self.cfg.http_timeout_sec) as resp:
                    status = getattr(resp, "status", resp.getcode())
                    if status != 200:
                        raise RuntimeError(f"Telemetry HTTP {status}")
                    raw = resp.read()
                    data = json.loads(raw.decode("utf-8"))
                with self._lock:
                    self._last_sample = data
                    self._last_update_ts = time.time()
                    self._connected = True
                logging.debug(
                    "Telemetry updated at %s", datetime.fromtimestamp(self._last_update_ts, tz=timezone.utc).isoformat()
                )
                # Reset backoff on success
                backoff = self.cfg.retry_initial_backoff_sec
                # Normal poll interval
                self._sleep_interruptible(self.cfg.poll_interval_sec)
            except Exception as e:
                with self._lock:
                    self._connected = False
                logging.warning("Telemetry poll failed: %s", e)
                # Exponential backoff with jitter
                jitter = random.uniform(0, backoff * 0.1)
                wait = min(self.cfg.retry_max_backoff_sec, backoff) + jitter
                logging.info("Retrying telemetry in %.2fs", wait)
                self._sleep_interruptible(wait)
                backoff = min(self.cfg.retry_max_backoff_sec, backoff * 2)

    def _sleep_interruptible(self, seconds: float):
        # Sleep in small chunks to respond to stop quickly
        end = time.time() + seconds
        while not self._stop_event.is_set() and time.time() < end:
            time.sleep(min(0.2, max(0.0, end - time.time())))

    def get_last_sample(self) -> Tuple[Optional[dict], Optional[float]]:
        with self._lock:
            return self._last_sample, self._last_update_ts

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def send_velocity(self, linear_velocity: float, angular_velocity: float = 0.0) -> Dict[str, Any]:
        payload = {
            "linear_velocity": float(linear_velocity),
            "angular_velocity": float(angular_velocity),
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self.cfg.velocity_cmd_url,
            data=body,
            headers=self._headers(content_type_json=True),
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.cfg.http_timeout_sec) as resp:
                status = getattr(resp, "status", resp.getcode())
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read()
                if "application/json" in ctype:
                    try:
                        body_parsed = json.loads(raw.decode("utf-8"))
                    except Exception:
                        body_parsed = {"raw": raw.decode("utf-8", errors="replace")}
                else:
                    body_parsed = {"raw": raw.decode("utf-8", errors="replace")}
                logging.info("Sent velocity cmd: lin=%.3f ang=%.3f status=%s", linear_velocity, angular_velocity, status)
                return {"http_status": status, "body": body_parsed}
        except error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, 'read') else str(e)
            logging.error("Velocity command failed HTTPError: %s %s", e.code, err_body)
            return {"http_status": int(getattr(e, "code", 0) or 0), "error": err_body}
        except Exception as e:
            logging.error("Velocity command failed: %s", e)
            return {"http_status": 0, "error": str(e)}
