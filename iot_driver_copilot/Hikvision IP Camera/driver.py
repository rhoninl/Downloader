import os
import threading
import time
import io
from flask import Flask, Response, stream_with_context, request, jsonify, abort
import cv2
import requests
from requests.auth import HTTPDigestAuth, HTTPBasicAuth

app = Flask(__name__)

# Environment variables for configuration
CAMERA_IP = os.environ.get('CAMERA_IP', '192.168.1.64')
CAMERA_RTSP_PORT = int(os.environ.get('CAMERA_RTSP_PORT', '554'))
CAMERA_HTTP_PORT = int(os.environ.get('CAMERA_HTTP_PORT', '80'))
CAMERA_USERNAME = os.environ.get('CAMERA_USERNAME', 'admin')
CAMERA_PASSWORD = os.environ.get('CAMERA_PASSWORD', '12345')
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', '8080'))
RTSP_PATH = os.environ.get('RTSP_PATH', '/Streaming/Channels/101')
SNAPSHOT_PATH = os.environ.get('SNAPSHOT_PATH', '/ISAPI/Streaming/channels/101/picture')
AUTH_TYPE = os.environ.get('AUTH_TYPE', 'digest').lower()  # "digest" or "basic"

# RTSP URL construction
RTSP_URL = f"rtsp://{CAMERA_USERNAME}:{CAMERA_PASSWORD}@{CAMERA_IP}:{CAMERA_RTSP_PORT}{RTSP_PATH}"

# HTTP snapshot URL construction
SNAPSHOT_URL = f"http://{CAMERA_IP}:{CAMERA_HTTP_PORT}{SNAPSHOT_PATH}"

# Global control flag for stream
stream_active = threading.Event()
stream_active.clear()

# Video stream generator
def gen_video_stream():
    global stream_active
    cap = cv2.VideoCapture(RTSP_URL)
    if not cap.isOpened():
        yield b''
        cap.release()
        return

    stream_active.set()
    try:
        boundary = "--frame"
        while stream_active.is_set():
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue
            # Encode frame as JPEG
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = jpeg.tobytes()
            yield (b'%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n' % (boundary.encode(), len(frame_bytes)))
            yield frame_bytes
            yield b'\r\n'
            # Control frame rate
            time.sleep(0.04)  # ~25fps
    finally:
        cap.release()
        stream_active.clear()

@app.route('/video/stream', methods=['GET'])
def video_stream():
    # Set the stream active flag
    if stream_active.is_set():
        # Only one stream at a time, reject additional requests
        return jsonify({'error': 'Stream already active'}), 409
    stream_active.set()
    return Response(
        stream_with_context(gen_video_stream()),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.route('/video/stream/stop', methods=['POST'])
def stop_video_stream():
    if stream_active.is_set():
        stream_active.clear()
        return jsonify({'status': 'stream stopping'})
    else:
        return jsonify({'status': 'no active stream'}), 400

@app.route('/camera/snapshot', methods=['GET'])
def camera_snapshot():
    # Choose auth method
    if AUTH_TYPE == 'digest':
        auth = HTTPDigestAuth(CAMERA_USERNAME, CAMERA_PASSWORD)
    else:
        auth = HTTPBasicAuth(CAMERA_USERNAME, CAMERA_PASSWORD)
    try:
        resp = requests.get(SNAPSHOT_URL, auth=auth, timeout=10, stream=True)
        if resp.status_code == 200 and resp.headers.get('Content-Type', '').startswith('image/'):
            return Response(resp.content, mimetype='image/jpeg')
        else:
            return jsonify({'error': 'Failed to retrieve snapshot', 'status_code': resp.status_code}), 502
    except Exception as ex:
        return jsonify({'error': 'Snapshot request failed', 'message': str(ex)}), 500

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)