import base64
import json
import logging
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from config import load_config, Config, ConfigError


# Thread-safe telemetry buffer
class TelemetryBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}
        self._last_update: Optional[float] = None

    def set(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._data = data
            self._last_update = time.time()

    def snapshot(self) -> Tuple[Dict[str, Any], Optional[float]]:
        with self._lock:
            return dict(self._data), self._last_update


def build_auth_headers(cfg: Config) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if cfg.auth_token:
        headers["Authorization"] = f"Bearer {cfg.auth_token}"
    elif cfg.username is not None and cfg.password is not None:
        userpass = f"{cfg.username}:{cfg.password}".encode("utf-8")
        b64 = base64.b64encode(userpass).decode("ascii")
        headers["Authorization"] = f"Basic {b64}"
    return headers


def http_get_json(url: str, headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    req = Request(url, headers={**headers, "Accept": "application/json"}, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", resp.getcode())
        if status < 200 or status >= 300:
            raise HTTPError(url, status, f"Bad status: {status}", resp.headers, None)
        data = resp.read()
        return json.loads(data.decode("utf-8"))


def http_post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={**headers, "Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", resp.getcode())
        if status < 200 or status >= 300:
            raise HTTPError(url, status, f"Bad status: {status}", resp.headers, None)
        body = resp.read()
        if not body:
            return {"status": "ok"}
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return {"raw": body.decode("utf-8", errors="replace")}


def telemetry_worker(cfg: Config, buf: TelemetryBuffer, stop_event: threading.Event) -> None:
    headers = build_auth_headers(cfg)
    backoff = cfg.backoff_min
    logging.info("Telemetry worker started")
    while not stop_event.is_set():
        try:
            data = http_get_json(cfg.device_status_url, headers, cfg.request_timeout)
            buf.set(data)
            backoff = cfg.backoff_min
            logging.info("Telemetry updated at %s", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
            # Regular polling interval
            stop_event.wait(cfg.poll_interval)
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError) as e:
            logging.error("Telemetry fetch error: %s", e)
            # Exponential backoff on errors
            wait_s = min(backoff, cfg.backoff_max)
            logging.info("Retrying telemetry in %.3f seconds", wait_s)
            stop_event.wait(wait_s)
            backoff = min(cfg.backoff_max, backoff * 2 if backoff > 0 else cfg.backoff_min)
        except Exception as e:
            logging.exception("Unexpected error in telemetry worker: %s", e)
            stop_event.wait(min(cfg.backoff_min, 5.0))
    logging.info("Telemetry worker stopped")


def make_handler(cfg: Config, buf: TelemetryBuffer):
    auth_headers = build_auth_headers(cfg)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SmartDonkeyHTTPProxy/1.0"

        def log_message(self, format: str, *args) -> None:
            logging.info("%s - - %s", self.address_string(), format % args)

        def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            if path not in ("/move/forward", "/move/backward"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return

            qs = parse_qs(parsed.query)
            lv_vals = qs.get("linear_velocity")
            if not lv_vals or len(lv_vals) == 0:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing query parameter: linear_velocity"})
                return
            try:
                linear_velocity = float(lv_vals[0])
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "linear_velocity must be a number"})
                return

            if path == "/move/backward":
                linear_velocity = -abs(linear_velocity)
            else:
                linear_velocity = abs(linear_velocity)

            payload = {
                "linear_velocity": linear_velocity,
                "angular_velocity": 0.0,
            }

            try:
                resp = http_post_json(cfg.device_move_velocity_url, auth_headers, payload, cfg.request_timeout)
            except (URLError, HTTPError, TimeoutError) as e:
                logging.error("Command dispatch error: %s", e)
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "device command failed", "details": str(e)})
                return
            except Exception as e:
                logging.exception("Unexpected command error: %s", e)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})
                return

            last_data, last_ts = buf.snapshot()
            response = {
                "status": "ok",
                "command": "move_velocity",
                "sent": payload,
                "device_response": resp,
                "telemetry_last_update": last_ts,
                "telemetry": last_data,
            }
            self._send_json(HTTPStatus.OK, response)

    return Handler


def main() -> None:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    logging.info("Starting driver HTTP server on %s:%s", cfg.http_host, cfg.http_port)
    logging.info("Using device status URL: %s", cfg.device_status_url)
    logging.info("Using device move URL: %s", cfg.device_move_velocity_url)
    if cfg.auth_token:
        logging.info("Auth mode: Bearer token")
    elif cfg.username is not None and cfg.password is not None:
        logging.info("Auth mode: Basic auth (username provided)")
    else:
        logging.info("Auth mode: none")

    buf = TelemetryBuffer()
    stop_event = threading.Event()

    # Start telemetry worker
    telemetry_thread = threading.Thread(target=telemetry_worker, args=(cfg, buf, stop_event), name="telemetry", daemon=True)
    telemetry_thread.start()

    # Create HTTP server
    Handler = make_handler(cfg, buf)
    httpd = ThreadingHTTPServer((cfg.http_host, cfg.http_port), Handler)

    def shutdown_handler(signum, frame):
        logging.info("Signal %s received, shutting down...", signum)
        stop_event.set()
        httpd.shutdown()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    server_thread = threading.Thread(target=httpd.serve_forever, name="http-server", daemon=True)
    server_thread.start()

    try:
        while server_thread.is_alive():
            server_thread.join(timeout=0.5)
    finally:
        logging.info("Server stopping...")
        httpd.server_close()
        stop_event.set()
        telemetry_thread.join(timeout=5.0)
        logging.info("Shutdown complete")


if __name__ == "main":
    main()
