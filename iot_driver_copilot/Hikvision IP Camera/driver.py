import os
import threading
import io
import time
from flask import Flask, Response, stream_with_context, request, jsonify
import cv2
import numpy as np

# Environment variables for configuration
DEVICE_IP = os.environ.get("DEVICE_IP", "192.168.1.64")
DEVICE_RTSP_PORT = int(os.environ.get("DEVICE_RTSP_PORT", 554))
DEVICE_USERNAME = os.environ.get("DEVICE_USERNAME", "admin")
DEVICE_PASSWORD = os.environ.get("DEVICE_PASSWORD", "12345")
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", 8080))
RTSP_PATH = os.environ.get("DEVICE_RTSP_PATH", "Streaming/Channels/101")
SNAPSHOT_PATH = os.environ.get("DEVICE_SNAPSHOT_PATH", "Streaming/channels/101/picture")

# RTSP url for main stream (H.264)
RTSP_URL = f"rtsp://{DEVICE_USERNAME}:{DEVICE_PASSWORD}@{DEVICE_IP}:{DEVICE_RTSP_PORT}/{RTSP_PATH}"

# HTTP snapshot url (Hikvision HTTP snapshot API)
SNAPSHOT_URL = f"http://{DEVICE_IP}/ISAPI/{SNAPSHOT_PATH}"

app = Flask(__name__)

# Control flag for stopping stream
streaming_sessions = {}

def gen_video_stream(session_id):
    cap = cv2.VideoCapture(RTSP_URL)
    if not cap.isOpened():
        yield b''
        return

    boundary = "frame"
    try:
        while streaming_sessions.get(session_id, True):
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue
            # Encode frame as JPEG
            ret2, jpeg = cv2.imencode('.jpg', frame)
            if not ret2:
                continue
            frame_bytes = jpeg.tobytes()
            yield (
                b'--' + boundary.encode() + b'\r\n'
                b'Content-Type: image/jpeg\r\n'
                b'Content-Length: ' + str(len(frame_bytes)).encode() + b'\r\n'
                b'\r\n' + frame_bytes + b'\r\n'
            )
            time.sleep(0.04)  # ~25fps
    finally:
        cap.release()
        streaming_sessions.pop(session_id, None)

@app.route('/video/stream', methods=['GET'])
def video_stream():
    # Start a new streaming session
    session_id = str(time.time()) + str(np.random.randint(0, 10000))
    streaming_sessions[session_id] = True
    boundary = "frame"
    headers = {
        'Content-Type': f'multipart/x-mixed-replace; boundary={boundary}'
    }
    return Response(stream_with_context(gen_video_stream(session_id)), headers=headers)

@app.route('/camera/snapshot', methods=['GET'])
def camera_snapshot():
    # Try to get snapshot via HTTP API if available
    import requests
    try:
        resp = requests.get(
            SNAPSHOT_URL,
            auth=(DEVICE_USERNAME, DEVICE_PASSWORD),
            timeout=4,
            stream=True
        )
        if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('image'):
            return Response(resp.content, mimetype='image/jpeg')
    except Exception:
        pass

    # Fallback: capture one frame from RTSP
    cap = cv2.VideoCapture(RTSP_URL)
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return Response("Failed to capture snapshot", status=500)
    ret2, jpeg = cv2.imencode('.jpg', frame)
    if not ret2:
        return Response("Failed to encode snapshot", status=500)
    return Response(jpeg.tobytes(), mimetype='image/jpeg')

@app.route('/video/stream/stop', methods=['POST'])
def stop_stream():
    # Stop all active streams
    stopped = 0
    for k in list(streaming_sessions.keys()):
        streaming_sessions[k] = False
        stopped += 1
    return jsonify({"message": f"Stopped {stopped} stream(s)"}), 200

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)