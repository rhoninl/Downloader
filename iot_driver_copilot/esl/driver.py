#!/usr/bin/env python3
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError


# Configuration
class Config:
    def __init__(self):
        self.http_host = os.getenv("HTTP_HOST", "0.0.0.0")
        self.http_port = int(os.getenv("HTTP_PORT", "8000"))

        base_url = os.getenv("DEVICE_BASE_URL")
        if not base_url:
            print("ERROR: DEVICE_BASE_URL must be set", file=sys.stderr)
            sys.exit(2)
        self.device_base_url = base_url.rstrip("/")

        self.username = os.getenv("ESL_USERNAME")
        self.password_md5 = os.getenv("ESL_PASSWORD_MD5")
        if not self.username or not self.password_md5:
            print("ERROR: ESL_USERNAME and ESL_PASSWORD_MD5 must be set", file=sys.stderr)
            sys.exit(2)

        self.device_timeout = float(os.getenv("DEVICE_TIMEOUT_SECONDS", "5"))
        self.retry_max = int(os.getenv("DEVICE_RETRY_MAX", "3"))
        self.backoff_base = float(os.getenv("DEVICE_BACKOFF_BASE", "0.5"))
        self.backoff_max = float(os.getenv("DEVICE_BACKOFF_MAX", "5.0"))
        self.token_refresh = float(os.getenv("TOKEN_REFRESH_SECONDS", "600"))

        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        )


class TokenManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._token = None
        self._session = None
        self._last_login = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="TokenManager", daemon=False)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=10)

    def get_token(self):
        with self._lock:
            return self._token

    def _run(self):
        backoff = self.cfg.backoff_base
        while not self._stop.is_set():
            now = time.time()
            needs_login = (self._token is None) or (now - self._last_login > self.cfg.token_refresh)
            if not needs_login:
                # Sleep until next refresh or stop
                wait = max(1.0, min(self.cfg.token_refresh - (now - self._last_login), 5.0))
                self._stop.wait(wait)
                continue

            try:
                if self._login_once():
                    logging.info("Logged into device; session refreshed")
                    backoff = self.cfg.backoff_base
                    # Sleep a little to avoid hot loop
                    self._stop.wait(1.0)
                else:
                    raise RuntimeError("Login returned non-zero code")
            except Exception as e:
                logging.error("Login failed: %s", e)
                # Exponential backoff
                self._stop.wait(backoff)
                backoff = min(backoff * 2.0, self.cfg.backoff_max)

    def force_refresh(self):
        with self._lock:
            self._token = None
        # Wake the loop to refresh immediately
        return True

    def _login_once(self) -> bool:
        body = json.dumps({"username": self.cfg.username, "password": self.cfg.password_md5}).encode("utf-8")
        url = f"{self.cfg.device_base_url}/api/login"
        req = urlrequest.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json;charset=UTF-8")
        logging.info("Attempting login to device")
        with urlrequest.urlopen(req, timeout=self.cfg.device_timeout) as resp:
            resp_body = resp.read()
            payload = json.loads(resp_body.decode("utf-8") if resp_body else "{}")
            if payload.get("code") == 0:
                with self._lock:
                    self._token = payload.get("token")
                    self._session = payload.get("session")
                    self._last_login = time.time()
                logging.info("Login success; session=%s; last_login=%s", str(self._session), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._last_login)))
                return True
            else:
                logging.warning("Login returned code=%s message=%s", payload.get("code"), payload.get("message"))
                return False


