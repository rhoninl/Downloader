import os
import threading
import queue
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import base64
import requests
import socket

# Required environment variables:
# CAMERA_IP: Camera IP address
# CAMERA_RTSP_PORT: Camera RTSP port (default 554)
# CAMERA_HTTP_PORT: Camera HTTP port (for snapshot, default 80)
# CAMERA_USERNAME: Camera username
# CAMERA_PASSWORD: Camera password
# SERVER_HOST: HTTP server host (default "0.0.0.0")
# SERVER_PORT: HTTP server port (default 8080)
# STREAM_PATH: RTSP stream path (default "/Streaming/Channels/101")

CAMERA_IP = os.environ.get("CAMERA_IP")
CAMERA_RTSP_PORT = int(os.environ.get("CAMERA_RTSP_PORT", "554"))
CAMERA_HTTP_PORT = int(os.environ.get("CAMERA_HTTP_PORT", "80"))
CAMERA_USERNAME = os.environ.get("CAMERA_USERNAME", "admin")
CAMERA_PASSWORD = os.environ.get("CAMERA_PASSWORD", "")
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8080"))
STREAM_PATH = os.environ.get("STREAM_PATH", "/Streaming/Channels/101")

RTSP_URL = f"rtsp://{CAMERA_USERNAME}:{CAMERA_PASSWORD}@{CAMERA_IP}:{CAMERA_RTSP_PORT}{STREAM_PATH}"

SNAPSHOT_URL = f"http://{CAMERA_IP}:{CAMERA_HTTP_PORT}/ISAPI/Streaming/channels/101/picture"

STREAM_CLIENTS = {}
STREAM_THREAD = None
STREAM_QUEUE = queue.Queue(maxsize=100)
STREAM_RUNNING = threading.Event()


