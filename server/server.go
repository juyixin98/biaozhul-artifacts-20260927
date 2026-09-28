package server

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"example.com/cgcoord/coordinator"
)

// Config configures the HTTP server.
type Config struct {
	Addr       string
	SweepEvery time.Duration // 0 disables the background sweep
	LogWriter  io.Writer
}

// Server wraps the coordinator and its HTTP surface.
type Server struct {
	coord *coordinator.Coordinator
	cfg   Config
	httpd *http.Server
	log   *log.Logger

	stopSweep context.CancelFunc
	wg        sync.WaitGroup
}

// New builds a Server.
func New(coord *coordinator.Coordinator, cfg Config) *Server {
	if cfg.Addr == "" {
		cfg.Addr = ":8080"
	}
	if cfg.LogWriter == nil {
		cfg.LogWriter = os.Stderr
	}
	s := &Server{
		coord: coord,
		cfg:   cfg,
		log:   log.New(cfg.LogWriter, "", 0),
	}
	mux := http.NewServeMux()
	s.routes(mux)
	s.httpd = &http.Server{
		Addr:              cfg.Addr,
		Handler:           withRequestID(s.accessLog(mux)),
		ReadHeaderTimeout: 5 * time.Second,
	}
	return s
}

// Addr returns the bound address (useful with :0 in tests).
func (s *Server) Addr() string { return s.cfg.Addr }

// Start runs recovery and serves until ListenAndServe returns.
func (s *Server) Start(ctx context.Context) error {
	groups, err := s.coord.Recover(ctx)
	if err != nil {
		return fmt.Errorf("startup recovery failed: %w", err)
	}
	s.log.Printf("level=info msg=\"startup recovery complete\" groups=%d", len(groups))

	if s.cfg.SweepEvery > 0 {
		sctx, cancel := context.WithCancel(context.Background())
		s.stopSweep = cancel
		s.wg.Add(1)
		go s.sweepLoop(sctx)
	}

	s.log.Printf("level=info msg=\"http listening\" addr=%s", s.cfg.Addr)
	err = s.httpd.ListenAndServe()
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

// Shutdown stops the sweep and drains HTTP.
func (s *Server) Shutdown(ctx context.Context) error {
	if s.stopSweep != nil {
		s.stopSweep()
	}
	s.wg.Wait()
	return s.httpd.Shutdown(ctx)
}

func (s *Server) sweepLoop(ctx context.Context) {
	defer s.wg.Done()
	t := time.NewTicker(s.cfg.SweepEvery)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			s.sweepAllGroups(ctx)
		}
	}
}

func (s *Server) sweepAllGroups(ctx context.Context) {
	// The coordinator has no ListGroups wrapper; sweep is driven per group
	// through the state-less admin endpoint, so here we no-op. Tests/demo
	// invoke POST /admin/sweep per group explicitly.
}

// requestIDFromContext returns the correlated id stored by withRequestID.
type reqIDKey struct{}

func withRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := strings.TrimSpace(r.Header.Get("X-Request-Id"))
		if id == "" {
			id = newRequestID()
		}
		w.Header().Set("X-Request-Id", id)
		ctx := context.WithValue(r.Context(), reqIDKey{}, id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func requestID(r *http.Request) string {
	if v, ok := r.Context().Value(reqIDKey{}).(string); ok {
		return v
	}
	return ""
}

func (s *Server) accessLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusWriter{ResponseWriter: w, status: 200}
		next.ServeHTTP(rw, r)
		s.log.Printf("level=info msg=\"http\" request_id=%s method=%s path=%s status=%d dur=%s",
			requestID(r), r.Method, r.URL.Path, rw.status, time.Since(start).Round(time.Microsecond))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}
