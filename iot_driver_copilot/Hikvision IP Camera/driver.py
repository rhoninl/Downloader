import os
import io
import threading
import time
from flask import Flask, Response, jsonify, request, stream_with_context
import cv2

# ========== Environment Variables ==========
DEVICE_IP = os.environ.get('DEVICE_IP', '192.168.1.64')
DEVICE_RTSP_PORT = int(os.environ.get('DEVICE_RTSP_PORT', 554))
DEVICE_HTTP_PORT = int(os.environ.get('DEVICE_HTTP_PORT', 80))
DEVICE_USERNAME = os.environ.get('DEVICE_USERNAME', 'admin')
DEVICE_PASSWORD = os.environ.get('DEVICE_PASSWORD', '12345')
SERVER_HOST = os.environ.get('SERVER_HOST', '0.0.0.0')
SERVER_PORT = int(os.environ.get('SERVER_PORT', 8080))
RTSP_PATH = os.environ.get('RTSP_PATH', 'Streaming/Channels/101')

# ========== RTSP URL Construction ==========
RTSP_URL = f'rtsp://{DEVICE_USERNAME}:{DEVICE_PASSWORD}@{DEVICE_IP}:{DEVICE_RTSP_PORT}/{RTSP_PATH}'

# ========== Flask App ==========
app = Flask(__name__)

# ========== State ==========
streaming_active = threading.Event()

# ========== Utilities ==========
def get_rtsp_stream():
    cap = cv2.VideoCapture(RTSP_URL)
    if not cap.isOpened():
        cap.release()
        return None
    return cap

def mjpeg_stream_generator():
    cap = get_rtsp_stream()
    if cap is None:
        yield b''
        return
    try:
        while streaming_active.is_set():
            ret, frame = cap.read()
            if not ret:
                break
            ret, jpeg = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = jpeg.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    finally:
        cap.release()

def get_single_jpeg_frame():
    cap = get_rtsp_stream()
    if cap is None:
        return None
    try:
        # Try to grab a frame in max 2 seconds
        for _ in range(40):
            ret, frame = cap.read()
            if ret:
                ret, jpeg = cv2.imencode('.jpg', frame)
                if ret:
                    return jpeg.tobytes()
            time.sleep(0.05)
    finally:
        cap.release()
    return None

# ========== API Endpoints ==========

@app.route('/live-feed', methods=['GET'])
def live_feed():
    """
    Retrieve the current video stream as HTTP MJPEG stream.
    """
    if not streaming_active.is_set():
        return jsonify({"error": "Stream is not active. Please start the stream with /live-feed/start"}), 400
    return Response(
        stream_with_context(mjpeg_stream_generator()),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.route('/snapshot', methods=['GET'])
def snapshot():
    """
    Capture a single JPEG image from the camera.
    """
    img_bytes = get_single_jpeg_frame()
    if img_bytes is None:
        return jsonify({"error": "Failed to capture snapshot"}), 500
    return Response(img_bytes, mimetype='image/jpeg')

@app.route('/live-feed/start', methods=['POST'])
def start_stream():
    """
    Command the camera to begin streaming.
    """
    streaming_active.set()
    return jsonify({
        "success": True,
        "stream_url": f"http://{SERVER_HOST}:{SERVER_PORT}/live-feed"
    })

@app.route('/live-feed/stop', methods=['POST'])
def stop_stream():
    """
    Stop the ongoing video feed.
    """
    streaming_active.clear()
    return jsonify({"success": True, "message": "Streaming stopped"})

# ========== Main ==========
if __name__ == '__main__':
    streaming_active.clear()
    app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True)