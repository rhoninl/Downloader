#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <netdb.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

// ========================= Config =========================
struct Config {
    std::string http_host = "0.0.0.0";
    uint16_t http_port = 8080;

    std::string camera_device = "/dev/video0";
    int camera_width = 640;
    int camera_height = 480;
    int camera_fps = 30;

    int capture_timeout_ms = 1000; // select timeout
    int retry_max_attempts = 0;     // 0 = infinite
    int backoff_initial_ms = 500;
    int backoff_max_ms = 5000;
};

static std::atomic<bool> g_shutdown{false};

static std::string now_str() {
    using namespace std::chrono;
    auto now = system_clock::now();
    auto t = system_clock::to_time_t(now);
    auto tm = *std::localtime(&t);
    auto ms = duration_cast<milliseconds>(now.time_since_epoch()) % 1000;
    std::ostringstream oss;
    oss << std::put_time(&tm, "%Y-%m-%d %H:%M:%S") << '.' << std::setw(3) << std::setfill('0') << ms.count();
    return oss.str();
}

static void logf(const char* level, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    fprintf(stderr, "[%s] [%s] ", now_str().c_str(), level);
    vfprintf(stderr, fmt, args);
    fprintf(stderr, "\n");
    va_end(args);
}

static std::string getenv_str(const char* key, const char* def) {
    const char* v = getenv(key);
    if (v && *v) return std::string(v);
    return std::string(def);
}

static int getenv_int(const char* key, int def) {
    const char* v = getenv(key);
    if (!v || !*v) return def;
    char* end = nullptr;
    long val = strtol(v, &end, 10);
    if (end == v || *end != '\0') return def;
    return (int)val;
}

static uint16_t getenv_u16(const char* key, uint16_t def) {
    const char* v = getenv(key);
    if (!v || !*v) return def;
    char* end = nullptr;
    long val = strtol(v, &end, 10);
    if (end == v || *end != '\0') return def;
    if (val < 0 || val > 65535) return def;
    return (uint16_t)val;
}

static Config load_config() {
    Config c;
    c.http_host = getenv_str("HTTP_HOST", "0.0.0.0");
    c.http_port = getenv_u16("HTTP_PORT", 8080);

    c.camera_device = getenv_str("CAMERA_DEVICE", "/dev/video0");
    c.camera_width = getenv_int("CAMERA_WIDTH", 640);
    c.camera_height = getenv_int("CAMERA_HEIGHT", 480);
    c.camera_fps = getenv_int("CAMERA_FPS", 30);

    c.capture_timeout_ms = getenv_int("CAPTURE_TIMEOUT_MS", 1000);
    c.retry_max_attempts = getenv_int("RETRY_MAX_ATTEMPTS", 0);
    c.backoff_initial_ms = getenv_int("BACKOFF_INITIAL_MS", 500);
    c.backoff_max_ms = getenv_int("BACKOFF_MAX_MS", 5000);
    return c;
}

// ========================= V4L2 Camera =========================
struct MMapBuffer {
    void* start = nullptr;
    size_t length = 0;
};

class V4L2Camera {
public:
    V4L2Camera() {}
    ~V4L2Camera() { closeDevice(); }

    bool openDevice(const std::string& dev) {
        device_ = dev;
        fd_ = ::open(dev.c_str(), O_RDWR | O_NONBLOCK, 0);
        if (fd_ < 0) {
            logf("ERROR", "open(%s) failed: %s", dev.c_str(), strerror(errno));
            return false;
        }
        struct v4l2_capability cap;
        if (xioctl(fd_, VIDIOC_QUERYCAP, &cap) == -1) {
            logf("ERROR", "VIDIOC_QUERYCAP failed: %s", strerror(errno));
            ::close(fd_);
            fd_ = -1;
            return false;
        }
        if (!(cap.capabilities & V4L2_CAP_VIDEO_CAPTURE)) {
            logf("ERROR", "%s is not a video capture device", dev.c_str());
            ::close(fd_);
            fd_ = -1;
            return false;
        }
        if (!(cap.capabilities & V4L2_CAP_STREAMING)) {
            logf("ERROR", "%s does not support streaming I/O", dev.c_str());
            ::close(fd_);
            fd_ = -1;
            return false;
        }
        return true;
    }

