// Command flexhash runs the local resilient-hash routing service.
//
// Boot procedure:
//  1. Parse the standalone config file (listen address, sqlite path,
//     bucket count, initial members).
//  2. Open SQLite and migrate.
//  3. If the event log is non-empty, rebuild the manager purely from the log
//     (storage is the source of truth across restarts; config-file member
//     changes are only consumed on an empty database — push topology changes
//     through POST /v1/config instead).
//  4. Otherwise bootstrap version 1 from the config file and persist it.
//  5. Serve HTTP with graceful shutdown.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"flexhash/internal/config"
	"flexhash/internal/fherr"
	"flexhash/internal/hashring"
	"flexhash/internal/replay"
	"flexhash/internal/server"
	"flexhash/internal/store"
)

func main() {
	configPath := flag.String("config", "configs/flexhash.json", "path to config file")
	flag.Parse()

	if err := run(*configPath); err != nil {
		log.Fatalf("flexhash: %v", err)
	}
}

func run(configPath string) error {
	logger := log.New(os.Stderr, "flexhash ", log.LstdFlags|log.Lmicroseconds)

	cfg, err := config.LoadFile(configPath)
	if err != nil {
		return err
	}
	ctx := context.Background()

	st, err := store.Open(ctx, cfg.SQLitePath)
	if err != nil {
		return err
	}
	defer func() {
		if err := st.Close(); err != nil {
			logger.Printf("close store: %v", err)
		}
	}()

	var mgr *hashring.Manager
	events, err := st.Events(ctx)
	if err != nil {
		return err
	}
	if len(events) > 0 {
		// Storage is truth: rebuild everything from the replay log.
		rebuilt, rep, err := replay.Rebuild(ctx, st)
		if err != nil {
			return err
		}
		mgr = rebuilt
		ring, hrev := mgr.Current()
		if ring.BucketCount != cfg.BucketCount {
			return fherr.New(fherr.KindStateConflict, "main",
				"config bucket_count differs from persisted topology; "+
					"bucket count is topology identity and cannot change at runtime")
		}
		logger.Printf("replayed %d events -> config v%d health rev %d (%d buckets)",
			rep.EventsReplayed, ring.Version, hrev, ring.BucketCount)
		if _, rep2, err := replay.VerifyAssignments(ctx, st); err != nil {
			logger.Printf("warning: replay verification error: %v", err)
		} else if len(rep2.Mismatches) > 0 {
			for _, m := range rep2.Mismatches {
				logger.Printf("REPLAY MISMATCH: %s", m)
			}
		}
	} else {
		// Fresh bootstrap from the config file.
		mgr = hashring.NewManager(cfg.BucketCount)
		coreMembers := make([]hashring.Member, 0, len(cfg.Members))
		for _, m := range cfg.SortedMembers() {
			coreMembers = append(coreMembers, hashring.Member{
				ID: m.ID, Address: m.Address, Weight: m.Weight, Healthy: m.Healthy,
			})
		}
		ring, _, err := mgr.Bootstrap(1, coreMembers, 0)
		if err != nil {
			return err
		}
		if err := persistBootstrap(ctx, st, cfg, ring); err != nil {
			return err
		}
		logger.Printf("bootstrapped config v1: %d members, %d buckets",
			len(coreMembers), cfg.BucketCount)
	}

	svc := server.NewService(mgr, st, cfg.BucketCount)
	svc.SetCurrentConfig(cfg)
	mux := http.NewServeMux()
	svc.Routes(mux)

	srv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           requestLog(logger, mux),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		logger.Printf("listening on %s", cfg.ListenAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("http: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	logger.Printf("shutting down")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return srv.Shutdown(shutdownCtx)
}

func persistBootstrap(ctx context.Context, st *store.Store, cfg *config.Config, ring *hashring.Ring) error {
	membersJSON, err := jsonMarshalMembers(cfg)
	if err != nil {
		return err
	}
	rows := make([]store.AssignmentRow, 0, ring.BucketCount)
	for _, a := range ring.Assignments() {
		rows = append(rows, store.AssignmentRow{
			Version: 1, Bucket: a.Bucket, Member: a.Member,
		})
	}
	return st.SaveConfig(ctx, store.ConfigSnapshot{
		Version:     1,
		BucketCount: ring.BucketCount,
		MembersJSON: membersJSON,
	}, rows)
}

func jsonMarshalMembers(cfg *config.Config) ([]byte, error) {
	coreMembers := make([]hashring.Member, 0, len(cfg.Members))
	for _, m := range cfg.SortedMembers() {
		coreMembers = append(coreMembers, hashring.Member{
			ID: m.ID, Address: m.Address, Weight: m.Weight, Healthy: m.Healthy,
		})
	}
	b, err := json.Marshal(coreMembers)
	if err != nil {
		return nil, fherr.Wrap(fherr.KindComputationFailed, "main.jsonMarshalMembers",
			"encode initial members", err)
	}
	return b, nil
}

func requestLog(logger *log.Logger, h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &statusWriter{ResponseWriter: w, status: 200}
		h.ServeHTTP(rw, r)
		logger.Printf("%s %s -> %d (%s)", r.Method, r.URL.Path, rw.status, time.Since(start))
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (s *statusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}
