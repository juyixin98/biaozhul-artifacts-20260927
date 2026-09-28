// Command cidrsvc runs the minimal non-overlapping CIDR cover HTTP service.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"cidrsvc/internal/config"
	"cidrsvc/internal/service"
	"cidrsvc/internal/store"
)

func main() {
	cfgPath := flag.String("config", "configs/config.json", "path to JSON config")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "config error: %v\n", err)
		os.Exit(2)
	}

	logger, closeLog, err := service.NewJSONLogger(cfg.LogPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "logger error: %v\n", err)
		os.Exit(2)
	}
	defer closeLog()

	ctx := context.Background()
	st, err := store.Open(ctx, cfg.DatabasePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "store error: %v\n", err)
		os.Exit(2)
	}
	defer st.Close()

	handler := service.New(cfg, st, logger).Routes()

	logger.Log(service.LogEntry{
		Time: time.Now().UTC(), Level: "info", Message: "service_starting",
		Version: service.Version,
	})
	fmt.Fprintf(os.Stderr, "cidrsvc %s starting: %s\n", service.Version, cfg)

	httpSrv := &http.Server{
		Addr:              cfg.HTTPListen,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
	}

	errCh := make(chan error, 1)
	go func() {
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errCh:
		fmt.Fprintf(os.Stderr, "server error: %v\n", err)
		os.Exit(1)
	case sig := <-sigCh:
		fmt.Fprintf(os.Stderr, "\nreceived %s, shutting down...\n", sig)
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.ShutdownTimeout)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		fmt.Fprintf(os.Stderr, "graceful shutdown failed: %v\n", err)
		os.Exit(1)
	}
	logger.Log(service.LogEntry{Time: time.Now().UTC(), Level: "info", Message: "service_stopped"})
}