    bool configureFormat(int width, int height, int fps) {
        // Try MJPEG
        struct v4l2_format fmt;
        memset(&fmt, 0, sizeof(fmt));
        fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        fmt.fmt.pix.width = width;
        fmt.fmt.pix.height = height;
        fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
        fmt.fmt.pix.field = V4L2_FIELD_ANY;
        if (xioctl(fd_, VIDIOC_S_FMT, &fmt) == -1) {
            logf("ERROR", "VIDIOC_S_FMT failed: %s", strerror(errno));
            return false;
        }
        if (fmt.fmt.pix.pixelformat != V4L2_PIX_FMT_MJPEG) {
            char fourcc[5];
            fourcc[0] = (char)(fmt.fmt.pix.pixelformat & 0xFF);
            fourcc[1] = (char)((fmt.fmt.pix.pixelformat >> 8) & 0xFF);
            fourcc[2] = (char)((fmt.fmt.pix.pixelformat >> 16) & 0xFF);
            fourcc[3] = (char)((fmt.fmt.pix.pixelformat >> 24) & 0xFF);
            fourcc[4] = '\0';
            logf("ERROR", "Device does not support MJPEG; got pixel format %s. MJPEG required for HTTP streaming.", fourcc);
            return false;
        }
        width_ = fmt.fmt.pix.width;
        height_ = fmt.fmt.pix.height;

        // Set frame rate
        struct v4l2_streamparm sp;
        memset(&sp, 0, sizeof(sp));
        sp.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        sp.parm.capture.timeperframe.numerator = 1;
        sp.parm.capture.timeperframe.denominator = fps > 0 ? fps : 30;
        if (xioctl(fd_, VIDIOC_S_PARM, &sp) == -1) {
            logf("WARN", "VIDIOC_S_PARM failed: %s (continuing with default)", strerror(errno));
        }
        fps_ = sp.parm.capture.timeperframe.denominator / (sp.parm.capture.timeperframe.numerator ? sp.parm.capture.timeperframe.numerator : 1);
        return true;
    }

    bool initMMap(unsigned int buffer_count = 4) {
        struct v4l2_requestbuffers req;
        memset(&req, 0, sizeof(req));
        req.count = buffer_count;
        req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        req.memory = V4L2_MEMORY_MMAP;
        if (xioctl(fd_, VIDIOC_REQBUFS, &req) == -1) {
            logf("ERROR", "VIDIOC_REQBUFS failed: %s", strerror(errno));
            return false;
        }
        if (req.count < 2) {
            logf("ERROR", "Insufficient buffer memory on %s", device_.c_str());
            return false;
        }
        buffers_.resize(req.count);
        for (unsigned int n = 0; n < req.count; ++n) {
            struct v4l2_buffer buf;
            memset(&buf, 0, sizeof(buf));
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = n;
            if (xioctl(fd_, VIDIOC_QUERYBUF, &buf) == -1) {
                logf("ERROR", "VIDIOC_QUERYBUF failed: %s", strerror(errno));
                return false;
            }
            buffers_[n].length = buf.length;
            buffers_[n].start = mmap(NULL, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, buf.m.offset);
            if (buffers_[n].start == MAP_FAILED) {
                logf("ERROR", "mmap failed: %s", strerror(errno));
                return false;
            }
        }
        return true;
    }

