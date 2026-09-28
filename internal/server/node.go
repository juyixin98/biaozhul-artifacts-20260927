// Package server assembles one node process: kernel ledger + snapshot
// coordinator + transport + persistence + the standard-library HTTP API.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"time"

	"clsnap/internal/apperr"
	"clsnap/internal/clock"
	"clsnap/internal/config"
	"clsnap/internal/kernel"
	"clsnap/internal/logrun"
	"clsnap/internal/protocol"
	"clsnap/internal/snapshot"
	"clsnap/internal/store"
	"clsnap/internal/transport"
)

// Node is one running process.
type Node struct {
	Cfg       *config.Node
	Store     store.Store
	Ledger    *kernel.Ledger
	Coord     *snapshot.Coordinator
	Router    *StaticRouter
	Clk       *clock.Clock
	Tr        transport.Transport
	journal   *logrun.Journal // may be nil in plain server mode
	epoch     uint64

	mu       sync.Mutex
	lastSeq  map[inboundKey]uint64 // receiver-side FIFO check
	stopPump context.CancelFunc
	pumpWG   sync.WaitGroup
}

type inboundKey struct{ src, dst protocol.NodeID }

// Deps for wiring. When Direct is non-nil it replaces the HTTP transport
// (used inside one process by the scenario harness).
type Deps struct {
	Cfg     *config.Node
	St      store.Store
	Direct  transport.Transport
	Journal *logrun.Journal
}

// NewNode wires a node using an already opened store.
func NewNode(ctx context.Context, d Deps) (*Node, error) {
	cfg := d.Cfg
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	if err := d.St.Bootstrap(ctx, cfg.ID, cfg.Accounts); err != nil {
		return nil, err
	}
	epoch, err := d.St.BumpEpoch(ctx, cfg.ID)
	if err != nil {
		return nil, err
	}
	accounts, err := d.St.LoadAccounts(ctx, cfg.ID)
	if err != nil {
		return nil, err
	}
	led, err := kernel.NewLedger(cfg.ID, accounts)
	if err != nil {
		return nil, err
	}

	// Routing table for this node only needs accounts of all nodes. In the
	// three-process deployment each node learns all accounts from its own
	// config plus peers' configs are merged by the launcher; in the direct
	// harness the wiring provides the union via seed. Here cfg.Accounts only
	// lists local accounts, so callers (main / harness) must call SetRouter.
	r := NewStaticRouter(map[protocol.NodeID][]protocol.Account{cfg.ID: accounts})

	var tr transport.Transport = d.Direct
	if tr == nil {
		urls := map[protocol.NodeID]string{}
		for _, p := range cfg.Peers {
			urls[p.ID] = p.URL
		}
		tr = NewHTTPTransport(cfg.ID, urls)
	}
	var lg snapshot.Logger
	if d.Journal != nil {
		lg = d.Journal
	}
	clk := clock.New()
	coord, err := snapshot.New(snapshot.Deps{
		Node: cfg.ID, Peers: cfg.PeerIDs(), Ledger: led, Store: d.St,
		Transport: tr, Clock: clk, Logger: lg, Router: r, Epoch: epoch,
	})
	if err != nil {
		return nil, err
	}
	n := &Node{
		Cfg: cfg, Store: d.St, Ledger: led, Coord: coord, Router: r,
		Clk: clk, Tr: tr, journal: d.Journal, epoch: epoch,
		lastSeq: map[inboundKey]uint64{},
	}
	// Restart rule: abort everything left recording by an earlier epoch.
	if _, err := coord.RecoverAbortedRounds(ctx); err != nil {
		return nil, err
	}
	return n, nil
}

