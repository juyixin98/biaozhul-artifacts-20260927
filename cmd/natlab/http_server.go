package main

import (
	"encoding/json"
	"io"
	"log"
	"net/http"
	"time"

	"natlab/internal/config"
)

// httpServer wraps the handler with standard-library access logging.
type httpServer struct {
	cfg     *config.Config
	handler http.Handler
	logw    io.Writer
}

func (s *httpServer) listenAndServe() error {
	logged := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		s.handler.ServeHTTP(rec, r)
		entry, _ := json.Marshal(map[string]any{
			"ts":       start.UTC().Format(time.RFC3339Nano),
			"method":   r.Method,
			"path":     r.URL.Path,
			"status":   rec.status,
			"duration": time.Since(start).String(),
			"remote":   r.RemoteAddr,
		})
		_, _ = s.logw.Write(append(entry, '\n'))
	})
	srv := &http.Server{
		Addr:              s.cfg.ListenAddr,
		Handler:           logged,
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("natlab listening on %s (public=%s pool=%d-%d db=%s)",
		s.cfg.ListenAddr, s.cfg.PublicIPString(), s.cfg.PortLow, s.cfg.PortHigh, s.cfg.DBPath)
	return srv.ListenAndServe()
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}