    bool startStreaming() {
        for (size_t i = 0; i < buffers_.size(); ++i) {
            struct v4l2_buffer buf;
            memset(&buf, 0, sizeof(buf));
            buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            buf.memory = V4L2_MEMORY_MMAP;
            buf.index = i;
            if (xioctl(fd_, VIDIOC_QBUF, &buf) == -1) {
                logf("ERROR", "VIDIOC_QBUF failed: %s", strerror(errno));
                return false;
            }
        }
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        if (xioctl(fd_, VIDIOC_STREAMON, &type) == -1) {
            logf("ERROR", "VIDIOC_STREAMON failed: %s", strerror(errno));
            return false;
        }
        streaming_ = true;
        return true;
    }

    bool stopStreaming() {
        if (!streaming_) return true;
        enum v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        if (xioctl(fd_, VIDIOC_STREAMOFF, &type) == -1) {
            logf("WARN", "VIDIOC_STREAMOFF failed: %s", strerror(errno));
        }
        streaming_ = false;
        return true;
    }

    void closeDevice() {
        stopStreaming();
        for (auto &b : buffers_) {
            if (b.start && b.start != MAP_FAILED) munmap(b.start, b.length);
        }
        buffers_.clear();
        if (fd_ >= 0) {
            ::close(fd_);
            fd_ = -1;
        }
    }

    // Return: 1 on frame captured, 0 on timeout, -1 on error
    int captureFrame(std::vector<uint8_t>& out, int timeout_ms) {
        if (fd_ < 0) return -1;
        fd_set fds;
        FD_ZERO(&fds);
        FD_SET(fd_, &fds);
        struct timeval tv;
        tv.tv_sec = timeout_ms / 1000;
        tv.tv_usec = (timeout_ms % 1000) * 1000;
        int r = select(fd_ + 1, &fds, NULL, NULL, &tv);
        if (r == -1) {
            if (errno == EINTR) return 0;
            logf("ERROR", "select error: %s", strerror(errno));
            return -1;
        }
        if (r == 0) {
            return 0; // timeout
        }
        struct v4l2_buffer buf;
        memset(&buf, 0, sizeof(buf));
        buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        buf.memory = V4L2_MEMORY_MMAP;
        if (xioctl(fd_, VIDIOC_DQBUF, &buf) == -1) {
            if (errno == EAGAIN) return 0;
            logf("ERROR", "VIDIOC_DQBUF failed: %s", strerror(errno));
            return -1;
        }
        if (buf.index >= buffers_.size()) {
            logf("ERROR", "buffer index out of range");
            return -1;
        }
        void* src = buffers_[buf.index].start;
        size_t len = buf.bytesused;
        out.resize(len);
        memcpy(out.data(), src, len);
        if (xioctl(fd_, VIDIOC_QBUF, &buf) == -1) {
            logf("ERROR", "VIDIOC_QBUF requeue failed: %s", strerror(errno));
            return -1;
        }
        return 1;
    }

    int width() const { return width_; }
    int height() const { return height_; }
    int fps() const { return fps_; }
    const std::string& device() const { return device_; }

private:
    int fd_ = -1;
    std::string device_;
    std::vector<MMapBuffer> buffers_;
    bool streaming_ = false;
    int width_ = 0;
    int height_ = 0;
    int fps_ = 0;

    static int xioctl(int fd, int request, void* arg) {
        int r;
        do { r = ioctl(fd, request, arg); } while (r == -1 && errno == EINTR);
        return r;
    }
};

// ========================= Camera Manager =========================
class CameraManager {
public:
    explicit CameraManager(const Config& cfg) : cfg_(cfg) {}
    ~CameraManager() { stop(); }

    bool start() {
        std::lock_guard<std::mutex> lk(state_mtx_);
        if (running_) {
            logf("INFO", "Camera already running");
            return true;
        }
        stop_requested_ = false;
        worker_ = std::thread(&CameraManager::captureLoop, this);
        running_ = true;
        return true;
    }

    void stop() {
        {
            std::lock_guard<std::mutex> lk(state_mtx_);
            if (!running_) return;
            stop_requested_ = true;
        }
        if (worker_.joinable()) worker_.join();
        running_ = false;
    }

