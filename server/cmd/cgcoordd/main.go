// Command cgcoordd runs the partitioned-log consumer-group coordinator as
// an HTTP service. Storage defaults to the in-process memory backend; pass
// -backend postgres (and a DSN) to persist in PostgreSQL.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"example.com/cgcoord/config"
	"example.com/cgcoord/coordinator"
	"example.com/cgcoord/server"
	"example.com/cgcoord/storage"
)

func main() {
	configPath := flag.String("config", envOr("CGCOORD_CONFIG", ""), "path to JSON config (env: CGCOORD_CONFIG)")
	flag.Parse()

	cfg, err := config.Load(*configPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "configuration error:", err)
		os.Exit(2)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	var store storage.Store
	switch cfg.Backend {
	case "postgres":
		pg, err := storage.NewPostgresStore(ctx, cfg.PostgresDSN)
		if err != nil {
			fmt.Fprintln(os.Stderr, "postgres unavailable:", err)
			os.Exit(1)
		}
		store = pg
		fmt.Fprintf(os.Stderr, "backend=postgres dsn=%s\n", redactDSN(cfg.PostgresDSN))
	default:
		store = storage.NewMemoryStore()
		fmt.Fprintln(os.Stderr, "backend=memory (state is lost on restart; use -backend postgres to persist)")
	}
	defer store.Close()

	logger := server.NewCoordLogger(os.Stderr)
	coord := coordinator.New(store, logger, time.Now)

	srv := server.New(coord, server.Config{
		Addr:       cfg.HTTPAddr,
		SweepEvery: cfg.SweepInterval,
		LogWriter:  os.Stderr,
	})

	errCh := make(chan error, 1)
	go func() { errCh <- srv.Start(ctx) }()

	select {
	case <-ctx.Done():
		fmt.Fprintln(os.Stderr, "shutdown signal received")
	case err := <-errCh:
		if err != nil {
			fmt.Fprintln(os.Stderr, "server error:", err)
			os.Exit(1)
		}
	}
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		fmt.Fprintln(os.Stderr, "shutdown error:", err)
	}
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func redactDSN(dsn string) string {
	// Avoid printing credentials into logs.
	out := dsn
	for _, key := range []string{"password=", "PASSWORD="} {
		i := indexFold(out, key)
		if i < 0 {
			continue
		}
		start := i + len(key)
		end := start
		for end < len(out) && out[end] != ' ' && out[end] != '\'' {
			end++
		}
		out = out[:start] + "***" + out[end:]
	}
	return out
}

func indexFold(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		match := true
		for j := 0; j < len(sub); j++ {
			a, b := s[i+j], sub[j]
			if a >= 'A' && a <= 'Z' {
				a += 'a' - 'A'
			}
			if b >= 'A' && b <= 'Z' {
				b += 'a' - 'A'
			}
			if a != b {
				match = false
				break
			}
		}
		if match {
			return i
		}
	}
	return -1
}
