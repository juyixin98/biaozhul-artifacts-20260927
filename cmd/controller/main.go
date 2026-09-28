// Command controller runs the reconciliation loop against the desired-state
// API and the actual resource service over HTTP.
package main

import (
	"context"
	"flag"
	"os"
	"os/signal"
	"syscall"
	"time"

	"crcontroller/internal/actual"
	"crcontroller/internal/config"
	"crcontroller/internal/controllerstore"
	"crcontroller/internal/desired"
	"crcontroller/internal/logx"
	"crcontroller/internal/reconcile"
)

func main() {
	cfgPath := flag.String("config", "configs/controller.json", "config file")
	flag.Parse()

	var cfg config.Controller
	if err := config.Load(*cfgPath, &cfg); err != nil {
		fatal(err)
	}
	if err := config.ValidateController(&cfg); err != nil {
		fatal(err)
	}
	logger := logx.New(os.Stderr)

	cs, err := controllerstore.Open(cfg.DatabasePath)
	if err != nil {
		fatal(err)
	}
	defer cs.Close()

	bo := reconcile.Backoff{
		Base: time.Duration(orDefault(cfg.BackoffBaseMS, 50)) * time.Millisecond,
		Max:  time.Duration(orDefault(cfg.BackoffMaxMS, 5000)) * time.Millisecond,
	}
	resync := 30 * time.Second
	if cfg.ResyncInterval != "" {
		if d, err := time.ParseDuration(cfg.ResyncInterval); err == nil {
			resync = d
		}
	}

	ctl := reconcile.New(reconcile.Config{
		Desired:        desired.New(cfg.DesiredURL, cfg.ControllerAuth),
		Actual:         actual.New(cfg.ActualURL),
		Store:          cs,
		Log:            logger,
		Backoff:        bo,
		ResyncInterval: resync,
	})

	ctx, cancel := context.WithCancel(context.Background())
	ctl.Start(ctx)

	diagAddr := cfg.DiagnosticsAddr
	if diagAddr == "" {
		diagAddr = "127.0.0.1:18083"
	}
	diag := reconcile.NewDiagnosticsServer(cs, ctl)
	diag.ListenAndServe(ctx, diagAddr)

	logger.Info("controller", "started", map[string]any{
		"desired": cfg.DesiredURL, "actual": cfg.ActualURL,
		"diagnostics": diagAddr, "resync": resync.String(),
	})

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	cancel()
	ctl.Queue().ShutDown()
	time.Sleep(150 * time.Millisecond) // let the worker drain
	logger.Info("controller", "stopped", nil)
}

func orDefault(v, def int) int {
	if v <= 0 {
		return def
	}
	return v
}

func fatal(err error) {
	_, _ = os.Stderr.WriteString("fatal: " + err.Error() + "\n")
	os.Exit(1)
}