    bool isConnected() const { return connected_.load(); }

    // Latest frame copy
    bool getLatestFrame(std::vector<uint8_t>& out, uint64_t& seq_out, std::chrono::steady_clock::time_point& ts_out) {
        std::lock_guard<std::mutex> lk(frame_mtx_);
        if (frame_seq_ == 0 || latest_frame_.empty()) return false;
        out = latest_frame_;
        seq_out = frame_seq_;
        ts_out = last_update_;
        return true;
    }

    // Wait for new frame (blocking up to wait_ms)
    bool waitForNewFrame(uint64_t last_seq, std::vector<uint8_t>& out, uint64_t& seq_out, std::chrono::steady_clock::time_point& ts_out, int wait_ms) {
        std::unique_lock<std::mutex> lk(frame_mtx_);
        if (frame_seq_ == 0) {
            frame_cv_.wait_for(lk, std::chrono::milliseconds(wait_ms));
        } else {
            frame_cv_.wait_for(lk, std::chrono::milliseconds(wait_ms), [&]{ return frame_seq_ != last_seq; });
        }
        if (latest_frame_.empty() || frame_seq_ == last_seq) return false;
        out = latest_frame_;
        seq_out = frame_seq_;
        ts_out = last_update_;
        return true;
    }

    int width() const { return width_; }
    int height() const { return height_; }
    int fps() const { return fps_; }
    std::string devicePath() const { return cfg_.camera_device; }

private:
    Config cfg_;
    std::atomic<bool> connected_{false};
    std::atomic<bool> running_{false};
    std::atomic<bool> stop_requested_{false};
    std::thread worker_;

    // frame buffer
    std::mutex frame_mtx_;
    std::condition_variable frame_cv_;
    std::vector<uint8_t> latest_frame_;
    std::chrono::steady_clock::time_point last_update_{};
    uint64_t frame_seq_ = 0;

    // state
    std::mutex state_mtx_;
    int width_ = 0;
    int height_ = 0;
    int fps_ = 0;

