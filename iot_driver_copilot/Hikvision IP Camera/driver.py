import os
import io
import threading
import time
from flask import Flask, Response, stream_with_context, request, jsonify
import requests
import cv2
import numpy as np

# Configuration from environment variables
CAMERA_IP = os.environ.get('CAMERA_IP', '192.168.1.64')
CAMERA_RTSP_PORT = int(os.environ.get('CAMERA_RTSP_PORT', '554'))
CAMERA_USER = os.environ.get('CAMERA_USER', 'admin')
CAMERA_PASS = os.environ.get('CAMERA_PASS', '12345')
CAMERA_HTTP_PORT = int(os.environ.get('CAMERA_HTTP_PORT', '80'))

SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', '8000'))

RTSP_PATH = os.environ.get(
    'CAMERA_RTSP_PATH',
    'Streaming/Channels/101'
)
RTSP_TRANSPORT = os.environ.get('CAMERA_RTSP_TRANSPORT', 'tcp')  # tcp or udp

app = Flask(__name__)

# Shared streaming state
streaming_sessions = {}
streaming_sessions_lock = threading.Lock()

def build_rtsp_url():
    return f"rtsp://{CAMERA_USER}:{CAMERA_PASS}@{CAMERA_IP}:{CAMERA_RTSP_PORT}/{RTSP_PATH}"

def build_snapshot_url():
    return f"http://{CAMERA_IP}:{CAMERA_HTTP_PORT}/ISAPI/Streaming/channels/101/picture"

def gen_mjpeg_stream(session_id):
    # OpenCV VideoCapture for RTSP stream
    rtsp_url = build_rtsp_url()
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        yield b''
        return

    with streaming_sessions_lock:
        streaming_sessions[session_id] = cap

    try:
        while True:
            # Check for stop signal
            with streaming_sessions_lock:
                if session_id not in streaming_sessions:
                    break
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue
            # Encode as JPEG
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = jpeg.tobytes()
            # Write multipart/x-mixed-replace content
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    finally:
        cap.release()
        with streaming_sessions_lock:
            if session_id in streaming_sessions:
                del streaming_sessions[session_id]

@app.route('/video/stream', methods=['GET'])
def video_stream():
    session_id = request.args.get('session_id', str(time.time()))
    response = Response(
        stream_with_context(gen_mjpeg_stream(session_id)),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )
    response.headers['X-Session-ID'] = session_id
    return response

@app.route('/video/stream/stop', methods=['POST'])
def video_stream_stop():
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'session_id query param required'}), 400
    with streaming_sessions_lock:
        cap = streaming_sessions.get(session_id)
        if cap:
            cap.release()
            del streaming_sessions[session_id]
            return jsonify({'status': 'stream stopped'}), 200
        else:
            return jsonify({'error': 'no such stream'}), 404

@app.route('/camera/snapshot', methods=['GET'])
def camera_snapshot():
    snapshot_url = build_snapshot_url()
    try:
        resp = requests.get(
            snapshot_url,
            auth=(CAMERA_USER, CAMERA_PASS),
            timeout=5
        )
        if resp.status_code == 200 and resp.headers.get('Content-Type', '').lower().startswith('image'):
            return Response(resp.content, mimetype='image/jpeg')
        else:
            # Fallback: try to grab a frame from RTSP
            rtsp_url = build_rtsp_url()
            cap = cv2.VideoCapture(rtsp_url)
            ret, frame = cap.read()
            cap.release()
            if ret:
                ret, jpeg = cv2.imencode('.jpg', frame)
                if ret:
                    return Response(jpeg.tobytes(), mimetype='image/jpeg')
            return jsonify({'error': 'could not obtain snapshot'}), 502
    except Exception as e:
        return jsonify({'error': str(e)}), 502

if __name__ == '__main__':
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)