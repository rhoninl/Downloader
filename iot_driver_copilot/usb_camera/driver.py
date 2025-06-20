import os
import cv2
from flask import Flask, Response, jsonify, request

# Environment variable configuration
CAMERA_INDEX = int(os.environ.get("DEVICE_USB_CAMERA_INDEX", 0))
HTTP_SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
HTTP_SERVER_PORT = int(os.environ.get("SERVER_PORT", 8080))

app = Flask(__name__)

camera = None
is_streaming = False

def get_camera():
    global camera
    if camera is None or not camera.isOpened():
        camera = cv2.VideoCapture(CAMERA_INDEX)
    return camera

def release_camera():
    global camera
    if camera is not None and camera.isOpened():
        camera.release()
        camera = None

def gen_mjpeg_stream():
    global is_streaming
    cap = get_camera()
    while is_streaming and cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        ret, jpeg = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + jpeg.tobytes() + b'\r\n')
    release_camera()

@app.route('/camera/start', methods=['POST'])
def start_stream():
    global is_streaming
    if not is_streaming:
        cap = get_camera()
        if not cap.isOpened():
            return jsonify({"success": False, "message": "Camera could not be opened."}), 500
        is_streaming = True
        return jsonify({"success": True, "message": "Camera streaming started."})
    else:
        return jsonify({"success": True, "message": "Camera already streaming."})

@app.route('/camera/stop', methods=['POST'])
def stop_stream():
    global is_streaming
    if is_streaming:
        is_streaming = False
        release_camera()
        return jsonify({"success": True, "message": "Camera streaming stopped."})
    else:
        return jsonify({"success": True, "message": "Camera is not streaming."})

@app.route('/camera/stream', methods=['GET'])
def video_stream():
    global is_streaming
    if not is_streaming:
        return jsonify({"success": False, "message": "Stream is not started. Please POST to /camera/start first."}), 400
    return Response(gen_mjpeg_stream(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

if __name__ == '__main__':
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)