class DeviceClient:
    def __init__(self, cfg: Config, tokens: TokenManager):
        self.cfg = cfg
        self.tokens = tokens
        self._last_update_lock = threading.RLock()
        self.last_update_ts = None
        self.last_update_type = None  # 'flush' or 'data'

    def _update_marker(self, typ: str):
        with self._last_update_lock:
            self.last_update_ts = time.time()
            self.last_update_type = typ
            logging.info("Last device update: type=%s at=%s", typ, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.last_update_ts)))

    def call_device(self, path: str, method: str, json_body: dict, incoming_token: str = None, op_type: str = "op"):
        url = f"{self.cfg.device_base_url}{path}"
        payload = json.dumps(json_body).encode("utf-8")

        attempt = 0
        backoff = self.cfg.backoff_base
        last_exc = None

        while attempt < self.cfg.retry_max:
            attempt += 1
            token = incoming_token or self.tokens.get_token()
            req = urlrequest.Request(url, data=payload, method=method)
            req.add_header("Content-Type", "application/json;charset=UTF-8")
            if token:
                req.add_header("token", token)

            try:
                with urlrequest.urlopen(req, timeout=self.cfg.device_timeout) as resp:
                    body_bytes = resp.read()
                    text = body_bytes.decode("utf-8") if body_bytes else "{}"
                    try:
                        body_json = json.loads(text)
                    except json.JSONDecodeError:
                        body_json = {"raw": text}

                    # If device indicates token invalid, force refresh and retry
                    if isinstance(body_json, dict) and body_json.get("code") == 0:
                        self._update_marker(op_type)
                        return 200, body_json
                    else:
                        # Possible token issue or other error; force refresh once and retry
                        logging.warning("Device returned non-zero code=%s msg=%s on attempt %d", body_json.get("code"), body_json.get("msg") or body_json.get("message"), attempt)
                        if attempt < self.cfg.retry_max:
                            self.tokens.force_refresh()
                            time.sleep(backoff)
                            backoff = min(backoff * 2.0, self.cfg.backoff_max)
                            continue
                        else:
                            return 502, body_json

            except HTTPError as e:
                last_exc = e
                status = e.code
                try:
                    err_text = e.read().decode("utf-8")
                except Exception:
                    err_text = ""
                logging.error("HTTPError from device: %s %s", status, err_text)
                if status in (401, 403):
                    self.tokens.force_refresh()
                if attempt < self.cfg.retry_max:
                    time.sleep(backoff)
                    backoff = min(backoff * 2.0, self.cfg.backoff_max)
                    continue
                return 502, {"error": "device_http_error", "status": status, "body": err_text}
            except URLError as e:
                last_exc = e
                logging.error("URLError from device: %s", e)
                if attempt < self.cfg.retry_max:
                    time.sleep(backoff)
                    backoff = min(backoff * 2.0, self.cfg.backoff_max)
                    continue
                return 504, {"error": "device_unreachable", "reason": str(e)}
            except Exception as e:
                last_exc = e
                logging.exception("Unexpected error calling device")
                if attempt < self.cfg.retry_max:
                    time.sleep(backoff)
                    backoff = min(backoff * 2.0, self.cfg.backoff_max)
                    continue
                return 500, {"error": "unexpected", "reason": str(e)}

        # Shouldn't reach here normally
        return 500, {"error": "retries_exhausted", "reason": str(last_exc) if last_exc else "unknown"}


class Handler(BaseHTTPRequestHandler):
    cfg: Config = None
    tokens: TokenManager = None
    client: DeviceClient = None

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_json_body(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        data = self.rfile.read(length) if length > 0 else b''
        if not data:
            return {}
        try:
            return json.loads(data.decode('utf-8'))
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON body")

    def log_message(self, format, *args):
        logging.info("%s - %s" % (self.address_string(), format % args))

    def do_POST(self):
        if self.path == "/esl/flush":
            self._handle_esl_flush()
            return
        self._send_json(404, {"error": "not_found"})

    def do_PUT(self):
        if self.path == "/esl/data":
            self._handle_esl_data()
            return
        self._send_json(404, {"error": "not_found"})

    def _handle_esl_flush(self):
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        tag = body.get("tag")
        if not tag:
            self._send_json(400, {"error": "missing_field", "field": "tag"})
            return
        incoming_token = self.headers.get("token") or self.headers.get("Token")
        status, resp = self.client.call_device(
            path="/api/esl/eslflush",
            method="POST",
            json_body={"tag": tag},
            incoming_token=incoming_token,
            op_type="flush",
        )
        self._send_json(status, resp if isinstance(resp, dict) else {"result": resp})

    def _handle_esl_data(self):
        try:
            body = self._read_json_body()
        except ValueError as e:
            self._send_json(400, {"error": str(e)})
            return
        if not isinstance(body, dict) or not body:
            self._send_json(400, {"error": "empty_or_invalid_payload"})
            return
        incoming_token = self.headers.get("token") or self.headers.get("Token")
        status, resp = self.client.call_device(
            path="/api/esl/productalert",
            method="POST",  # device expects POST even though our API is PUT
            json_body=body,
            incoming_token=incoming_token,
            op_type="data",
        )
        self._send_json(status, resp if isinstance(resp, dict) else {"result": resp})


def run_server():
    cfg = Config()
    tokens = TokenManager(cfg)
    tokens.start()
    client = DeviceClient(cfg, tokens)

    Handler.cfg = cfg
    Handler.tokens = tokens
    Handler.client = client

    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), Handler)
    httpd.timeout = 1.0

    stop_event = threading.Event()

    def shutdown_handler(signum, frame):
        logging.info("Received signal %s; shutting down...", signum)
        stop_event.set()
        # Trigger serve_forever to exit
        httpd.shutdown()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    logging.info("HTTP server starting on %s:%d", cfg.http_host, cfg.http_port)
    try:
        httpd.serve_forever()
    finally:
        logging.info("HTTP server stopping...")
        tokens.stop()
        httpd.server_close()
        logging.info("Shutdown complete")


if __name__ == "__main__":
    run_server()
