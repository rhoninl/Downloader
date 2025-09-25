import asyncio
import json
import logging
import math
import random
import signal
import sys
import time
from typing import Any, Dict, Optional

import aiohttp
from aiohttp import web

from config import load_config, Config, ConfigError


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
)
logger = logging.getLogger("robot-car-driver")


class DeviceClient:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._session: Optional[aiohttp.ClientSession] = None
        self._telemetry_task: Optional[asyncio.Task] = None
        self._running = False

        self._latest_telemetry: Optional[Dict[str, Any]] = None
        self._latest_ts: Optional[float] = None
        self._tel_lock = asyncio.Lock()

        # connection state tracking for logging
        self._was_connected = False

    async def start(self):
        timeout = aiohttp.ClientTimeout(total=self._cfg.request_timeout_secs)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._running = True
        self._telemetry_task = asyncio.create_task(self._telemetry_loop(), name="telemetry-loop")
        logger.info("Device client started; telemetry loop launched")

    async def stop(self):
        self._running = False
        if self._telemetry_task:
            self._telemetry_task.cancel()
            try:
                await self._telemetry_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error stopping telemetry task")
        if self._session:
            await self._session.close()
        logger.info("Device client stopped")

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._cfg.device_bearer_token:
            headers["Authorization"] = f"Bearer {self._cfg.device_bearer_token}"
        return headers

    def _auth(self) -> Optional[aiohttp.BasicAuth]:
        if self._cfg.device_username and self._cfg.device_password:
            return aiohttp.BasicAuth(self._cfg.device_username, self._cfg.device_password)
        return None

    def _url(self, path: str) -> str:
        return f"{self._cfg.device_base_url}{path}"

    async def send_velocity(self, linear_velocity: float) -> Dict[str, Any]:
        if not self._session:
            raise RuntimeError("Session not initialized")
        url = self._url(self._cfg.device_set_velocity_path)
        payload = {
            "linear_velocity": float(linear_velocity),
            "angular_velocity": 0.0,
        }
        logger.info("POST %s payload=%s", url, payload)
        async with self._session.post(url, json=payload, headers=self._headers(), auth=self._auth()) as resp:
            txt = await resp.text()
            if resp.status >= 400:
                logger.error("Device set_velocity failed: %s %s", resp.status, txt)
                raise web.HTTPBadGateway(text=json.dumps({
                    "error": "device_error",
                    "status": resp.status,
                    "body": txt,
                }), content_type='application/json')
            try:
                return json.loads(txt) if txt else {"status": "ok"}
            except Exception:
                return {"status": "ok", "raw": txt}

    async def _telemetry_loop(self):
        assert self._session is not None
        url = self._url(self._cfg.device_telemetry_path)
        backoff = self._cfg.backoff_min_secs
        while self._running:
            try:
                async with self._session.get(url, headers=self._headers(), auth=self._auth()) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"Telemetry HTTP {resp.status}")
                    data = await resp.json(content_type=None)
                    async with self._tel_lock:
                        self._latest_telemetry = data
                        self._latest_ts = time.time()
                    if not self._was_connected:
                        logger.info("Telemetry connection established at %s", time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self._latest_ts)))
                        self._was_connected = True
                    logger.debug("Telemetry updated; keys=%s", list(data.keys()) if isinstance(data, dict) else type(data))
                # success path: sleep normal polling interval and reset backoff
                await asyncio.sleep(self._cfg.poll_interval_secs)
                backoff = self._cfg.backoff_min_secs
            except asyncio.CancelledError:
                break
            except Exception as e:
                # failure path: log and backoff
                if self._was_connected:
                    logger.warning("Telemetry disconnected: %s", e)
                    self._was_connected = False
                else:
                    logger.debug("Telemetry poll error (still disconnected): %s", e)
                # jittered exponential backoff
                jitter = random.uniform(0.5, 1.5)
                delay = min(self._cfg.backoff_max_secs, max(self._cfg.backoff_min_secs, backoff * jitter))
                logger.info("Retrying telemetry in %.2fs", delay)
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break
                backoff = min(self._cfg.backoff_max_secs, backoff * 2)

    async def latest_snapshot(self) -> Dict[str, Any]:
        async with self._tel_lock:
            return {
                "telemetry": self._latest_telemetry,
                "timestamp": self._latest_ts,
            }


