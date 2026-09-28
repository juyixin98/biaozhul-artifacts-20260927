// Package server exposes the RIB over a small local HTTP API:
//
//	POST /v1/batches         atomically apply one batch of upserts/deletes
//	GET  /v1/lookup?target=  longest-prefix match + recursive resolution
//	GET  /v1/routes          list installed routes
//	GET  /v1/version         current table version
//	GET  /v1/events          persisted batches (event log / replay source)
//	POST /v1/replay          reconstruct state from events, optionally verify
//	GET  /healthz
//
// Every response carries the request id under X-Request-ID. Accept/reject/
// indeterminate decisions are recorded through internal/diag with the table
// version and target; sensitive metadata is redacted before logging.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/opp221/ribd/internal/diag"
	"github.com/opp221/ribd/internal/netmodel"
	"github.com/opp221/ribd/internal/replay"
	"github.com/opp221/ribd/internal/rib"
	"github.com/opp221/ribd/internal/store"
)

// Server bundles dependencies for the HTTP handlers.
type Server struct {
	Table  *rib.Table
	Store  *store.Store // optional; nil for memory mode
	Logger *diag.Logger
}

// New constructs a Server.
func New(t *rib.Table, st *store.Store, lg *diag.Logger) *Server {
	return &Server{Table: t, Store: st, Logger: lg}
}

// Handler returns the routed http.Handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.health)
	mux.HandleFunc("/v1/version", s.version)
	mux.HandleFunc("/v1/routes", s.routes)
	mux.HandleFunc("/v1/lookup", s.lookup)
	mux.HandleFunc("/v1/batches", s.batches)
	mux.HandleFunc("/v1/events", s.events)
	mux.HandleFunc("/v1/replay", s.replay)
	return mux
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Request-ID", requestID(r))
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) version(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r)
	w.Header().Set("X-Request-ID", rid)
	snap := s.Table.Current()
	writeJSON(w, http.StatusOK, map[string]any{"version": snap.VersionNum(), "routes": len(snap.Routes())})
}

