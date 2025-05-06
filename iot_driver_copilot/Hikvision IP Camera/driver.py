import os
import threading
import io
import time
from flask import Flask, Response, request, jsonify, abort
import requests
import cv2
import numpy as np

# Load configuration from environment variables
DEVICE_IP = os.environ.get("DEVICE_IP")
RTSP_PORT = int(os.environ.get("RTSP_PORT", "554"))
CAMERA_USER = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASSWORD = os.environ.get("CAMERA_PASSWORD", "")
HTTP_SERVER_HOST = os.environ.get("HTTP_SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("HTTP_SERVER_PORT", "8080"))
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "/ISAPI/Streaming/channels/101/picture")

RTSP_STREAM_PATH = os.environ.get("RTSP_STREAM_PATH", "/Streaming/Channels/101")

# Compose RTSP URL
RTSP_URL = f"rtsp://{CAMERA_USER}:{CAMERA_PASSWORD}@{DEVICE_IP}:{RTSP_PORT}{RTSP_STREAM_PATH}"

# Compose snapshot URL
SNAPSHOT_URL = f"http://{DEVICE_IP}{SNAPSHOT_PATH}"

app = Flask(__name__)

# Stream control
streaming_flags = {}
stream_locks = {}

def gen_video_stream(client_id):
    # Use OpenCV to read RTSP and stream as multipart/x-mixed-replace
    cap = cv2.VideoCapture(RTSP_URL)
    if not cap.isOpened():
        yield b"--frame\r\nContent-Type: text/plain\r\n\r\nUnable to open video stream.\r\n"
        return
    streaming_flags[client_id] = True
    try:
        while streaming_flags.get(client_id):
            ret, frame = cap.read()
            if not ret:
                continue
            # Encode frame as JPEG
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            time.sleep(0.04)  # ~25 FPS
    finally:
        cap.release()
        streaming_flags.pop(client_id, None)

@app.route('/video/stream', methods=['GET'])
def video_stream():
    client_id = request.remote_addr + ":" + str(request.environ.get("REMOTE_PORT", ""))
    if client_id in stream_locks:
        abort(429, "Already streaming for this client")
    stream_locks[client_id] = True
    streaming_flags[client_id] = True

    def stream_with_cleanup():
        try:
            for chunk in gen_video_stream(client_id):
                yield chunk
        finally:
            stream_locks.pop(client_id, None)
            streaming_flags.pop(client_id, None)

    return Response(stream_with_cleanup(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/camera/snapshot', methods=['GET'])
def camera_snapshot():
    # Try HTTP snapshot API first
    try:
        resp = requests.get(
            SNAPSHOT_URL,
            auth=(CAMERA_USER, CAMERA_PASSWORD),
            timeout=5,
            stream=True,
        )
        if resp.status_code == 200:
            data = resp.content
            return Response(data, mimetype='image/jpeg')
    except Exception:
        pass
    # Fallback: grab from RTSP stream using OpenCV
    cap = cv2.VideoCapture(RTSP_URL)
    if not cap.isOpened():
        abort(503, "Unable to open video stream for snapshot")
    ret, frame = cap.read()
    cap.release()
    if not ret:
        abort(503, "Unable to capture frame")
    ret, jpeg = cv2.imencode('.jpg', frame)
    if not ret:
        abort(503, "Unable to encode frame")
    return Response(jpeg.tobytes(), mimetype='image/jpeg')

@app.route('/video/stream/stop', methods=['POST'])
def stop_video_stream():
    client_id = request.remote_addr + ":" + str(request.environ.get("REMOTE_PORT", ""))
    # Signal the generator to stop
    if client_id in streaming_flags:
        streaming_flags[client_id] = False
        return jsonify({"status": "stopping"}), 200
    else:
        return jsonify({"status": "not streaming"}), 200

if __name__ == '__main__':
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)