// Command dhcp4d runs the loopback-only DHCPv4 subset lab server.
//
// It wires the four layers together:
//
//	config -> dhcp4 (wire model) -> storage (SQLite state machine)
//	      -> server (adapter) -> udpserver / replay (interfaces)
//
// The default configuration binds only to 127.0.0.1 high ports. Binding
// any non-loopback address requires "allow_non_loopback": true and is
// still logged loudly.
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/netip"
	"os"
	"os/signal"
	"syscall"
	"time"

	"dhcp4lab/internal/config"
	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/ippool"
	"dhcp4lab/internal/replay"
	"dhcp4lab/internal/server"
	"dhcp4lab/internal/storage"
	"dhcp4lab/internal/udpserver"
	"dhcp4lab/internal/version"
)

func main() {
	var (
		configPath = flag.String("config", "configs/lab.json", "path to JSON configuration")
		showVer    = flag.Bool("version", false, "print version and exit")
		textLogs   = flag.Bool("text-logs", false, "human-readable logs instead of JSON")
	)
	flag.Parse()

	if *showVer {
		fmt.Printf("dhcp4d %s (%s)\n", version.Server, version.Protocol)
		return
	}

	handlerOpts := &slog.HandlerOptions{Level: slog.LevelInfo}
	var logger *slog.Logger
	if *textLogs {
		logger = slog.New(slog.NewTextHandler(os.Stdout, handlerOpts))
	} else {
		logger = slog.New(slog.NewJSONHandler(os.Stdout, handlerOpts))
	}

	if err := run(*configPath, logger); err != nil {
		logger.LogAttrs(context.Background(), slog.LevelError, "fatal",
			slog.String("component", "main"), slog.Any("err", err))
		os.Exit(1)
	}
}

func run(configPath string, logger *slog.Logger) error {
	raw, err := os.ReadFile(configPath)
	if err != nil {
		return fmt.Errorf("read config %s: %w", configPath, err)
	}
	cfg, err := config.Parse(raw)
	if err != nil {
		return err
	}
	logger.LogAttrs(context.Background(), slog.LevelInfo, "configuration loaded",
		slog.String("component", "main"),
		slog.String("version", version.Server),
		slog.String("listen_udp", cfg.ListenUDP),
		slog.String("admin_http", cfg.AdminHTTP),
		slog.String("network", cfg.Network),
		slog.String("pool", cfg.PoolStart+"-"+cfg.PoolEnd),
		slog.Duration("lease_time", cfg.LeaseTime.Duration),
		slog.Duration("offer_ttl", cfg.OfferTTL.Duration))

	if cfg.AllowNonLoopback {
		logger.Warn("non-loopback binding explicitly permitted; this lab server is not " +
			"fit for production networks and may answer real DHCP clients")
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	db, err := storage.OpenDB(ctx, cfg.Database)
	if err != nil {
		return fmt.Errorf("open database: %w", err)
	}
	defer db.Close()

	pStart, _ := netip.ParseAddr(cfg.PoolStart)
	pEnd, _ := netip.ParseAddr(cfg.PoolEnd)
	pool, err := ippool.New(pStart, pEnd)
	if err != nil {
		return err
	}
	srvID, _ := netip.ParseAddr(cfg.ServerID)
	store, err := storage.New(ctx, storage.Options{
		DB: db, Pool: pool, ServerID: srvID,
		LeaseTime: cfg.LeaseTime.Duration, OfferTTL: cfg.OfferTTL.Duration,
	})
	if err != nil {
		return fmt.Errorf("init store: %w", err)
	}
	defer store.Close()
	store.StartReaper(ctx, cfg.SweepInterval.Duration, logger)

	params, err := server.ParamsFromConfig(cfg)
	if err != nil {
		return err
	}
	srv, err := server.New(store, params, logger)
	if err != nil {
		return err
	}

	// The transport handler re-parses so malformed datagrams never reach
	// the adapter; on success it returns the encoded reply.
	udpHandler := func(ctx context.Context, datagram []byte) []byte {
		pkt, perr := dhcp4.Unmarshal(datagram)
		if perr != nil {
			return nil
		}
		dec, err := srv.Handle(ctx, pkt)
		if err != nil {
			logger.LogAttrs(ctx, slog.LevelError, "adapter error",
				slog.String("component", "main"), slog.Any("err", err))
			return nil
		}
		return dec.ReplyBytes
	}

	udp := udpserver.New(cfg.ListenUDP, udpHandler, logger)
	if err := udp.Listen(); err != nil {
		return err
	}
	serveErr := make(chan error, 1)
	go func() { serveErr <- udp.Serve(ctx) }()

	api := replay.New(srv, udp.RunID(), logger)
	httpSrv, httpLn, err := replay.ListenAndServe(ctx, cfg.AdminHTTP, api.Handler(), logger)
	if err != nil {
		return fmt.Errorf("admin http: %w", err)
	}
	logger.LogAttrs(ctx, slog.LevelInfo, "admin http listening",
		slog.String("component", "main"),
		slog.String("run_id", udp.RunID()),
		slog.String("addr", httpLn.Addr().String()))

	<-ctx.Done()
	logger.Info("shutdown requested")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = httpSrv.Shutdown(shutdownCtx)
	if err := udp.Close(); err != nil {
		logger.Warn("udp close error", "err", err)
	}
	select {
	case e := <-serveErr:
		if e != nil {
			return e
		}
	default:
	}
	return nil
}