    void captureLoop() {
        int attempts = 0;
        int backoff = cfg_.backoff_initial_ms;
        while (!stop_requested_.load() && !g_shutdown.load()) {
            V4L2Camera cam;
            if (!cam.openDevice(cfg_.camera_device)) {
                attempts++;
                if (cfg_.retry_max_attempts > 0 && attempts >= cfg_.retry_max_attempts) {
                    logf("ERROR", "Max retry attempts reached. Stopping camera thread.");
                    break;
                }
                logf("WARN", "Retrying open in %d ms (attempt %d)", backoff, attempts);
                sleep_ms(backoff);
                backoff = std::min(cfg_.backoff_max_ms, backoff * 2);
                continue;
            }
            if (!cam.configureFormat(cfg_.camera_width, cfg_.camera_height, cfg_.camera_fps)) {
                logf("ERROR", "Failed to configure format (MJPEG %dx%d @%dfps).", cfg_.camera_width, cfg_.camera_height, cfg_.camera_fps);
                attempts++;
                cam.closeDevice();
                if (cfg_.retry_max_attempts > 0 && attempts >= cfg_.retry_max_attempts) {
                    logf("ERROR", "Max retry attempts reached. Stopping camera thread.");
                    break;
                }
                logf("WARN", "Retrying configure in %d ms (attempt %d)", backoff, attempts);
                sleep_ms(backoff);
                backoff = std::min(cfg_.backoff_max_ms, backoff * 2);
                continue;
            }
            if (!cam.initMMap(4)) {
                attempts++;
                cam.closeDevice();
                if (cfg_.retry_max_attempts > 0 && attempts >= cfg_.retry_max_attempts) {
                    logf("ERROR", "Max retry attempts reached. Stopping camera thread.");
                    break;
                }
                logf("WARN", "Retrying initMMap in %d ms (attempt %d)", backoff, attempts);
                sleep_ms(backoff);
                backoff = std::min(cfg_.backoff_max_ms, backoff * 2);
                continue;
            }
            if (!cam.startStreaming()) {
                attempts++;
                cam.closeDevice();
                if (cfg_.retry_max_attempts > 0 && attempts >= cfg_.retry_max_attempts) {
                    logf("ERROR", "Max retry attempts reached. Stopping camera thread.");
                    break;
                }
                logf("WARN", "Retrying startStreaming in %d ms (attempt %d)", backoff, attempts);
                sleep_ms(backoff);
                backoff = std::min(cfg_.backoff_max_ms, backoff * 2);
                continue;
            }

            // Reset backoff on success
            attempts = 0;
            backoff = cfg_.backoff_initial_ms;
            connected_.store(true);
            width_ = cam.width();
            height_ = cam.height();
            fps_ = cam.fps();
            logf("INFO", "Camera connected: %s %dx%d @%dfps", cfg_.camera_device.c_str(), width_, height_, fps_);

            while (!stop_requested_.load() && !g_shutdown.load()) {
                std::vector<uint8_t> frame;
                int rc = cam.captureFrame(frame, cfg_.capture_timeout_ms);
                if (rc == 1) {
                    {
                        std::lock_guard<std::mutex> lk(frame_mtx_);
                        latest_frame_.swap(frame);
                        last_update_ = std::chrono::steady_clock::now();
                        frame_seq_++;
                    }
                    frame_cv_.notify_all();
                } else if (rc == 0) {
                    // timeout; keep looping
                    continue;
                } else {
                    logf("ERROR", "Capture error; attempting reconnect.");
                    break; // will reconnect
                }
            }

            cam.stopStreaming();
            cam.closeDevice();
            connected_.store(false);
            logf("WARN", "Camera disconnected");
            // Exponential backoff before reconnect
            sleep_ms(backoff);
            backoff = std::min(cfg_.backoff_max_ms, backoff * 2);
        }
        logf("INFO", "Camera thread exit");
    }

    static void sleep_ms(int ms) {
        std::this_thread::sleep_for(std::chrono::milliseconds(ms));
    }
};

