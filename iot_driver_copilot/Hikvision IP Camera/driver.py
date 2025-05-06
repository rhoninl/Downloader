import os
import asyncio
from fastapi import FastAPI, Response, Request, BackgroundTasks, status
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from threading import Lock

import av
import cv2
import numpy as np

# Environment Variables
DEVICE_IP = os.getenv('DEVICE_IP', '192.168.1.64')
RTSP_PORT = int(os.getenv('DEVICE_RTSP_PORT', 554))
USERNAME = os.getenv('DEVICE_USERNAME', 'admin')
PASSWORD = os.getenv('DEVICE_PASSWORD', '12345')
RTSP_PATH = os.getenv('DEVICE_RTSP_PATH', 'Streaming/Channels/101')
HTTP_HOST = os.getenv('SERVER_HOST', '0.0.0.0')
HTTP_PORT = int(os.getenv('SERVER_PORT', 8000))

RTSP_URL = f"rtsp://{USERNAME}:{PASSWORD}@{DEVICE_IP}:{RTSP_PORT}/{RTSP_PATH}"

SNAPSHOT_PATH = os.getenv('DEVICE_SNAPSHOT_PATH', 'Streaming/channels/1/picture')
SNAPSHOT_URL = f"http://{DEVICE_IP}/ISAPI/{SNAPSHOT_PATH}"

# Video stream session management
class StreamSessionManager:
    def __init__(self):
        self.lock = Lock()
        self.active = False
        self.container = None
        self.stop_flag = False

    def start(self):
        with self.lock:
            if not self.active:
                self.stop_flag = False
                self.container = av.open(RTSP_URL)
                self.active = True

    def stop(self):
        with self.lock:
            self.stop_flag = True
            if self.container is not None:
                self.container.close()
                self.container = None
            self.active = False

    def get_container(self):
        with self.lock:
            return self.container

    def should_stop(self):
        with self.lock:
            return self.stop_flag

stream_manager = StreamSessionManager()
app = FastAPI()
last_stream_task = None

@app.get("/video/stream")
async def video_stream():
    """
    Open and retrieve a live H.264 video feed from the camera.
    The response is a multipart MJPEG stream consumable in browsers and by ffmpeg/curl.
    """
    async def mjpeg_generator():
        try:
            await run_in_threadpool(stream_manager.start)
            container = await run_in_threadpool(stream_manager.get_container)
            video_stream = next(s for s in container.streams if s.type == 'video')
            for packet in container.demux(video_stream):
                if stream_manager.should_stop():
                    break
                for frame in packet.decode():
                    img = frame.to_ndarray(format='bgr24')
                    ret, jpeg = cv2.imencode('.jpg', img)
                    if not ret:
                        continue
                    yield (
                        b'--frame\r\n'
                        b'Content-Type: image/jpeg\r\n\r\n' +
                        jpeg.tobytes() +
                        b'\r\n'
                    )
        finally:
            await run_in_threadpool(stream_manager.stop)

    headers = {
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Content-Type': 'multipart/x-mixed-replace; boundary=frame'
    }
    return StreamingResponse(mjpeg_generator(), headers=headers)

@app.post("/video/stream/stop")
async def stop_video_stream():
    """
    Stop the ongoing live video stream and release camera resources.
    """
    await run_in_threadpool(stream_manager.stop)
    return JSONResponse({"status": "stopped"}, status_code=status.HTTP_200_OK)

@app.get("/camera/snapshot")
async def camera_snapshot():
    """
    Capture a single JPEG image from the camera’s current view.
    """
    # Try RTSP first, fallback to ISAPI HTTP snapshot if enabled
    try:
        container = av.open(RTSP_URL)
        stream = next(s for s in container.streams if s.type == 'video')
        for packet in container.demux(stream):
            for frame in packet.decode():
                img = frame.to_ndarray(format='bgr24')
                ret, jpeg = cv2.imencode('.jpg', img)
                container.close()
                if ret:
                    return Response(jpeg.tobytes(), media_type="image/jpeg")
                break
        container.close()
    except Exception:
        pass

    # Optionally: try HTTP snapshot API of Hikvision (requires snapshot enabled and credentials)
    try:
        import requests
        resp = requests.get(SNAPSHOT_URL, auth=(USERNAME, PASSWORD), timeout=5)
        if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('image/jpeg'):
            return Response(resp.content, media_type="image/jpeg")
    except Exception:
        pass

    return JSONResponse({"error": "Could not retrieve snapshot"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HTTP_HOST, port=HTTP_PORT)