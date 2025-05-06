import os
import asyncio
import aiohttp
from aiohttp import web
import base64

import cv2
import av

# Environment configuration
DEVICE_IP = os.environ.get('DEVICE_IP', '192.168.1.64')
DEVICE_USER = os.environ.get('DEVICE_USER', 'admin')
DEVICE_PASS = os.environ.get('DEVICE_PASS', '12345')
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', 8080))
RTSP_PORT = os.environ.get('RTSP_PORT', '554')
SNAPSHOT_PORT = os.environ.get('SNAPSHOT_PORT', '80')

RTSP_PATH = os.environ.get('RTSP_PATH', '/Streaming/Channels/101')
SNAPSHOT_PATH = os.environ.get('SNAPSHOT_PATH', '/ISAPI/Streaming/channels/101/picture')

VIDEO_STREAM_ROUTE = '/video/stream'
VIDEO_STREAM_STOP_ROUTE = '/video/stream/stop'
SNAPSHOT_ROUTE = '/camera/snapshot'

# Shared state for stream control
streams = {}

# Helper: RTSP URL builder
def get_rtsp_url():
    return f'rtsp://{DEVICE_USER}:{DEVICE_PASS}@{DEVICE_IP}:{RTSP_PORT}{RTSP_PATH}'

# Helper: Snapshot URL builder
def get_snapshot_url():
    return f'http://{DEVICE_IP}:{SNAPSHOT_PORT}{SNAPSHOT_PATH}'

# Video Streaming Handler
async def video_stream(request):
    """
    Streams H.264 video as MJPEG over HTTP for browser compatibility.
    """
    boundary = "frame"
    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': f'multipart/x-mixed-replace; boundary=--{boundary}',
            'Cache-Control': 'no-cache',
            'Connection': 'close',
            'Pragma': 'no-cache'
        }
    )
    await response.prepare(request)

    session_id = id(request)
    streams[session_id] = True

    rtsp_url = get_rtsp_url()
    container = None
    try:
        container = av.open(rtsp_url)
        video_stream = next(s for s in container.streams if s.type == 'video')

        for frame in container.decode(video_stream):
            if not streams.get(session_id):
                break
            img = frame.to_ndarray(format='bgr24')
            ret, jpeg = cv2.imencode('.jpg', img)
            if not ret:
                continue
            data = jpeg.tobytes()
            await response.write(
                f"--{boundary}\r\n".encode() +
                b"Content-Type: image/jpeg\r\n" +
                f"Content-Length: {len(data)}\r\n\r\n".encode() +
                data + b"\r\n"
            )
            await asyncio.sleep(0.04)  # ~25fps
    except Exception:
        pass
    finally:
        if session_id in streams:
            del streams[session_id]
        if container is not None:
            container.close()
        await response.write_eof()
    return response

# Stream Stop Handler
async def video_stream_stop(request):
    """
    Stops the ongoing live video stream.
    """
    for session in list(streams.keys()):
        streams[session] = False
    return web.json_response({"status": "stopped"})

# Snapshot Handler
async def camera_snapshot(request):
    """
    Grabs a JPEG snapshot from the camera.
    """
    url = get_snapshot_url()
    auth = aiohttp.BasicAuth(DEVICE_USER, DEVICE_PASS)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, auth=auth) as resp:
            if resp.status != 200:
                return web.Response(status=500, text='Snapshot failed')
            data = await resp.read()
            return web.Response(
                body=data,
                content_type='image/jpeg',
                headers={'Cache-Control': 'no-cache'}
            )

# Application setup
app = web.Application()
app.add_routes([
    web.get(VIDEO_STREAM_ROUTE, video_stream),
    web.post(VIDEO_STREAM_STOP_ROUTE, video_stream_stop),
    web.get(SNAPSHOT_ROUTE, camera_snapshot),
])

if __name__ == '__main__':
    web.run_app(app, host=SERVER_HOST, port=SERVER_PORT)