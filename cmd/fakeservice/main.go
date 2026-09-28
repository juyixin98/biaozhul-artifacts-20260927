// Command fakeservice runs the standalone actual-resource service with its
// fault-injection control plane. It imports none of the controller internals
// beyond the fakecloud package and shared diagnostics.
package main

import (
	"context"
	"errors"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"resourcecontroller/internal/config"
	"resourcecontroller/internal/diag"
	"resourcecontroller/internal/fakecloud"
)

func main() {
	cfg := config.FakeServiceFromEnv()
	log := diag.New(os.Stdout, "fakecloud")

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	svc := fakecloud.NewService(log)
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           svc.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	serverErr := make(chan error, 1)
	go func() {
		log.Info(ctx, "fake resource service listening", "addr", cfg.HTTPAddr)
		serverErr <- srv.ListenAndServe()
	}()

	select {
	case <-ctx.Done():
	case err := <-serverErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error(ctx, "http server failed", "error", err.Error())
			os.Exit(1)
		}
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Error(shutdownCtx, "graceful shutdown failed", "error", err.Error())
	}
	log.Info(context.Background(), "fake resource service stopped")
}
