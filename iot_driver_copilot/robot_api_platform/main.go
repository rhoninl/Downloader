// main.go
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

type DriverStatus struct {
	UptimeSec         int64        `json:"uptime_sec"`
	StartTimeISO      string       `json:"start_time_iso"`
	DeviceBaseURL     string       `json:"device_base_url"`
	LastError         string       `json:"last_error,omitempty"`
	TotalPings        uint64       `json:"total_pings"`
	SuccessfulPings   uint64       `json:"successful_pings"`
	FailedPings       uint64       `json:"failed_pings"`
	LastPing          *PingResult  `json:"last_ping,omitempty"`
	LastCommand       *CommandInfo `json:"last_command,omitempty"`
	BackoffCurrentMS  int          `json:"backoff_current_ms"`
	PollIntervalMS    int          `json:"poll_interval_ms"`
	DeviceTimeoutMS   int          `json:"device_timeout_ms"`
}

type CommandInfo struct {
	Name        string  `json:"name"`
	Velocity    float64 `json:"velocity"`
	WhenISO     string  `json:"when_iso"`
	HTTPStatus  int     `json:"http_status"`
	ResultOK    bool    `json:"ok"`
	DeviceBody  string  `json:"device_body,omitempty"`
	Error       string  `json:"error,omitempty"`
}

type State struct {
	cfg            *Config
	client         *DeviceClient
	startTime      time.Time
	sseHub         *SSEHub
	latestMu       sync.RWMutex
	latestPing     *PingResult
	seq            uint64
	lastErrorMu    sync.RWMutex
	lastError      string
	metricsMu      sync.RWMutex
	totalPings     uint64
	successPings   uint64
	failedPings    uint64
	backoffCurrent int
	lastCommand    *CommandInfo
}

func main() {
	// Load configuration
	cfg := LoadConfig()
	sanitizeAndLogConfig(cfg)

	st := &State{
		cfg:       cfg,
		client:    NewDeviceClient(cfg),
		startTime: time.Now(),
		sseHub:    NewSSEHub(),
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Start background poller
	go st.poller(ctx)

	mux := http.NewServeMux()
	mux.HandleFunc("/status", st.handleStatus)
	mux.HandleFunc("/snapshot", st.handleSnapshot)
	mux.HandleFunc("/stream", st.handleStream)
	mux.HandleFunc("/move/forward", st.handleMoveForward)
	mux.HandleFunc("/move/backward", st.handleMoveBackward)

	server := &http.Server{
		Addr:    fmt.Sprintf("%s:%d", cfg.HTTPHost, cfg.HTTPPort),
		Handler: logRequests(mux),
	}

	// Graceful shutdown
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		sig := <-sigCh
		log.Printf("received signal: %v, shutting down", sig)
		shutdownCtx, cancel2 := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel2()
		_ = server.Shutdown(shutdownCtx)
		cancel()
	}()

	log.Printf("HTTP server listening on %s:%d", cfg.HTTPHost, cfg.HTTPPort)
	if err := server.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server error: %v", err)
	}
}

func (s *State) poller(ctx context.Context) {
	cfg := s.cfg
	interval := time.Duration(cfg.PollIntervalMS) * time.Millisecond
	minBackoff := time.Duration(cfg.RetryBackoffMinMS) * time.Millisecond
	maxBackoff := time.Duration(cfg.RetryBackoffMaxMS) * time.Millisecond
	backoff := minBackoff

	for {
		select {
		case <-ctx.Done():
			log.Printf("poller stopped")
			return
		default:
		}

		pr, err := s.client.Ping(ctx)
		seq := atomic.AddUint64(&s.seq, 1)
		pr.Seq = seq
		if err != nil || !pr.OK {
			// record error
			s.setLastError(pr.Error)
			s.metricsAdd(false)
			s.setBackoff(int(backoff / time.Millisecond))
			s.setLatest(&pr)
			// broadcast
			s.broadcastPing(pr)
			// backoff
			timer := time.NewTimer(backoff)
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
			backoff = nextBackoff(backoff, minBackoff, maxBackoff)
			continue
		}

		// success
		s.metricsAdd(true)
		s.setBackoff(0)
		s.setLatest(&pr)
		s.broadcastPing(pr)

		// wait for interval
		select {
		case <-ctx.Done():
			return
		case <-time.After(interval):
		}
		backoff = minBackoff
	}
}

func nextBackoff(cur, min, max time.Duration) time.Duration {
	n := cur * 2
	if n < min {
		return min
	}
	if n > max {
		return max
	}
	return n
}

func (s *State) setLatest(pr *PingResult) {
	s.latestMu.Lock()
	defer s.latestMu.Unlock()
	s.latestPing = pr
}

func (s *State) getLatest() *PingResult {
	s.latestMu.RLock()
	defer s.latestMu.RUnlock()
	return s.latestPing
}

func (s *State) setLastError(e string) {
	s.lastErrorMu.Lock()
	defer s.lastErrorMu.Unlock()
	s.lastError = e
}

func (s *State) getLastError() string {
	s.lastErrorMu.RLock()
	defer s.lastErrorMu.RUnlock()
	return s.lastError
}

func (s *State) metricsAdd(success bool) {
	s.metricsMu.Lock()
	defer s.metricsMu.Unlock()
	s.totalPings++
	if success {
		s.successPings++
	} else {
		s.failedPings++
	}
}

