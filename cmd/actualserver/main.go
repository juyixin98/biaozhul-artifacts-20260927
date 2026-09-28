// Command actualserver runs the physical-resource service with its own SQLite
// database and the test-only fault-injection admin API.
package main

import (
	"context"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"crcontroller/internal/actualserver"
	"crcontroller/internal/actualstore"
	"crcontroller/internal/config"
	"crcontroller/internal/logx"
)

func main() {
	cfgPath := flag.String("config", "configs/actualserver.json", "config file")
	flag.Parse()

	var cfg config.ActualServer
	if err := config.Load(*cfgPath, &cfg); err != nil {
		fatal(err)
	}
	if err := config.ValidateActualServer(&cfg); err != nil {
		fatal(err)
	}
	logger := logx.New(os.Stderr)

	st, err := actualstore.Open(cfg.DatabasePath)
	if err != nil {
		fatal(err)
	}
	defer st.Close()

	srv := actualserver.New(st, logger)
	httpSrv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		logger.Info("actual", "listening", map[string]any{
			"addr": cfg.Listen, "db": cfg.DatabasePath,
		})
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			fatal(err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(ctx)
	logger.Info("actual", "stopped", nil)
}

func fatal(err error) {
	_, _ = os.Stderr.WriteString("fatal: " + err.Error() + "\n")
	os.Exit(1)
}
