// config.go
package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"
)

type Config struct {
	HTTPHost              string
	HTTPPort              int
	DeviceBaseURL         string
	DevicePingPath        string
	DeviceForwardPath     string
	DeviceBackwardPath    string
	DeviceTimeoutMS       int
	PollIntervalMS        int
	RetryBackoffMinMS     int
	RetryBackoffMaxMS     int
	DefaultLinearVelocity float64
	BasicAuthUsername     string
	BasicAuthPassword     string
}

func getEnvMust(name string) string {
	v := strings.TrimSpace(os.Getenv(name))
	if v == "" {
		panic(fmt.Sprintf("missing required env var: %s", name))
	}
	return v
}

func getEnvIntMust(name string) int {
	v := getEnvMust(name)
	n, err := strconv.Atoi(v)
	if err != nil {
		panic(fmt.Sprintf("invalid int for %s: %v", name, err))
	}
	return n
}

func getEnvFloatMust(name string) float64 {
	v := getEnvMust(name)
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		panic(fmt.Sprintf("invalid float for %s: %v", name, err))
	}
	return f
}

func getEnvOpt(name string) string {
	return strings.TrimSpace(os.Getenv(name))
}

func LoadConfig() *Config {
	cfg := &Config{
		HTTPHost:              getEnvMust("HTTP_HOST"),
		HTTPPort:              getEnvIntMust("HTTP_PORT"),
		DeviceBaseURL:         strings.TrimRight(getEnvMust("DEVICE_BASE_URL"), "/"),
		DevicePingPath:        ensureLeadingSlash(getEnvMust("DEVICE_PING_PATH")),
		DeviceForwardPath:     ensureLeadingSlash(getEnvMust("DEVICE_FORWARD_PATH")),
		DeviceBackwardPath:    ensureLeadingSlash(getEnvMust("DEVICE_BACKWARD_PATH")),
		DeviceTimeoutMS:       getEnvIntMust("DEVICE_TIMEOUT_MS"),
		PollIntervalMS:        getEnvIntMust("POLL_INTERVAL_MS"),
		RetryBackoffMinMS:     getEnvIntMust("RETRY_BACKOFF_MS_MIN"),
		RetryBackoffMaxMS:     getEnvIntMust("RETRY_BACKOFF_MS_MAX"),
		DefaultLinearVelocity: getEnvFloatMust("DEFAULT_LINEAR_VELOCITY"),
		BasicAuthUsername:     getEnvOpt("BASIC_AUTH_USERNAME"),
		BasicAuthPassword:     getEnvOpt("BASIC_AUTH_PASSWORD"),
	}
	return cfg
}

func ensureLeadingSlash(p string) string {
	if p == "" {
		return "/"
	}
	if !strings.HasPrefix(p, "/") {
		return "/" + p
	}
	return p
}
