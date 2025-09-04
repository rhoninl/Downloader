# device_client.py
import base64
import json
import time
import logging
import socket
from http.client import HTTPConnection, HTTPSConnection, ResponseException
from urllib.parse import urlencode
from typing import Optional, Tuple

from config import Config

logger = logging.getLogger(__name__)

class DeviceClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._auth_header = None
        if cfg.device_username is not None and cfg.device_password is not None:
            token = f"{cfg.device_username}:{cfg.device_password}".encode('utf-8')
            self._auth_header = 'Basic ' + base64.b64encode(token).decode('ascii')

    def _conn(self):
        if self.cfg.device_scheme == 'https':
            return HTTPSConnection(self.cfg.device_host, self.cfg.device_port, timeout=self.cfg.request_timeout_sec)
        else:
            return HTTPConnection(self.cfg.device_host, self.cfg.device_port, timeout=self.cfg.request_timeout_sec)

    def send_get(self, path: str, params: Optional[dict] = None) -> Tuple[int, bytes, float]:
        attempts = 0
        backoff = self.cfg.retry_backoff_ms / 1000.0
        last_exc = None
        while attempts < self.cfg.retry_max_attempts:
            attempts += 1
            try:
                full_path = self.cfg.join_device_path(path)
                if params:
                    query = urlencode(params, doseq=True)
                    if '?' in full_path:
                        full_path = full_path + '&' + query
                    else:
                        full_path = full_path + '?' + query
                conn = self._conn()
                headers = {}
                if self._auth_header:
                    headers['Authorization'] = self._auth_header
                start = time.monotonic()
                conn.request('GET', full_path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                rtt = (time.monotonic() - start) * 1000.0
                status = resp.status
                conn.close()
                logger.info('Device GET %s -> %s in %.1fms', full_path, status, rtt)
                return status, body, rtt
            except Exception as e:  # noqa: BLE001
                last_exc = e
                logger.warning('Device GET attempt %d failed: %s', attempts, e)
                time.sleep(backoff)
                backoff *= 2
        # After retries
        raise RuntimeError(f'Device GET failed after {self.cfg.retry_max_attempts} attempts: {last_exc}')

    def head_root(self) -> Tuple[bool, Optional[int], float, Optional[str]]:
        # Perform a HEAD / to validate HTTP service and measure RTT
        try:
            conn = self._conn()
            start = time.monotonic()
            conn.request('HEAD', self.cfg.join_device_path('/') if self.cfg.device_base_path else '/')
            resp = conn.getresponse()
            # Reading body is not necessary for HEAD but ensure release
            _ = resp.read()
            rtt = (time.monotonic() - start) * 1000.0
            status = resp.status
            conn.close()
            logger.debug('Device HEAD / -> %s in %.1fms', status, rtt)
            return True, status, rtt, None
        except Exception as e:  # noqa: BLE001
            logger.debug('Device HEAD failed: %s', e)
            return False, None, 0.0, str(e)

    def port_reachable(self) -> Tuple[bool, float, Optional[str]]:
        # Lightweight TCP connect test
        start = time.monotonic()
        try:
            with socket.create_connection((self.cfg.device_host, self.cfg.device_port), timeout=self.cfg.request_timeout_sec):
                rtt = (time.monotonic() - start) * 1000.0
                return True, rtt, None
        except Exception as e:  # noqa: BLE001
            return False, 0.0, str(e)

    def move_forward(self, linear_velocity: float) -> Tuple[int, bytes, float]:
        return self.send_get('/move/forward', {'linear_velocity': linear_velocity})

    def move_backward(self, linear_velocity: float) -> Tuple[int, bytes, float]:
        return self.send_get('/move/backward', {'linear_velocity': linear_velocity})
