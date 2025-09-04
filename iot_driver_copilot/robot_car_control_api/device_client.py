import json
import logging
import time
import urllib.request
import urllib.error
import base64
from urllib.parse import urljoin
from typing import Any, Dict, Optional

from config import load_config


logger = logging.getLogger("device_client")


class DeviceHTTPClient:
    def __init__(self) -> None:
        self.cfg = load_config()
        self._common_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # Auth headers
        if self.cfg.auth_token:
            self._common_headers["Authorization"] = f"Bearer {self.cfg.auth_token}"
        elif self.cfg.basic_username is not None and self.cfg.basic_password is not None:
            creds = f"{self.cfg.basic_username}:{self.cfg.basic_password}".encode("utf-8")
            b64 = base64.b64encode(creds).decode("ascii")
            self._common_headers["Authorization"] = f"Basic {b64}"

    def _request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = urljoin(self.cfg.device_base_url.rstrip('/') + '/', path.lstrip('/'))
        body = None
        headers = dict(self._common_headers)
        if data is not None:
            body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url=url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.device_timeout_s) as resp:
                raw = resp.read()
                if not raw:
                    return {"status": resp.status, "data": None}
                try:
                    return {"status": resp.status, "data": json.loads(raw.decode("utf-8"))}
                except Exception:
                    return {"status": resp.status, "data": raw.decode("utf-8", errors="ignore")}
        except urllib.error.HTTPError as e:
            content = e.read().decode("utf-8", errors="ignore") if hasattr(e, 'read') else ""
            raise RuntimeError(f"HTTPError {e.code} {method} {url}: {content}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"URLError {method} {url}: {e}") from e

    def get_status(self) -> Dict[str, Any]:
        res = self._request("GET", self.cfg.device_status_path)
        if res.get("status", 200) >= 400:
            raise RuntimeError(f"Device status HTTP {res['status']}")
        data = res.get("data")
        if isinstance(data, dict):
            return data
        return {"raw": data}

    def send_move(self, linear_velocity: float, duration_ms: Optional[int]) -> Dict[str, Any]:
        # Generic payload using common robot fields
        payload: Dict[str, Any] = {
            "linear_velocity": linear_velocity,
            "angular_velocity": 0.0,
        }
        if duration_ms and duration_ms > 0:
            payload["duration_ms"] = int(duration_ms)
        logger.info("Sending move command to device: %s", payload)
        return self._request("POST", self.cfg.device_move_cmd_path, payload)

    def send_stop(self) -> Dict[str, Any]:
        if self.cfg.device_stop_cmd_path:
            logger.info("Sending explicit stop command to device")
            return self._request("POST", self.cfg.device_stop_cmd_path, {"stop": True})
        else:
            # Fallback to velocity 0 as stop
            logger.info("Sending zero-velocity stop fallback to device")
            return self.send_move(0.0, None)