def get_auth_header():
    userpass = f"{CAMERA_USERNAME}:{CAMERA_PASSWORD}"
    b64 = base64.b64encode(userpass.encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {b64}"}


def open_rtsp_stream():
    # Minimal RTSP-over-TCP client for H.264 proxying
    # This implementation is basic and supports only Hikvision's simple RTSP stream
    import re

    # Connect to camera RTSP server
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect((CAMERA_IP, CAMERA_RTSP_PORT))
    cseq = 1

    def send(cmd):
        nonlocal cseq
        sock.sendall(cmd.encode("utf-8"))
        cseq += 1

    def recv():
        data = b""
        while True:
            part = sock.recv(4096)
            data += part
            if b"\r\n\r\n" in data:
                break
        return data.decode("utf-8", errors="ignore")

    session = None

    # RTSP DESCRIBE
    describe = (
        f'DESCRIBE {RTSP_URL} RTSP/1.0\r\n'
        f'CSeq: {cseq}\r\n'
        f'Authorization: Basic {base64.b64encode(f"{CAMERA_USERNAME}:{CAMERA_PASSWORD}".encode()).decode()}\r\n'
        'Accept: application/sdp\r\n'
        '\r\n'
    )
    send(describe)
    describe_resp = recv()
    if "401 Unauthorized" in describe_resp:
        sock.close()
        raise Exception("RTSP Unauthorized")
    m = re.search(r'Session: ([^\r\n]+)', describe_resp)
    if m:
        session = m.group(1).split(';')[0].strip()

    # RTSP SETUP (TCP interleaved)
    setup = (
        f'SETUP {RTSP_URL}/trackID=1 RTSP/1.0\r\n'
        f'CSeq: {cseq}\r\n'
        f'Authorization: Basic {base64.b64encode(f"{CAMERA_USERNAME}:{CAMERA_PASSWORD}".encode()).decode()}\r\n'
        'Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n'
        f'Session: {session}\r\n' if session else ''
    )
    send(setup)
    setup_resp = recv()
    m = re.search(r'Session: ([^\r\n]+)', setup_resp)
    if m:
        session = m.group(1).split(';')[0].strip()

    # RTSP PLAY
    play = (
        f'PLAY {RTSP_URL} RTSP/1.0\r\n'
        f'CSeq: {cseq}\r\n'
        f'Authorization: Basic {base64.b64encode(f"{CAMERA_USERNAME}:{CAMERA_PASSWORD}".encode()).decode()}\r\n'
        f'Session: {session}\r\n'
        '\r\n'
    )
    send(play)
    play_resp = recv()
    if "200 OK" not in play_resp:
        sock.close()
        raise Exception("RTSP PLAY failed")

    # Start reading RTP packets (RTP/RTSP over TCP: $xx size data)
    try:
        while STREAM_RUNNING.is_set():
            hdr = sock.recv(4)
            if not hdr or hdr[0] != 0x24:
                continue
            channel = hdr[1]
            size = (hdr[2] << 8) | hdr[3]
            payload = b''
            while len(payload) < size:
                chunk = sock.recv(size - len(payload))
                if not chunk:
                    break
                payload += chunk
            if channel == 0:  # Video channel
                try:
                    STREAM_QUEUE.put(payload, timeout=1)
                except queue.Full:
                    pass
    finally:
        sock.close()


def stream_worker():
    while STREAM_RUNNING.is_set():
        try:
            if not STREAM_CLIENTS:
                time.sleep(0.5)
                continue
            open_rtsp_stream()
        except Exception as e:
            time.sleep(2)


class CameraHandler(BaseHTTPRequestHandler):
    server_version = "HikvisionHTTPProxy/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/video/stream":
            self.handle_video_stream()
        elif parsed.path == "/camera/snapshot":
            self.handle_snapshot()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/video/stream/stop":
            self.handle_stop_stream()
        else:
            self.send_error(404, "Not Found")

    def handle_video_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        client_id = id(self)
        STREAM_CLIENTS[client_id] = self
        try:
            if not STREAM_RUNNING.is_set():
                STREAM_RUNNING.set()
                global STREAM_THREAD
                if STREAM_THREAD is None or not STREAM_THREAD.is_alive():
                    STREAM_THREAD = threading.Thread(target=stream_worker, daemon=True)
                    STREAM_THREAD.start()

            while True:
                try:
                    pkt = STREAM_QUEUE.get(timeout=5)
                except queue.Empty:
                    break
                # RTP payload conversion (H.264 NALUs) to fragmented MP4 is complex; 
                # for simplicity, just stream raw NALUs as video/mp4 for players like VLC.
                self.wfile.write(pkt)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            STREAM_CLIENTS.pop(client_id, None)
            if not STREAM_CLIENTS:
                STREAM_RUNNING.clear()
                while not STREAM_QUEUE.empty():
                    try:
                        STREAM_QUEUE.get_nowait()
                    except queue.Empty:
                        break

    def handle_snapshot(self):
        try:
            resp = requests.get(SNAPSHOT_URL, headers=get_auth_header(), timeout=5, stream=True)
            if resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("image/jpeg"):
                self.send_response(200)
                self.send_header("Content-Type", resp.headers["Content-Type"])
                self.send_header("Content-Length", resp.headers.get("Content-Length", ""))
                self.end_headers()
                for chunk in resp.iter_content(4096):
                    self.wfile.write(chunk)
            else:
                self.send_error(502, "Camera did not return JPEG")
        except Exception:
            self.send_error(504, "Camera snapshot timeout")

    def handle_stop_stream(self):
        client_id = id(self)
        STREAM_CLIENTS.pop(client_id, None)
        if not STREAM_CLIENTS:
            STREAM_RUNNING.clear()
            while not STREAM_QUEUE.empty():
                try:
                    STREAM_QUEUE.get_nowait()
                except queue.Empty:
                    break
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"stopped"}')

    def log_message(self, format, *args):
        pass  # Suppress default logging


def run():
    httpd = HTTPServer((SERVER_HOST, SERVER_PORT), CameraHandler)
    print(f"Serving Hikvision Camera HTTP Proxy on {SERVER_HOST}:{SERVER_PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()