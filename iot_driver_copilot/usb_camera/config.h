#pragma once
#include <cstdlib>
#include <string>

struct Config {
    std::string http_host;
    int http_port;

    std::string uvc_device;
    int uvc_width;
    int uvc_height;
    int uvc_fps;

    int read_timeout_ms;
    int retry_base_ms;
    int retry_max_ms;

    std::string stream_boundary;
    std::string log_level;

    static bool loadFromEnv(Config &out) {
        // Helper lambdas
        auto getStr = [](const char *name, std::string &dst) -> bool {
            const char *v = std::getenv(name);
            if (!v) return false;
            dst = v;
            return true;
        };
        auto getInt = [](const char *name, int &dst) -> bool {
            const char *v = std::getenv(name);
            if (!v) return false;
            char *end = nullptr;
            long val = std::strtol(v, &end, 10);
            if (end == v || *end != '\0') return false;
            dst = (int)val;
            return true;
        };

        bool ok = true;
        ok = ok && getStr("HTTP_HOST", out.http_host);
        ok = ok && getInt("HTTP_PORT", out.http_port);
        ok = ok && getStr("UVC_DEVICE", out.uvc_device);
        ok = ok && getInt("UVC_WIDTH", out.uvc_width);
        ok = ok && getInt("UVC_HEIGHT", out.uvc_height);
        ok = ok && getInt("UVC_FPS", out.uvc_fps);
        ok = ok && getInt("READ_TIMEOUT_MS", out.read_timeout_ms);
        ok = ok && getInt("RETRY_BASE_MS", out.retry_base_ms);
        ok = ok && getInt("RETRY_MAX_MS", out.retry_max_ms);
        ok = ok && getStr("STREAM_BOUNDARY", out.stream_boundary);
        ok = ok && getStr("LOG_LEVEL", out.log_level);

        return ok;
    }
};