// MergeRouterAccounts extends this node's account routing table with the
// union of all nodes' seed accounts. Called by launchers after every node
// config is known.
func (n *Node) MergeRouterAccounts(seed map[protocol.NodeID][]protocol.Account) {
	n.Router = NewStaticRouter(seed)
	// Rebuild coordinator with the new router.
	c, err := snapshot.New(snapshot.Deps{
		Node: n.Cfg.ID, Peers: n.Cfg.PeerIDs(), Ledger: n.Ledger, Store: n.Store,
		Transport: n.Tr, Clock: n.Clk, Logger: n.journalOrNil(), Router: n.Router, Epoch: n.epoch,
	})
	if err != nil {
		panic(err) // wiring bug, fail fast
	}
	n.Coord = c
}

func (n *Node) journalOrNil() snapshot.Logger {
	if n.journal == nil {
		return nil
	}
	return n.journal
}

// StartPump launches the periodic durable-outbox flush (HTTP mode only).
func (n *Node) StartPump(ctx context.Context) {
	if _, ok := n.Tr.(*HTTPTransport); !ok {
		return
	}
	interval := time.Duration(n.Cfg.FlushMS) * time.Millisecond
	if interval <= 0 {
		interval = 50 * time.Millisecond
	}
	ctx, cancel := context.WithCancel(ctx)
	n.stopPump = cancel
	n.pumpWG.Add(1)
	go func() {
		defer n.pumpWG.Done()
		t := time.NewTicker(interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-t.C:
				_, _ = n.Coord.FlushOutbox(ctx)
			}
		}
	}()
}

// StopPump halts the background flush.
func (n *Node) StopPump() {
	if n.stopPump != nil {
		n.stopPump()
	}
	n.pumpWG.Wait()
}

// receiveEnvelope applies receiver-side FIFO checking then hands to the
// coordinator. expected seq per channel is last+1; seq 0 (direct transport,
// which guarantees order structurally) skips the check.
func (n *Node) receiveEnvelope(ctx context.Context, env protocol.Envelope) error {
	if env.Seq != 0 {
		n.mu.Lock()
		key := inboundKey{env.Src, env.Dst}
		want := n.lastSeq[key] + 1
		if env.Seq > want {
			n.mu.Unlock()
			return apperr.Conflictf(apperr.CodeFIFOViolation,
				"channel %s->%s received seq %d, expected %d (gap)", env.Src, env.Dst, env.Seq, want)
		}
		// Redelivery of an already processed seq: idempotent ACK.
		if env.Seq < want {
			n.mu.Unlock()
			return nil
		}
		n.lastSeq[key] = env.Seq
		n.mu.Unlock()
	}
	return n.Coord.Receive(ctx, env)
}

// Handler returns the HTTP mux for the process.
func (n *Node) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", n.handleHealth)
	mux.HandleFunc("/accounts", n.handleAccounts)
	mux.HandleFunc("/transfers", n.handleTransfers)
	mux.HandleFunc("/msg", n.handleMsg)
	mux.HandleFunc("/snapshots", n.handleSnapshots)
	mux.HandleFunc("/snapshots/", n.handleSnapshotByID)
	mux.HandleFunc("/events/", n.handleEvents)
	mux.HandleFunc("/runs", n.handleRuns)
	return mux
}

func (n *Node) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"node": n.Cfg.ID, "epoch": n.epoch, "status": "ok",
	})
}

func (n *Node) handleAccounts(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeErr(w, apperr.Inputf(apperr.CodeMalformed, "use GET"))
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"accounts": n.Ledger.Accounts()})
}

func (n *Node) handleTransfers(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, apperr.Inputf(apperr.CodeMalformed, "use POST"))
		return
	}
	var t protocol.Transfer
	if err := json.NewDecoder(r.Body).Decode(&t); err != nil {
		writeErr(w, apperr.Inputf(apperr.CodeMalformed, "decode transfer: %s", err))
		return
	}
	if err := n.Coord.StartTransfer(r.Context(), t); err != nil {
		writeErr(w, err)
		return
	}
	// In direct transport nothing flushes automatically; HTTP mode flushes on
	// demand too — make submission immediately attempt delivery so a simple
	// curl demo works without waiting for the pump.
	if _, err := n.Coord.FlushOutbox(r.Context()); err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusAccepted, map[string]any{"accepted": t.Ref, "node": n.Cfg.ID})
}

