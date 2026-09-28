// Package service is the application orchestrator. It is the ONLY place that
// ties the Dijkstra-Scholten kernel (decisions), the PostgreSQL store (facts)
// and the synthetic compute interpreter together. Every mutating request is:
//
//	decide (kernel, pure) -> persist (one per-run DB transaction) -> fold
//
// The in-memory kernel state is rebuilt by folding the persisted log at
// startup, so the database is the source of truth and the service is
// restart-safe.
package service

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"dsnet/app/compute"
	"dsnet/kernel"
	"dsnet/proto"
	"dsnet/store"

	"github.com/jackc/pgx/v5"
)

// Logger is the minimal structured-log surface (slog in main).
type Logger interface {
	Info(msg string, args ...any)
	Error(msg string, args ...any)
}

// ServiceError carries the stable failure class plus HTTP code.
type ServiceError struct {
	Class proto.FailureClass
	Code  int
	Msg   string
	Cause error
}

func (e *ServiceError) Error() string { return fmt.Sprintf("%s: %s", e.Class, e.Msg) }
func (e *ServiceError) Unwrap() error { return e.Cause }

// errRaceLost is returned inside materialization when a competing worker
// already took the transfer. The caller reports "no work" and the worker polls.
var errRaceLost = fmt.Errorf("claim lost to another worker")

// ErrRaceLost is the exported sentinel callers can check with errors.Is.
var ErrRaceLost = errRaceLost

// Service orchestrates kernel + store.
type Service struct {
	st   *store.Store
	log  Logger
	ttl  time.Duration

	mu sync.Mutex
	ks *kernel.State

	idCounter uint64
}

// New opens a service and rebuilds kernel state from the persisted event log.
func New(ctx context.Context, st *store.Store, log Logger, ttl time.Duration) (*Service, error) {
	s := &Service{st: st, log: log, ttl: ttl, ks: kernel.NewState()}
	if err := s.rebuild(ctx); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Service) rebuild(ctx context.Context) error {
	events, err := s.st.AllEvents(ctx)
	if err != nil {
		return fmt.Errorf("load events: %w", err)
	}
	for _, ev := range events {
		kernel.Fold(s.ks, ev)
	}
	s.log.Info("kernel state rebuilt from event log",
		"events", len(events), "version", proto.Version)
	return nil
}

// nodeFor maps a partition to its DS node identity.
func nodeFor(partition string) string { return "node:" + partition }

// newID returns a process-unique, non-guessable identity.
func (s *Service) newID(prefix string) string {
	n := atomic.AddUint64(&s.idCounter, 1)
	var b [8]byte
	_, _ = rand.Read(b[:])
	return fmt.Sprintf("%s_%d_%s", prefix, n, hex.EncodeToString(b[:6]))
}

// commit runs decide over a scratch copy of live state for each command,
// accumulates the decided facts, persists them + materialization in one
// transaction, and only then folds them into live kernel state.
func (s *Service) commit(ctx context.Context, runID string,
	mat func(pgx.Tx, []proto.Event) error, cmds ...kernel.Command) ([]proto.Event, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	scratch := buildScratch(ctx, s.st, s.ks, runID)
	if scratch == nil {
		// No events yet for this run: clone empty and let Apply validate.
		scratch = kernel.NewState()
	}

	var all []proto.Event
	for _, cmd := range cmds {
		evs, err := kernel.Apply(scratch, cmd)
		if err != nil {
			class, code := kernel.MapError(err)
			return nil, &ServiceError{Class: class, Code: code, Msg: err.Error()}
		}
		for _, ev := range evs {
			kernel.Fold(scratch, ev)
			all = append(all, ev)
		}
	}
	if len(all) == 0 && mat == nil {
		return nil, nil
	}
	var matErr error
	err := s.st.AppendBatch(ctx, runID, all, func(tx pgx.Tx) error {
		if mat != nil {
			matErr = mat(tx, all)
			return matErr
		}
		return nil
	})
	if errors.Is(matErr, errRaceLost) {
		return nil, ErrRaceLost
	}
	if err != nil {
		return nil, &ServiceError{Class: proto.FailInvalidRequest, Code: 500,
			Msg: "persist failed: " + err.Error()}
	}
	for _, ev := range all {
		kernel.Fold(s.ks, ev)
	}
	return all, nil
}

// buildScratch reconstructs the current per-run state from the persisted log
// WITHOUT copying the live state. The DB log is authoritative; folding it is
// linear in the run's events and keeps the live mutex critical section
// deterministic. A missing run yields nil.
func buildScratch(ctx context.Context, st *store.Store, live *kernel.State, runID string) *kernel.State {
	if _, err := live.Run(runID); err != nil {
		return nil
	}
	events, err := st.Events(ctx, runID, 0)
	if err != nil {
		return nil
	}
	sc := kernel.NewState()
	for _, ev := range events {
		kernel.Fold(sc, ev)
	}
	return sc
}
