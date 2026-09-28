// Command apiserver runs the desired-state HTTP API backed by SQLite.
package main

import (
	"context"
	"flag"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"crcontroller/internal/apiserver"
	"crcontroller/internal/config"
	"crcontroller/internal/logx"
	"crcontroller/internal/store"
)

func main() {
	cfgPath := flag.String("config", "configs/apiserver.json", "config file")
	flag.Parse()

	var cfg config.APIServer
	if err := config.Load(*cfgPath, &cfg); err != nil {
		fatal(err)
	}
	if err := config.ValidateAPIServer(&cfg); err != nil {
		fatal(err)
	}
	logger := logx.New(os.Stderr)

	st, err := store.Open(cfg.DatabasePath)
	if err != nil {
		fatal(err)
	}
	defer st.Close()

	opts := []apiserver.Option{
		apiserver.WithControllerAuth(cfg.ControllerAuth),
	}
	// The controller registers its event sink directly when all components
	// run in one process. With separate binaries over HTTP the sink is left
	// nil unless explicitly enabled; disableEventSink keeps it off for
	// deterministic fault demos (the controller then relies on resync).
	if cfg.DisableEventSink {
		opts = append(opts, apiserver.WithEventSink(nil))
	}
	srv := apiserver.New(st, logger, opts...)

	httpSrv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		logger.Info("apiserver", "listening", map[string]any{
			"addr": cfg.Listen, "db": cfg.DatabasePath,
			"authConfigured": cfg.ControllerAuth != "",
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
	logger.Info("apiserver", "stopped", nil)
}

func fatal(err error) {
	_, _ = os.Stderr.WriteString("fatal: " + err.Error() + "\n")
	os.Exit(1)
}
