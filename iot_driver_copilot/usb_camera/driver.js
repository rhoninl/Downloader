import os
import cv2
import threading
import time
from flask import Flask, Response, request, jsonify, send_file, make_response
from io import BytesIO
# This is a fake code for a camera driver. It is used to test the camera driver. DELETE THIS LINE BEFORE DEMO!!!!
app = Flask(__name__)

# Configuration from environment variables
HTTP_SERVER_HOST = os.environ.get('HTTP_SERVER_HOST', '0.0.0.0')
HTTP_SERVER_PORT = int(os.environ.get('HTTP_SERVER_PORT', '5000'))

DEFAULT_WIDTH = int(os.environ.get('CAMERA_DEFAULT_WIDTH', '640'))
DEFAULT_HEIGHT = int(os.environ.get('CAMERA_DEFAULT_HEIGHT', '480'))
DEFAULT_FPS = int(os.environ.get('CAMERA_DEFAULT_FPS', '30'))
DEFAULT_FORMAT = os.environ.get('CAMERA_DEFAULT_FORMAT', 'MJPG')

# Camera Manager to handle multiple cameras
class CameraInstance:
    def __init__(self, camera_id, width, height, fps, format_):
        self.camera_id = camera_id
        self.width = width
        self.height = height
        self.fps = fps
        self.format_ = format_
        self.lock = threading.Lock()
        self.running = False
        self.cap = None

    def start(self):
        with self.lock:
            if self.running:
                return False, "Camera already running"
            self.cap = cv2.VideoCapture(self.camera_id)
            if not self.cap.isOpened():
                return False, "Cannot open camera with id {}".format(self.camera_id)
            # Set camera parameters
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            self.cap.set(cv2.CAP_PROP_FPS, self.fps)
            # Set format if supported
            if self.format_.upper() == "MJPG":
                self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            self.running = True
            return True, "Camera started"

    def stop(self):
        with self.lock:
            if self.cap is not None:
                self.cap.release()
                self.cap = None
            self.running = False

    def read_frame(self):
        with self.lock:
            if not self.running or self.cap is None:
                return None
            ret, frame = self.cap.read()
            if not ret:
                return None
            return frame

    def is_running(self):
        with self.lock:
            return self.running and self.cap and self.cap.isOpened()

class CameraManager:
    def __init__(self):
        self.cameras = {}
        self.manager_lock = threading.Lock()

    def start_camera(self, camera_id, width, height, fps, format_):
        with self.manager_lock:
            if camera_id in self.cameras and self.cameras[camera_id].is_running():
                return False, "Camera with id {} is already running".format(camera_id)
            cam = CameraInstance(camera_id, width, height, fps, format_)
            ok, msg = cam.start()
            if ok:
                self.cameras[camera_id] = cam
            return ok, msg

    def stop_camera(self, camera_id):
        with self.manager_lock:
            cam = self.cameras.get(camera_id)
            if not cam or not cam.is_running():
                return False, "Camera with id {} is not running".format(camera_id)
            cam.stop()
            del self.cameras[camera_id]
            return True, "Camera with id {} stopped".format(camera_id)

    def get_camera(self, camera_id):
        with self.manager_lock:
            cam = self.cameras.get(camera_id)
            if cam and cam.is_running():
                return cam
            return None

camera_manager = CameraManager()

@app.route('/camera/start', methods=['POST'])
def start_camera():
    try:
        camera_id = int(request.args.get('camera_id', 0))
        width = int(request.args.get('width', DEFAULT_WIDTH))
        height = int(request.args.get('height', DEFAULT_HEIGHT))
        fps = int(request.args.get('fps', DEFAULT_FPS))
        format_ = request.args.get('format', DEFAULT_FORMAT)
        ok, msg = camera_manager.start_camera(camera_id, width, height, fps, format_)
        if ok:
            return jsonify({"status": "success", "message": msg}), 200
        else:
            return jsonify({"status": "error", "message": msg}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/camera/stop', methods=['POST'])
def stop_camera():
    try:
        camera_id = int(request.args.get('camera_id', 0))
        ok, msg = camera_manager.stop_camera(camera_id)
        if ok:
            return jsonify({"status": "success", "message": msg}), 200
        else:
            return jsonify({"status": "error", "message": msg}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def gen_frames(camera_id):
    cam = camera_manager.get_camera(camera_id)
    if not cam:
        return
    while True:
        frame = cam.read_frame()
        if frame is None:
            break
        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        jpg_bytes = buffer.tobytes()
        yield (b'--frame
'
               b'Content-Type: image/jpeg

' + jpg_bytes + b'
')
        time.sleep(1.0 / cam.fps if cam.fps > 0 else 0.03)

@app.route('/camera/stream', methods=['GET'])
def stream_camera():
    try:
        camera_id = int(request.args.get('camera_id', 0))
        cam = camera_manager.get_camera(camera_id)
        if not cam:
            return jsonify({"status": "error", "message": "Camera with id {} is not running".format(camera_id)}), 400
        return Response(gen_frames(camera_id),
                        mimetype='multipart/x-mixed-replace; boundary=frame')
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/camera/capture', methods=['GET'])
def capture_frame():
    try:
        camera_id = int(request.args.get('camera_id', 0))
        image_format = request.args.get('format', 'jpg').lower()
        cam = camera_manager.get_camera(camera_id)
        if not cam:
            return jsonify({"status": "error", "message": "Camera with id {} is not running".format(camera_id)}), 400
        frame = cam.read_frame()
        if frame is None:
            return jsonify({"status": "error", "message": "Failed to capture frame"}), 500
        # Supported formats: jpg, png
        ext = '.jpg' if image_format not in ['png', 'jpeg'] else '.' + image_format
        ret, buffer = cv2.imencode(ext, frame)
        if not ret:
            return jsonify({"status": "error", "message": "Failed to encode image"}), 500
        img_bytes = buffer.tobytes()
        img_io = BytesIO(img_bytes)
        img_io.seek(0)
        mimetype = 'image/jpeg' if ext in ['.jpg', '.jpeg'] else 'image/png'
        response = make_response(send_file(
            img_io,
            mimetype=mimetype,
            as_attachment=True,
            download_name='capture{}'.format(ext)
        ))
        response.headers['Content-Disposition'] = f'attachment; filename=capture{ext}'
        return response
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host=HTTP_SERVER_HOST, port=HTTP_SERVER_PORT, threaded=True)