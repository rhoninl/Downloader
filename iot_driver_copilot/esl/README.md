# ESL Driver (HTTP Proxy)

A lightweight Python driver that connects to a Telpo ESL controller using its native HTTP API and exposes a compact HTTP interface. The driver handles device login, token management, retries, timeouts, and backoff, and provides two endpoints:

- POST /esl/flush: Trigger a flush/refresh on a specific ESL tag
- PUT /esl/data: Update product/template data on the controller

The driver manages the session token automatically in the background. Optionally, a client can override the token by sending a `token` header.

## Requirements

- Python 3.8+
- Network access to the ESL controller

## Configuration (Environment Variables)

- DEVICE_BASE_URL: Base URL of the ESL controller (e.g., http://192.168.2.97)
- ESL_USERNAME: Username for the controller login
- ESL_PASSWORD_MD5: MD5 hash (lowercase hex) of the login password
- HTTP_HOST: HTTP server bind host (default: 0.0.0.0)
- HTTP_PORT: HTTP server port (default: 8000)
- DEVICE_TIMEOUT_SECONDS: Device request timeout in seconds (default: 5)
- DEVICE_RETRY_MAX: Max per-request retries to the device (default: 3)
- DEVICE_BACKOFF_BASE: Initial backoff seconds between retries (default: 0.5)
- DEVICE_BACKOFF_MAX: Maximum backoff seconds (default: 5.0)
- TOKEN_REFRESH_SECONDS: Periodic login refresh interval seconds (default: 600)
- LOG_LEVEL: Logging level (DEBUG, INFO, WARN, ERROR; default: INFO)

Note: You must provide `ESL_PASSWORD_MD5`. Example MD5 for `admin` is `21232f297a57a5a743894a0e4a801fc3` (do not use this in production). In the device example, the password hash was `0192023a7bbd73250516f069df18b500`.

## Run

Example:

```bash
export DEVICE_BASE_URL="http://192.168.2.97"
export ESL_USERNAME="admin"
export ESL_PASSWORD_MD5="0192023a7bbd73250516f069df18b500"
export HTTP_PORT=8000
python3 driver.py
```

Logs will show login attempts, connection health, retries, and last update timestamps.

## API

All endpoints accept/return JSON and are browser/CLI friendly. Do not include trailing slashes.

1) Update ESL data
- Method: PUT
- Path: /esl/data
- Body: JSON with fields used by your template, e.g.: id, name, code, upc1, upc2, upc3, upc4, F_01..F_32, templ
- Internally maps to device /api/esl/productalert (POST)

Example:
```bash
curl -X PUT "http://localhost:8000/esl/data" \
  -H "Content-Type: application/json" \
  -d '{
    "id":"106",
    "name":"dmall测试商品",
    "code":"20241129",
    "upc1":"",
    "upc2":"",
    "upc3":"",
    "upc4":"",
    "F_01":"777.0",
    "F_04":"500L",
    "F_06":"北京",
    "templ":"ddshoptest"
  }'
```

2) Flush ESL tag (refresh display)
- Method: POST
- Path: /esl/flush
- Body: { "tag": "<TAG_ID>" }
- Internally maps to device /api/esl/eslflush (POST)

Example:
```bash
curl -X POST "http://localhost:8000/esl/flush" \
  -H "Content-Type: application/json" \
  -d '{"tag":"07000000002b"}'
```

Optional: If you already have a fresh device token and want to override the driver's token, add `-H 'token: <YOUR_TOKEN>'` to the above commands.

## Behavior

- The driver logs into the device on startup and refreshes the token periodically.
- Each device call has timeout, retry, and exponential backoff. On errors indicating token problems or connectivity issues, the driver re-logins and retries up to the configured limits.
- A background worker maintains the token with graceful shutdown on SIGINT/SIGTERM.

## Notes

- The driver does not expose native device URLs; it proxies requests and returns the device JSON responses.
- Only the explicitly required endpoints are implemented.
