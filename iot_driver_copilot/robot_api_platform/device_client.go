// device_client.go
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

type DeviceClient struct {
	baseURL      string
	pingPath     string
	forwardPath  string
	backwardPath string
	username     string
	password     string
	httpClient *http.Client
}

type PingResult struct {
	OK           bool   `json:"ok"`
	HTTPStatus   int    `json:"http_status"`
	RTTMillis    int64  `json:"rtt_ms"`
	Error        string `json:"error,omitempty"`
	TimestampISO string `json:"ts_iso"`
	Seq          uint64 `json:"seq"`
}

type CommandResult struct {
	OK           bool    `json:"ok"`
	Command      string  `json:"command"`
	Velocity     float64 `json:"velocity"`
	HTTPStatus   int     `json:"http_status"`
	DeviceBody   string  `json:"device_body,omitempty"`
	Error        string  `json:"error,omitempty"`
	TimestampISO string  `json:"ts_iso"`
}

func NewDeviceClient(cfg *Config) *DeviceClient {
	return &DeviceClient{
		baseURL:      strings.TrimRight(cfg.DeviceBaseURL, "/"),
		pingPath:     cfg.DevicePingPath,
		forwardPath:  cfg.DeviceForwardPath,
		backwardPath: cfg.DeviceBackwardPath,
		username:     cfg.BasicAuthUsername,
		password:     cfg.BasicAuthPassword,
		httpClient: &http.Client{Timeout: time.Duration(cfg.DeviceTimeoutMS) * time.Millisecond},
	}
}

func (c *DeviceClient) buildURL(path string, q url.Values) string {
	u := c.baseURL + path
	if q != nil {
		qs := q.Encode()
		if qs != "" {
			u += "?" + qs
		}
	}
	return u
}

func (c *DeviceClient) doRequest(ctx context.Context, method, fullURL string, body io.Reader) (*http.Response, error) {
	req, err := http.NewRequestWithContext(ctx, method, fullURL, body)
	if err != nil {
		return nil, err
	}
	if c.username != "" || c.password != "" {
		req.SetBasicAuth(c.username, c.password)
	}
	return c.httpClient.Do(req)
}

func (c *DeviceClient) Ping(ctx context.Context) (PingResult, error) {
	start := time.Now()
	fullURL := c.buildURL(c.pingPath, nil)
	resp, err := c.doRequest(ctx, http.MethodGet, fullURL, nil)
	pr := PingResult{OK: false, HTTPStatus: 0, RTTMillis: 0, Error: "", TimestampISO: time.Now().UTC().Format(time.RFC3339Nano)}
	if err != nil {
		pr.Error = err.Error()
		return pr, err
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	pr.HTTPStatus = resp.StatusCode
	pr.RTTMillis = time.Since(start).Milliseconds()
	pr.OK = resp.StatusCode >= 200 && resp.StatusCode < 400
	return pr, nil
}

func (c *DeviceClient) sendMove(ctx context.Context, path string, velocity float64) (CommandResult, error) {
	vals := url.Values{}
	vals.Set("linear_velocity", fmt.Sprintf("%g", velocity))
	fullURL := c.buildURL(path, vals)
	resp, err := c.doRequest(ctx, http.MethodGet, fullURL, nil)
	cr := CommandResult{OK: false, Command: path, Velocity: velocity, TimestampISO: time.Now().UTC().Format(time.RFC3339Nano)}
	if err != nil {
		cr.Error = err.Error()
		return cr, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
	cr.HTTPStatus = resp.StatusCode
	cr.DeviceBody = string(body)
	cr.OK = resp.StatusCode >= 200 && resp.StatusCode < 400
	return cr, nil
}

func (c *DeviceClient) MoveForward(ctx context.Context, velocity float64) (CommandResult, error) {
	return c.sendMove(ctx, c.forwardPath, velocity)
}

func (c *DeviceClient) MoveBackward(ctx context.Context, velocity float64) (CommandResult, error) {
	return c.sendMove(ctx, c.backwardPath, velocity)
}

func (pr *PingResult) ToJSON() []byte {
	b, _ := json.Marshal(pr)
	return b
}

func (cr *CommandResult) ToJSON() []byte {
	b, _ := json.Marshal(cr)
	return b
}
