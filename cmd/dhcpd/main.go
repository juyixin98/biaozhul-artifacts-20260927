// Command dhcpd runs the local loopback-only DHCPv4 lab server with a UDP
// datagram socket and an HTTP replay/diagnostics API.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"log/slog"
	"os"
	"os/signal"
	"runtime"
	"syscall"
	"time"

	"dhcpv4lab/internal/config"
	"dhcpv4lab/internal/server"
	"dhcpv4lab/internal/storage"
	"dhcpv4lab/internal/transport"
	"dhcpv4lab/internal/version"
)

func main() {
	cfgPath := flag.String("config", "", "path to JSON configuration (defaults are used when empty)")
	writeConfig := flag.Bool("print-defaults", false, "write the default JSON config to stdout and exit")
	flag.Parse()

	if *writeConfig {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(config.Default())
		return
	}

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		slog.New(slog.NewTextHandler(os.Stderr, nil)).Error("config invalid", "error", err)
		os.Exit(2)
	}

	level := slog.LevelInfo
	if os.Getenv("DHCPV4LAB_JSON_LOG") == "1" || os.Getenv("DHCPV4LAB_JSON_LOG") == "true" {
		level = slog.LevelDebug
	}
	log := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: level}))

	log.Info("starting", "banner", version.Banner(), "goMaxProcs", runtime.GOMAXPROCS(0),
		"udp", cfg.Server.UDPListen, "http", cfg.Server.HTTPListen,
		"pool", cfg.Pool.RangeStart+"-"+cfg.Pool.RangeEnd, "testMode", cfg.TestMode)

	st, err := storage.Open(cfg.Store.DSN, cfg.Store.Reset)
	if err != nil {
		log.Error("storage open failed", "error", err)
		os.Exit(1)
	}
	defer st.Close()

	runID := os.Getenv("DHCPV4LAB_RUN_ID")
	if runID == "" {
		runID = "run-" + time.Now().UTC().Format("20060102T150405.000000000")
	}

	var clk server.Clock = wallNow{}
	fakeClock := cfg.TestMode && os.Getenv("DHCPV4LAB_FAKE_CLOCK") == "1"
	if fakeClock {
		seed := time.Unix(server.FakeClockSeedUnix, 0)
		fc := server.NewFakeClock(seed)
		clk = fc
		log.Warn("testMode fake clock enabled — do not use outside local fixtures",
			"seed", seed.Format(time.RFC3339Nano))
	}
	srv, err := server.New(cfg, st, clk)
	if err != nil {
		log.Error("server init failed", "error", err)
		os.Exit(1)
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()

	// Background expiry sweep. Disabled with the fake clock (fixtures drive
	// expiry deterministically through POST /test/sweep) and with a negative
	// configured interval.
	if !fakeClock {
		sweep := cfg.Lease.OfferTTL.Duration
		if cfg.Lease.SweepInterval != nil {
			sweep = cfg.Lease.SweepInterval.Duration
		}
		if sweep > 0 {
			srv.StartSweeper(ctx, sweep/2+1, runID)
		}
	}

	udpSrv, err := transport.ListenUDP(cfg.Server.UDPListen, srv, log, runID,
		cfg.Server.ReadTimeout.Duration)
	if err != nil {
		log.Error("udp listen failed", "addr", cfg.Server.UDPListen, "error", err)
		os.Exit(1)
	}
	log.Info("udp bound (loopback lab socket; replies are unicast to datagram source)",
		"addr", udpSrv.LocalAddr().String())

	api := transport.NewHTTP(srv, log, runID, cfg.TestMode)
	go func() {
		if err := api.ListenAndServe(cfg.Server.HTTPListen); err != nil {
			if err.Error() != "http: Server closed" {
				log.Error("http server stopped", "error", err)
			}
		}
	}()
	log.Info("http replay/diagnostics listening", "addr", cfg.Server.HTTPListen,
		"testEndpoints", cfg.TestMode)

	serveErr := make(chan error, 1)
	go func() { serveErr <- udpSrv.Serve(ctx) }()

	select {
	case <-ctx.Done():
		log.Info("shutdown signal received")
	case err := <-serveErr:
		if err != nil {
			log.Error("udp serve failed", "error", err)
		}
	}
	shutdownCtx, c := context.WithTimeout(context.Background(), 2*time.Second)
	defer c()
	_ = api.Shutdown(shutdownCtx)
	_ = udpSrv.Close()
	log.Info("stopped")
}

type wallNow struct{}

func (wallNow) Now() time.Time { return time.Now() }