func (s *Server) routes(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r)
	w.Header().Set("X-Request-ID", rid)
	if r.Method != http.MethodGet {
		writeError(w, rid, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	snap := s.Table.Current()
	writeJSON(w, http.StatusOK, map[string]any{
		"version": snap.VersionNum(),
		"routes":  redactRoutes(snap.Routes()),
	})
}

func (s *Server) lookup(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r)
	w.Header().Set("X-Request-ID", rid)
	if r.Method != http.MethodGet {
		writeError(w, rid, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	target := r.URL.Query().Get("target")
	if target == "" {
		writeError(w, rid, http.StatusBadRequest, "bad_query", "query parameter target is required")
		return
	}
	snap := s.Table.Current()
	res := snap.LookupText(target)

	verdict := string(res.Status)
	s.Logger.Log(diag.Record{
		RequestID: rid,
		Event:     "lookup",
		Verdict:   verdict,
		TableVer:  res.TableVersion,
		Target:    target,
		Detail:    res.Reason,
		State: map[string]any{
			"family":        res.Family,
			"failure":       string(res.Failure),
			"chosen_route":  res.ChosenRoute,
			"egress":        res.Egress,
			"match_depth":   len(res.MatchChain),
			"resolve_depth": len(res.ResolveChain),
		},
	})
	writeJSON(w, http.StatusOK, res)
}

type batchRequest struct {
	Changes []rib.Change `json:"changes"`
	// DryRun validates the batch against current state without persisting or
	// swapping the table.
	DryRun bool `json:"dry_run,omitempty"`
	// Comment is stored nowhere but appears in diagnostics.
	Comment string `json:"comment,omitempty"`
}

func (s *Server) batches(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r)
	w.Header().Set("X-Request-ID", rid)
	if r.Method != http.MethodPost {
		writeError(w, rid, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	var req batchRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, rid, http.StatusBadRequest, "bad_json", err.Error())
		return
	}
	if len(req.Changes) == 0 {
		writeError(w, rid, http.StatusBadRequest, "empty_batch", "a batch must contain at least one change")
		return
	}
	base := s.Table.Current().VersionNum()

	if req.DryRun {
		// Validate against a throwaway copy of the snapshot: build() never
		// mutates current state.
		if _, err := s.Table.Current().BuildForAPI(req.Changes); err != nil {
			s.recordReject(rid, "batch.dry_run", base, req, err)
			writeError(w, rid, http.StatusUnprocessableEntity, "batch_rejected", err.Error())
			return
		}
		s.Logger.Log(diag.Record{RequestID: rid, Event: "batch.dry_run", Verdict: "accepted",
			TableVer: base, Detail: fmt.Sprintf("dry-run OK, %d change(s)", len(req.Changes)),
			State: map[string]any{"changes": len(req.Changes)}})
		writeJSON(w, http.StatusOK, map[string]any{"dry_run": true, "base_version": base, "changes": len(req.Changes)})
		return
	}

	snap, err := s.Table.Apply(req.Changes)
	if err != nil {
		s.recordReject(rid, "batch.apply", base, req, err)
		writeError(w, rid, http.StatusUnprocessableEntity, "batch_rejected", err.Error())
		return
	}
	s.Logger.Log(diag.Record{RequestID: rid, Event: "batch.apply", Verdict: "accepted",
		TableVer: snap.VersionNum(), Detail: fmt.Sprintf("applied %d change(s): %s", len(req.Changes), req.Comment),
		State: map[string]any{"base_version": base, "changes": len(req.Changes)}})
	writeJSON(w, http.StatusOK, map[string]any{
		"version":      snap.VersionNum(),
		"base_version": base,
		"changes":      len(req.Changes),
		"routes":       len(snap.Routes()),
	})
}

func (s *Server) recordReject(rid, event string, base uint64, req batchRequest, err error) {
	s.Logger.Log(diag.Record{
		RequestID: rid, Event: event, Verdict: "rejected", TableVer: base,
		Detail: err.Error(),
		State: map[string]any{
			"changes":         len(req.Changes),
			"change_kinds":    kindSummary(req.Changes),
			"rejection_class": classify(err),
		},
	})
}

func classify(err error) string {
	var ce *rib.ChangeError
	if errors.As(err, &ce) {
		return "change_error"
	}
	return "storage_error"
}

func kindSummary(changes []rib.Change) []string {
	out := make([]string, len(changes))
	for i, c := range changes {
		switch c.Kind {
		case rib.Upsert:
			out[i] = "upsert:" + c.Route.ID
		case rib.Delete:
			out[i] = "delete:" + c.Route.ID
		default:
			out[i] = "unknown"
		}
	}
	return out
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r)
	w.Header().Set("X-Request-ID", rid)
	if r.Method != http.MethodGet {
		writeError(w, rid, http.StatusMethodNotAllowed, "method_not_allowed", "use GET")
		return
	}
	if s.Store == nil {
		writeError(w, rid, http.StatusConflict, "no_persistence", "server runs with driver=memory; events are not persisted")
		return
	}
	to := parseVersionQuery(r, "to")
	evs, err := s.Store.Events(r.Context(), to)
	if err != nil {
		writeError(w, rid, http.StatusInternalServerError, "storage_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"events": evs, "count": len(evs)})
}

type replayRequest struct {
	To       uint64 `json:"to_version,omitempty"`
	Verify   bool   `json:"verify,omitempty"`
	MaxDepth int    `json:"max_depth,omitempty"`
}

func (s *Server) replay(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r)
	w.Header().Set("X-Request-ID", rid)
	if r.Method != http.MethodPost {
		writeError(w, rid, http.StatusMethodNotAllowed, "method_not_allowed", "use POST")
		return
	}
	if s.Store == nil {
		writeError(w, rid, http.StatusConflict, "no_persistence", "server runs with driver=memory; nothing to replay")
		return
	}
	req := replayRequest{MaxDepth: s.Table.Current().MaxDepthNum()}
	if r.ContentLength != 0 {
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil && err.Error() != "EOF" {
			writeError(w, rid, http.StatusBadRequest, "bad_json", err.Error())
			return
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()

	to := req.To
	if to == 0 {
		head, err := s.Store.HeadVersion(ctx)
		if err != nil {
			writeError(w, rid, http.StatusInternalServerError, "storage_error", err.Error())
			return
		}
		to = head
	}
	res, err := replay.FromStore(ctx, s.Store, req.MaxDepth, to)
	if err != nil {
		s.Logger.Log(diag.Record{RequestID: rid, Event: "replay", Verdict: "rejected",
			Detail: err.Error(), State: map[string]any{"to_version": to}})
		writeError(w, rid, http.StatusUnprocessableEntity, "replay_failed", err.Error())
		return
	}
	out := map[string]any{
		"replayed_version": res.Version,
		"batches_applied":  res.Applied,
		"routes":           redactRoutes(res.Snap.Routes()),
	}
	if req.Verify {
		mm, err := replay.VerifyAt(ctx, s.Store, uint64(req.MaxDepth), to)
		if err != nil {
			writeError(w, rid, http.StatusUnprocessableEntity, "verify_failed", err.Error())
			return
		}
		out["verify"] = map[string]any{"ok": len(mm) == 0, "mismatches": mm}
	}
	s.Logger.Log(diag.Record{RequestID: rid, Event: "replay", Verdict: "accepted",
		TableVer: res.Version, Detail: fmt.Sprintf("replayed %d batches to v%d", res.Applied, res.Version)})
	writeJSON(w, http.StatusOK, out)
}

func parseVersionQuery(r *http.Request, key string) uint64 {
	v := r.URL.Query().Get(key)
	if v == "" {
		return 0
	}
	n, err := strconv.ParseUint(v, 10, 64)
	if err != nil {
		return 0
	}
	return n
}

// redactRoutes masks sensitive metadata of every route before serialization.
func redactRoutes(routes []netmodel.Route) []map[string]any {
	out := make([]map[string]any, 0, len(routes))
	for _, rt := range routes {
		entry := map[string]any{
			"id":                rt.ID,
			"prefix":            rt.Prefix.String(),
			"admin_distance":    rt.AdminDist,
			"metric":            rt.Metric,
			"next_hop":          rt.NextHop,
			"seq":               rt.Seq,
			"installed_version": rt.InstalledVersion,
		}
		if len(rt.Meta) > 0 {
			entry["meta"] = diag.RedactMeta(rt.Meta)
		}
		out = append(out, entry)
	}
	return out
}

func requestID(r *http.Request) string {
	if id := r.Header.Get("X-Request-ID"); id != "" {
		return id
	}
	return diag.NewRequestID()
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, rid string, code int, kind, msg string) {
	writeJSON(w, code, map[string]any{
		"error":      kind,
		"message":    msg,
		"request_id": rid,
	})
}