func (n *Node) handleMsg(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeErr(w, apperr.Inputf(apperr.CodeMalformed, "use POST"))
		return
	}
	var env protocol.Envelope
	if err := json.NewDecoder(r.Body).Decode(&env); err != nil {
		writeErr(w, apperr.Inputf(apperr.CodeMalformed, "decode envelope: %s", err))
		return
	}
	if env.Dst != n.Cfg.ID {
		writeErr(w, apperr.Inputf(apperr.CodeMalformed,
			"envelope destined for %s delivered to %s", env.Dst, n.Cfg.ID))
		return
	}
	if err := n.receiveEnvelope(r.Context(), env); err != nil {
		writeErr(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

type startSnapshotReq struct {
	ID protocol.SnapshotID `json:"id"`
}

func (n *Node) handleSnapshots(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		ids, err := n.Store.ListSnapshots(r.Context())
		if err != nil {
			writeErr(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"snapshots": ids})
	case http.MethodPost:
		var req startSnapshotReq
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeErr(w, apperr.Inputf(apperr.CodeMalformed, "decode snapshot request: %s", err))
			return
		}
		if err := n.Coord.Initiate(r.Context(), req.ID); err != nil {
			writeErr(w, err)
			return
		}
		if _, err := n.Coord.FlushOutbox(r.Context()); err != nil {
			writeErr(w, err)
			return
		}
		writeJSON(w, http.StatusCreated, map[string]any{"snapshot": req.ID, "initiated_by": n.Cfg.ID})
	default:
		writeErr(w, apperr.Inputf(apperr.CodeMalformed, "use GET or POST"))
	}
}

func (n *Node) handleSnapshotByID(w http.ResponseWriter, r *http.Request) {
	id := protocol.SnapshotID(tailAfter(r.URL.Path, "/snapshots/"))
	if id == "" {
		writeErr(w, apperr.Inputf(apperr.CodeMalformed, "snapshot id required"))
		return
	}
	rec, err := n.Coord.Record(r.Context(), id)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rec)
}

func (n *Node) handleEvents(w http.ResponseWriter, r *http.Request) {
	runID := tailAfter(r.URL.Path, "/events/")
	evs, err := n.Store.ListEvents(r.Context(), runID)
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"run_id": runID, "events": evs})
}

func (n *Node) handleRuns(w http.ResponseWriter, r *http.Request) {
	runs, err := n.Store.ListRuns(r.Context())
	if err != nil {
		writeErr(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": runs})
}

func tailAfter(path, prefix string) string {
	if len(path) <= len(prefix) {
		return ""
	}
	return path[len(prefix):]
}

// writeErr maps the structured error contract onto HTTP status codes.
func writeErr(w http.ResponseWriter, err error) {
	ae, ok := apperr.As(err)
	if !ok {
		ae = apperr.Failure(apperr.CodeFailure, "http", err.Error(), nil)
	}
	status := http.StatusInternalServerError
	switch ae.Kind {
	case apperr.KindInput:
		status = http.StatusBadRequest
	case apperr.KindConflict:
		status = http.StatusConflict
	case apperr.KindExhausted:
		status = http.StatusTooManyRequests
	case apperr.KindFailure:
		status = http.StatusInternalServerError
	}
	writeJSON(w, status, map[string]any{
		"error": map[string]any{
			"kind": string(ae.Kind), "code": ae.Code, "op": ae.Op, "message": ae.Msg,
		},
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("content-type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// ListenAndServe runs the HTTP server with a graceful shutdown helper return.
func (n *Node) ListenAndServe(ctx context.Context) (*http.Server, error) {
	srv := &http.Server{
		Addr:              n.Cfg.Listen,
		Handler:           n.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			fmt.Printf("[%s] http server error: %v\n", n.Cfg.ID, err)
		}
	}()
	go func() {
		<-ctx.Done()
		shCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(shCtx)
	}()
	return srv, nil
}