async def move_forward(request: web.Request) -> web.Response:
    app = request.app
    cfg: Config = app["config"]
    client: DeviceClient = app["client"]

    try:
        body = await request.json()
    except Exception:
        body = {}

    speed = None
    if isinstance(body, dict) and "speed" in body:
        speed = body["speed"]
    if speed is None:
        # try query param
        sp = request.query.get("speed")
        if sp is not None:
            speed = sp

    try:
        speed = float(speed)
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "missing_or_invalid_speed", "detail": "Provide numeric 'speed' in body JSON or query"}), content_type='application/json')

    if not math.isfinite(speed) or speed <= 0:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_speed", "detail": "Speed must be a positive finite number"}), content_type='application/json')

    if abs(speed) > cfg.max_speed_mps:
        raise web.HTTPBadRequest(text=json.dumps({"error": "speed_exceeds_limit", "max_speed_mps": cfg.max_speed_mps}), content_type='application/json')

    try:
        device_resp = await client.send_velocity(abs(speed))
        payload = {
            "status": "ok",
            "command": "forward",
            "requested_speed": abs(speed),
            "device_response": device_resp,
        }
        return web.json_response(payload)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.exception("Error handling /move/forward: %s", e)
        raise web.HTTPBadGateway(text=json.dumps({"error": "device_unreachable", "detail": str(e)}), content_type='application/json')


async def move_backward(request: web.Request) -> web.Response:
    app = request.app
    cfg: Config = app["config"]
    client: DeviceClient = app["client"]

    try:
        body = await request.json()
    except Exception:
        body = {}

    speed = None
    if isinstance(body, dict) and "speed" in body:
        speed = body["speed"]
    if speed is None:
        # try query param
        sp = request.query.get("speed")
        if sp is not None:
            speed = sp

    try:
        speed = float(speed)
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"error": "missing_or_invalid_speed", "detail": "Provide numeric 'speed' in body JSON or query"}), content_type='application/json')

    if not math.isfinite(speed) or speed <= 0:
        raise web.HTTPBadRequest(text=json.dumps({"error": "invalid_speed", "detail": "Speed must be a positive finite number"}), content_type='application/json')

    if abs(speed) > cfg.max_speed_mps:
        raise web.HTTPBadRequest(text=json.dumps({"error": "speed_exceeds_limit", "max_speed_mps": cfg.max_speed_mps}), content_type='application/json')

    try:
        device_resp = await client.send_velocity(-abs(speed))
        payload = {
            "status": "ok",
            "command": "backward",
            "requested_speed": -abs(speed),
            "device_response": device_resp,
        }
        return web.json_response(payload)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.exception("Error handling /move/backward: %s", e)
        raise web.HTTPBadGateway(text=json.dumps({"error": "device_unreachable", "detail": str(e)}), content_type='application/json')


async def on_startup(app: web.Application):
    client: DeviceClient = app["client"]
    await client.start()


async def on_cleanup(app: web.Application):
    client: DeviceClient = app["client"]
    await client.stop()


def build_app(cfg: Config) -> web.Application:
    app = web.Application()
    app["config"] = cfg
    app["client"] = DeviceClient(cfg)

    app.router.add_post("/move/forward", move_forward)
    app.router.add_post("/move/backward", move_backward)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


def main():
    try:
        cfg = load_config()
    except ConfigError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(2)

    app = build_app(cfg)

    loop = asyncio.get_event_loop()

    # Graceful shutdown handling
    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("Shutdown signal received")
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _handle_signal)
        except NotImplementedError:
            # Windows may not support all signal handlers
            pass

    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, cfg.http_host, cfg.http_port)
    loop.run_until_complete(site.start())
    logger.info("HTTP server listening on %s:%s", cfg.http_host, cfg.http_port)

    try:
        loop.run_until_complete(stop_event.wait())
    finally:
        loop.run_until_complete(runner.cleanup())
        loop.run_until_complete(app.shutdown())
        loop.run_until_complete(app.cleanup())
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        try:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        loop.close()


if __name__ == "__main__":
    main()