// ========================= HTTP Server =========================
static bool send_all(int fd, const void* data, size_t len) {
    const char* p = (const char*)data;
    while (len > 0) {
        ssize_t n = ::send(fd, p, len, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        p += n;
        len -= (size_t)n;
    }
    return true;
}

static bool recv_until(int fd, std::string& out, const std::string& delim, size_t max_bytes = 64 * 1024) {
    out.clear();
    char buf[1024];
    while (out.find(delim) == std::string::npos) {
        ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (n == 0) return false;
        out.append(buf, buf + n);
        if (out.size() > max_bytes) return false;
    }
    return true;
}

class HttpServer {
public:
    HttpServer(const Config& cfg, CameraManager& cam) : cfg_(cfg), cam_(cam) {}

    bool start() {
        struct addrinfo hints; memset(&hints, 0, sizeof(hints));
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_flags = AI_PASSIVE;

        std::string port_str = std::to_string(cfg_.http_port);
        struct addrinfo* res = nullptr;
        int r = getaddrinfo(cfg_.http_host.c_str(), port_str.c_str(), &hints, &res);
        if (r != 0) {
            logf("ERROR", "getaddrinfo: %s", gai_strerror(r));
            return false;
        }
        int sfd = -1;
        for (auto p = res; p != nullptr; p = p->ai_next) {
            sfd = socket(p->ai_family, p->ai_socktype, p->ai_protocol);
            if (sfd < 0) continue;
            int yes = 1;
            setsockopt(sfd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
            if (bind(sfd, p->ai_addr, p->ai_addrlen) == 0) {
                if (listen(sfd, 16) == 0) {
                    listen_fd_ = sfd;
                    freeaddrinfo(res);
                    logf("INFO", "HTTP server listening on %s:%u", cfg_.http_host.c_str(), cfg_.http_port);
                    accept_thread_ = std::thread(&HttpServer::acceptLoop, this);
                    return true;
                }
            }
            ::close(sfd);
        }
        freeaddrinfo(res);
        logf("ERROR", "Failed to bind/listen on %s:%u", cfg_.http_host.c_str(), cfg_.http_port);
        return false;
    }

    void stop() {
        stopping_.store(true);
        if (listen_fd_ >= 0) {
            ::shutdown(listen_fd_, SHUT_RDWR);
            ::close(listen_fd_);
            listen_fd_ = -1;
        }
        if (accept_thread_.joinable()) accept_thread_.join();
        // Join client threads
        for (auto& t : client_threads_) {
            if (t.joinable()) t.join();
        }
        client_threads_.clear();
        logf("INFO", "HTTP server stopped");
    }

private:
    Config cfg_;
    CameraManager& cam_;
    int listen_fd_ = -1;
    std::atomic<bool> stopping_{false};
    std::thread accept_thread_;
    std::vector<std::thread> client_threads_;

    void acceptLoop() {
        while (!stopping_.load() && !g_shutdown.load()) {
            struct sockaddr_storage addr; socklen_t addrlen = sizeof(addr);
            int cfd = accept(listen_fd_, (struct sockaddr*)&addr, &addrlen);
            if (cfd < 0) {
                if (errno == EINTR) continue;
                if (stopping_.load() || g_shutdown.load()) break;
                logf("WARN", "accept failed: %s", strerror(errno));
                continue;
            }
            client_threads_.emplace_back(&HttpServer::handleClient, this, cfd);
        }
        logf("INFO", "Accept loop exit");
    }

    void handleClient(int cfd) {
        // Read request headers
        std::string raw;
        if (!recv_until(cfd, raw, "\r\n\r\n")) {
            ::close(cfd);
            return;
        }
        // Parse request line
        std::istringstream ss(raw);
        std::string line;
        std::getline(ss, line);
        if (!line.empty() && line.back() == '\r') line.pop_back();
        std::string method, path, version;
        {
            std::istringstream ls(line);
            ls >> method >> path >> version;
        }
        // Simple routing
        if (method == "POST" && path == "/connect") {
            handleConnect(cfd);
        } else if (method == "GET" && path == "/stream") {
            handleStream(cfd);
        } else {
            std::string body = "{\"error\":\"not_found\"}";
            std::ostringstream hdr;
            hdr << "HTTP/1.1 404 Not Found\r\n";
            hdr << "Content-Type: application/json\r\n";
            hdr << "Content-Length: " << body.size() << "\r\n";
            hdr << "Connection: close\r\n\r\n";
            send_all(cfd, hdr.str().c_str(), hdr.str().size());
            send_all(cfd, body.c_str(), body.size());
            ::close(cfd);
        }
    }

    void handleConnect(int cfd) {
        cam_.start();
        // Wait briefly to see if it becomes connected
        int wait_ms = 1000;
        auto start = std::chrono::steady_clock::now();
        while (!cam_.isConnected() && std::chrono::steady_clock::now() - start < std::chrono::milliseconds(wait_ms)) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
        std::ostringstream body;
        if (cam_.isConnected()) {
            body << "{\"status\":\"connected\",\"device\":\"" << cam_.devicePath() << "\",";
            body << "\"width\":" << cam_.width() << ",\"height\":" << cam_.height() << ",\"fps\":" << cam_.fps() << "}";
        } else {
            body << "{\"status\":\"connecting\",\"device\":\"" << cam_.devicePath() << "\"}";
        }
        std::string body_str = body.str();
        std::ostringstream hdr;
        hdr << "HTTP/1.1 200 OK\r\n";
        hdr << "Content-Type: application/json\r\n";
        hdr << "Content-Length: " << body_str.size() << "\r\n";
        hdr << "Connection: close\r\n\r\n";
        send_all(cfd, hdr.str().c_str(), hdr.str().size());
        send_all(cfd, body_str.c_str(), body_str.size());
        ::close(cfd);
        logf("INFO", "POST /connect responded: %s", body_str.c_str());
    }

    void handleStream(int cfd) {
        if (!cam_.isConnected()) {
            std::string body = "{\"error\":\"device_not_connected\"}";
            std::ostringstream hdr;
            hdr << "HTTP/1.1 503 Service Unavailable\r\n";
            hdr << "Content-Type: application/json\r\n";
            hdr << "Content-Length: " << body.size() << "\r\n";
            hdr << "Connection: close\r\n\r\n";
            send_all(cfd, hdr.str().c_str(), hdr.str().size());
            send_all(cfd, body.c_str(), body.size());
            ::close(cfd);
            return;
        }
        std::ostringstream hdr;
        hdr << "HTTP/1.1 200 OK\r\n";
        hdr << "Cache-Control: no-cache, no-store, must-revalidate\r\n";
        hdr << "Pragma: no-cache\r\n";
        hdr << "Expires: 0\r\n";
        hdr << "Connection: close\r\n";
        hdr << "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n";
        if (!send_all(cfd, hdr.str().c_str(), hdr.str().size())) {
            ::close(cfd);
            return;
        }
        logf("INFO", "Client started GET /stream");
        uint64_t last_seq = 0;
        while (!g_shutdown.load()) {
            std::vector<uint8_t> frame;
            uint64_t seq; std::chrono::steady_clock::time_point ts;
            bool got = cam_.waitForNewFrame(last_seq, frame, seq, ts, 1000);
            if (!got) {
                // If not updated in timeout, try to send last known frame (if any)
                if (last_seq == 0) continue; // no frame yet
                // else continue waiting
                continue;
            }
            last_seq = seq;
            // Format timestamp
            auto now_sys = std::chrono::system_clock::now() + (ts - std::chrono::steady_clock::now());
            auto t = std::chrono::system_clock::to_time_t(now_sys);
            auto tm = *std::localtime(&t);
            char tsbuf[64];
            strftime(tsbuf, sizeof(tsbuf), "%Y-%m-%d %H:%M:%S", &tm);
            // Build part headers
            std::ostringstream part;
            part << "--frame\r\n";
            part << "Content-Type: image/jpeg\r\n";
            part << "Content-Length: " << frame.size() << "\r\n";
            part << "X-Frame-Seq: " << seq << "\r\n";
            part << "X-Timestamp: " << tsbuf << "\r\n\r\n";
            if (!send_all(cfd, part.str().c_str(), part.str().size())) break;
            if (!send_all(cfd, frame.data(), frame.size())) break;
            static const char* crlf = "\r\n";
            if (!send_all(cfd, crlf, 2)) break;
        }
        ::close(cfd);
        logf("INFO", "Client closed GET /stream");
    }
};

// ========================= Signal Handling =========================
static void on_signal(int signo) {
    (void)signo;
    g_shutdown.store(true);
}

// ========================= Main =========================
int main() {
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    Config cfg = load_config();
    logf("INFO", "Starting USB camera HTTP driver");
    logf("INFO", "HTTP_HOST=%s HTTP_PORT=%u", cfg.http_host.c_str(), cfg.http_port);
    logf("INFO", "CAMERA_DEVICE=%s WIDTH=%d HEIGHT=%d FPS=%d", cfg.camera_device.c_str(), cfg.camera_width, cfg.camera_height, cfg.camera_fps);

    CameraManager cam(cfg);
    HttpServer server(cfg, cam);
    if (!server.start()) {
        logf("ERROR", "Failed to start HTTP server");
        return 1;
    }

    // Run until shutdown
    while (!g_shutdown.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    logf("INFO", "Shutting down...");
    server.stop();
    cam.stop();
    logf("INFO", "Exited cleanly");
    return 0;
}