func (s *State) snapshotStatus() DriverStatus {
	var last *PingResult
	if lp := s.getLatest(); lp != nil {
		// copy
		v := *lp
		last = &v
	}
	var lc *CommandInfo
	if s.lastCommand != nil {
		v := *s.lastCommand
		lc = &v
	}
	s.metricsMu.RLock()
	total := s.totalPings
	succ := s.successPings
	fail := s.failedPings
	s.metricsMu.RUnlock()
	return DriverStatus{
		UptimeSec:        int64(time.Since(s.startTime).Seconds()),
		StartTimeISO:     s.startTime.UTC().Format(time.RFC3339Nano),
		DeviceBaseURL:    sanitizeURL(s.cfg.DeviceBaseURL),
		LastError:        s.getLastError(),
		TotalPings:       total,
		SuccessfulPings:  succ,
		FailedPings:      fail,
		LastPing:         last,
		LastCommand:      lc,
		BackoffCurrentMS: s.getBackoff(),
		PollIntervalMS:   s.cfg.PollIntervalMS,
		DeviceTimeoutMS:  s.cfg.DeviceTimeoutMS,
	}
}

func sanitizeURL(u string) string {
	// remove credentials if any
	if strings.Contains(u, "@") {
		parts := strings.Split(u, "@")
		return "***:***@" + parts[len(parts)-1]
	}
	return u
}

func (s *State) getBackoff() int {
	s.metricsMu.RLock()
	defer s.metricsMu.RUnlock()
	return s.backoffCurrent
}

func (s *State) setBackoff(ms int) {
	s.metricsMu.Lock()
	defer s.metricsMu.Unlock()
	s.backoffCurrent = ms
}

func (s *State) broadcastPing(pr PingResult) {
	b, _ := json.Marshal(pr)
	s.sseHub.Broadcast(b)
}

func (s *State) handleStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	st := s.snapshotStatus()
	w.Header().Set("Content-Type", "application/json")
	enc := json.NewEncoder(w)
	enc.SetIndent("", " ")
	_ = enc.Encode(st)
}

func (s *State) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	lp := s.getLatest()
	if lp == nil {
		w.WriteHeader(http.StatusServiceUnavailable)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"error":"no data yet"}`))
		return
	}
	w.Header().Set("Content-Type", "application/json")
	enc := json.NewEncoder(w)
	enc.SetIndent("", " ")
	_ = enc.Encode(lp)
}

func (s *State) handleStream(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	w.Header().Set("X-Accel-Buffering", "no")
	if _, ok := w.(http.Flusher); !ok {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("streaming unsupported"))
		return
	}
	ch := s.sseHub.Register()
	defer s.sseHub.Unregister(ch)

	// send current snapshot immediately if present
	if lp := s.getLatest(); lp != nil {
		b, _ := json.Marshal(lp)
		_ = writeSSE(w, b)
	}

	notify := w.(http.CloseNotifier).CloseNotify()
	for {
		select {
		case <-notify:
			return
		case b := <-ch:
			if err := writeSSE(w, b); err != nil {
				return
			}
		}
	}
}

func (s *State) handleMoveForward(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	vel, err := parseVelocity(r, s.cfg.DefaultLinearVelocity)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), time.Duration(s.cfg.DeviceTimeoutMS)*time.Millisecond)
	defer cancel()
	res, err := s.client.MoveForward(ctx, vel)
	ci := &CommandInfo{Name: "forward", Velocity: vel, WhenISO: time.Now().UTC().Format(time.RFC3339Nano), HTTPStatus: res.HTTPStatus, ResultOK: res.OK, DeviceBody: res.DeviceBody, Error: res.Error}
	if err != nil {
		log.Printf("move forward error: %v", err)
	}
	s.lastCommand = ci
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(ci)
}

func (s *State) handleMoveBackward(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	vel, err := parseVelocity(r, s.cfg.DefaultLinearVelocity)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), time.Duration(s.cfg.DeviceTimeoutMS)*time.Millisecond)
	defer cancel()
	res, err := s.client.MoveBackward(ctx, vel)
	ci := &CommandInfo{Name: "backward", Velocity: vel, WhenISO: time.Now().UTC().Format(time.RFC3339Nano), HTTPStatus: res.HTTPStatus, ResultOK: res.OK, DeviceBody: res.DeviceBody, Error: res.Error}
	if err != nil {
		log.Printf("move backward error: %v", err)
	}
	s.lastCommand = ci
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(ci)
}

func parseVelocity(r *http.Request, def float64) (float64, error) {
	q := r.URL.Query().Get("linear_velocity")
	if strings.TrimSpace(q) == "" {
		return def, nil
	}
	f, err := strconv.ParseFloat(q, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid linear_velocity: %v", err)
	}
	return f, nil
}

func logRequests(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		next.ServeHTTP(w, r)
		log.Printf("%s %s from %s in %v", r.Method, r.URL.Path, r.RemoteAddr, time.Since(start))
	})
}

func sanitizeAndLogConfig(cfg *Config) {
	maskedBase := sanitizeURL(cfg.DeviceBaseURL)
	log.Printf("config: HTTP %s:%d, device=%s, pingPath=%s, fwdPath=%s, backPath=%s, poll=%dms, timeout=%dms, backoff=[%d..%d]ms",
		cfg.HTTPHost, cfg.HTTPPort, maskedBase, cfg.DevicePingPath, cfg.DeviceForwardPath, cfg.DeviceBackwardPath, cfg.PollIntervalMS, cfg.DeviceTimeoutMS, cfg.RetryBackoffMinMS, cfg.RetryBackoffMaxMS)
}
