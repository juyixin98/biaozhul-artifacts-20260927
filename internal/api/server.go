// Package api wires router/store/replay behind a small HTTP JSON API using
// only the standard-library mux.
//
// Endpoints:
//
//	GET  /healthz
//	POST /v1/route                resolve one tuple
//	POST /v1/route/bulk           resolve many tuples
//	GET  /v1/version              current snapshot (members, allocation)
//	GET  /v1/stats                bucket share vs actual traffic counters
//	POST /v1/config/reload        replace member set (CAS via expected_version)
//	POST /admin/members/{id}/down mark a next-hop failed (immediate exclusion)
//	POST /admin/members/{id}/up   versioned recovery
//	POST /admin/members/{id}/weight
//	GET  /v1/flowsets             list fixed flow sets
//	POST /v1/replay               run a version-to-version replay
//	GET  /v1/replay/{runID}       fetch replay report
//	GET  /v1/runs                 list recent runs
package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"path/filepath"
	"time"

	"flowrouter/internal/apperr"
	"flowrouter/internal/config"
	"flowrouter/internal/flow"
	"flowrouter/internal/replay"
	"flowrouter/internal/router"
	"flowrouter/internal/store"
)

// Server holds module references for handlers.
type Server struct {
	RT         *router.Router
	Store      *store.Store
	Planner    *replay.Planner
	Library    *replay.FileLibrary
	Config     *config.Config
	ConfigPath string
	Logger     *slog.Logger
}

// NewMux builds the standard-library mux with method-specific routes.
func (s *Server) NewMux() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.healthz)
	mux.HandleFunc("POST /v1/route", s.routeOne)
	mux.HandleFunc("POST /v1/route/bulk", s.routeBulk)
	mux.HandleFunc("GET /v1/version", s.getVersion)
	mux.HandleFunc("GET /v1/stats", s.getStats)
	mux.HandleFunc("POST /v1/config/reload", s.reloadConfig)
	mux.HandleFunc("POST /admin/members/{id}/down", s.memberDown)
	mux.HandleFunc("POST /admin/members/{id}/up", s.memberUp)
	mux.HandleFunc("POST /admin/members/{id}/weight", s.memberWeight)
	mux.HandleFunc("GET /v1/flowsets", s.listFlowSets)
	mux.HandleFunc("POST /v1/replay", s.createReplay)
	mux.HandleFunc("GET /v1/replay/{runID}", s.getReplay)
	mux.HandleFunc("GET /v1/runs", s.listRuns)
	return s.withRequestLog(mux)
}

func (s *Server) healthz(w http.ResponseWriter, r *http.Request) {
	cur := s.RT.Current()
	status := "ok"
	if cur.Ring == nil || cur.Ring.Empty() {
		status = "degraded"
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status": status, "version": cur.Version,
		"members": cur.NumMembers, "on_ring": cur.NumOnRing,
	})
}

type tupleReq struct {
	SrcIP   string `json:"src_ip"`
	DstIP   string `json:"dst_ip"`
	Proto   uint8  `json:"proto"`
	SrcPort uint16 `json:"src_port"`
	DstPort uint16 `json:"dst_port"`
}

func (s *Server) routeOne(w http.ResponseWriter, r *http.Request) {
	var req tupleReq
	if err := decodeBody(r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	f, err := flow.Parse(req.SrcIP, req.DstIP, req.Proto, req.SrcPort, req.DstPort)
	if err != nil {
		writeError(w, r, err)
		return
	}
	d, err := s.RT.Route(f)
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"version": d.Version, "flow_key": d.FlowKey, "flow_hash": fmt.Sprintf("%d", d.FlowHash),
		"member_id": d.MemberID, "reason": d.Reason,
	})
}

func (s *Server) routeBulk(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Flows []tupleReq `json:"flows"`
	}
	if err := decodeBody(r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	if len(req.Flows) == 0 {
		writeError(w, r, apperr.Invalid("EMPTY_BULK", "flows must contain at least one tuple"))
		return
	}
	if len(req.Flows) > 10000 {
		writeError(w, r, apperr.Invalid("BULK_TOO_LARGE", "at most 10000 tuples per bulk request"))
		return
	}
	type item struct {
		FlowKey  string `json:"flow_key"`
		MemberID string `json:"member_id,omitempty"`
		Error    string `json:"error,omitempty"`
		Code     string `json:"error_code,omitempty"`
	}
	out := make([]item, 0, len(req.Flows))
	ok := 0
	ver := s.RT.Current().Version
	for _, t := range req.Flows {
		f, err := flow.Parse(t.SrcIP, t.DstIP, t.Proto, t.SrcPort, t.DstPort)
		if err != nil {
			if ae, ok := apperr.As(err); ok {
				out = append(out, item{Error: ae.Message, Code: string(ae.Kind) + "/" + ae.Code})
			} else {
				out = append(out, item{Error: err.Error()})
			}
			continue
		}
		d, err := s.RT.Route(f)
		if err != nil {
			if ae, isAE := apperr.As(err); isAE {
				out = append(out, item{FlowKey: f.CanonicalKey(), Error: ae.Message, Code: string(ae.Kind) + "/" + ae.Code})
			} else {
				out = append(out, item{FlowKey: f.CanonicalKey(), Error: err.Error()})
			}
			continue
		}
		ok++
		out = append(out, item{FlowKey: d.FlowKey, MemberID: d.MemberID})
	}
	writeJSON(w, http.StatusOK, map[string]any{"version": ver, "resolved": ok, "results": out})
}

func (s *Server) getVersion(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.snapshotView())
}

func (s *Server) snapshotView() map[string]any {
	cur := s.RT.Current()
	bucket := map[string]float64{}
	vnodes := map[string]int{}
	for _, mi := range cur.Members {
		vnodes[mi.ID] = mi.VNodes
		if cur.Ring != nil {
			if sh, ok := cur.Ring.BucketShare(mi.ID); ok {
				bucket[mi.ID] = sh
			} else {
				bucket[mi.ID] = 0
			}
		}
	}
	return map[string]any{
		"version": cur.Version, "changed_at": cur.ChangedAt, "change": cur.Change,
		"members": cur.Members, "vnodes": vnodes, "bucket_share": bucket,
		"total_weight": cur.TotalWeight, "up_total_weight": cur.UpTotalWeight,
		"num_members": cur.NumMembers, "num_up": cur.NumUp, "num_on_ring": cur.NumOnRing,
	}
}

func (s *Server) getStats(w http.ResponseWriter, r *http.Request) {
	cur := s.RT.Current()
	c := s.RT.Counts()
	// Actual traffic share = realized fraction of routed flows over the
	// observed corpus. Computed from counters and clearly labeled so it is not
	// confused with bucket_share (the structural ring-arc fraction).
	traffic := map[string]float64{}
	if c.TotalRouted > 0 {
		for id, n := range c.PerMember {
			traffic[id] = float64(n) / float64(c.TotalRouted)
		}
	}
	bucket := map[string]float64{}
	for _, mi := range cur.Members {
		if cur.Ring != nil {
			if sh, ok := cur.Ring.BucketShare(mi.ID); ok {
				bucket[mi.ID] = sh
			} else {
				bucket[mi.ID] = 0
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"version":       cur.Version,
		"bucket_share":  bucket,  // structural vnode fraction
		"traffic_share": traffic, // observed realized fraction
		"counters":      c,
		"share_note":    "bucket_share is vnode count / total vnodes; traffic_share is realized routed flows since startup over the observed corpus and converges toward bucket share only as the corpus grows",
	})
}

type reloadReq struct {
	ExpectedVersion int64  `json:"expected_version"`
	Path            string `json:"path"` // optional explicit config path
}

func (s *Server) reloadConfig(w http.ResponseWriter, r *http.Request) {
	var req reloadReq
	if err := decodeBody(r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	path := req.Path
	if path == "" {
		path = s.ConfigPath
	}
	cfg, err := config.Load(path)
	if err != nil {
		writeError(w, r, err)
		return
	}
	// Preserve runtime down-state for members that still exist after reload.
	cur := s.RT.Current()
	down := map[string]bool{}
	reasons := map[string]string{}
	for _, mi := range cur.Members {
		if !mi.Up {
			down[mi.ID] = true
			reasons[mi.ID] = mi.DownReason
		}
	}
	next, changed, err := s.RT.ReplaceMembers(req.ExpectedVersion, cfg.Members, down, reasons)
	if err != nil {
		writeError(w, r, err)
		return
	}
	if changed {
		s.persistVersion(r.Context(), next, "reload:"+filepath.Base(path))
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"version": next.Version, "changed": changed, "snapshot": s.snapshotView(),
	})
}

type memberActionReq struct {
	ExpectedVersion int64  `json:"expected_version"`
	Reason          string `json:"reason"`
	Weight          int    `json:"weight"`
}

func (s *Server) memberDown(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var req memberActionReq
	if err := decodeBody(r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	next, changed, err := s.RT.SetDown(req.ExpectedVersion, id, req.Reason)
	if err != nil {
		writeError(w, r, err)
		return
	}
	if changed {
		s.persistVersion(r.Context(), next, "down:"+id)
	}
	writeJSON(w, http.StatusOK, map[string]any{"version": next.Version, "changed": changed})
}

func (s *Server) memberUp(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var req memberActionReq
	if err := decodeBody(r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	next, changed, err := s.RT.SetUp(req.ExpectedVersion, id)
	if err != nil {
		writeError(w, r, err)
		return
	}
	if changed {
		s.persistVersion(r.Context(), next, "recover:"+id)
	}
	writeJSON(w, http.StatusOK, map[string]any{"version": next.Version, "changed": changed})
}

func (s *Server) memberWeight(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var req memberActionReq
	if err := decodeBody(r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	next, changed, err := s.RT.SetWeight(req.ExpectedVersion, id, req.Weight)
	if err != nil {
		writeError(w, r, err)
		return
	}
	if changed {
		s.persistVersion(r.Context(), next, fmt.Sprintf("weight:%s=%d", id, req.Weight))
	}
	writeJSON(w, http.StatusOK, map[string]any{"version": next.Version, "changed": changed})
}

func (s *Server) listFlowSets(w http.ResponseWriter, r *http.Request) {
	names, err := s.Library.Names()
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"flow_sets": names})
}

type replayReq struct {
	FlowSet     string `json:"flow_set"`
	FromVersion int64  `json:"from_version"`
	ToVersion   int64  `json:"to_version"`
}

func (s *Server) createReplay(w http.ResponseWriter, r *http.Request) {
	var req replayReq
	if err := decodeBody(r, &req); err != nil {
		writeError(w, r, err)
		return
	}
	set, err := s.Library.Load(req.FlowSet)
	if err != nil {
		writeError(w, r, err)
		return
	}
	if req.FromVersion <= 0 || req.ToVersion <= 0 {
		writeError(w, r, apperr.Invalid("BAD_VERSIONS", "from_version and to_version must be >= 1"))
		return
	}
	res, err := s.Planner.Execute(r.Context(), set, req.FromVersion, req.ToVersion)
	if err != nil {
		// A classified replay failure was recorded in the run table; return it
		// as a normal 200 with status FAILED so clients can GET the report.
		if res != nil {
			writeJSON(w, http.StatusOK, res)
			return
		}
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) getReplay(w http.ResponseWriter, r *http.Request) {
	res, err := s.Planner.FetchRun(r.Context(), r.PathValue("runID"))
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func (s *Server) listRuns(w http.ResponseWriter, r *http.Request) {
	runs, err := s.Store.ListRuns(r.Context(), 50)
	if err != nil {
		writeError(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": runs})
}

// --- plumbing -------------------------------------------------------------

func decodeBody(r *http.Request, dst any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(io.LimitReader(r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return apperr.Invalid("BAD_JSON_BODY", "request body must be valid JSON matching the schema: "+err.Error())
	}
	if dec.More() {
		return apperr.Invalid("BAD_JSON_BODY", "request body must contain a single JSON object")
	}
	return nil
}

type errEnvelope struct {
	Error struct {
		Kind    string `json:"kind"`
		Code    string `json:"code"`
		Message string `json:"message"`
	} `json:"error"`
	RequestID string `json:"request_id"`
}

func writeError(w http.ResponseWriter, r *http.Request, err error) {
	env := errEnvelope{RequestID: requestIDFromContext(r)}
	var status int
	if ae, ok := apperr.As(err); ok {
		env.Error.Kind = string(ae.Kind)
		env.Error.Code = ae.Code
		env.Error.Message = ae.Message
		status = apperr.HTTPStatus(ae.Kind)
	} else {
		env.Error.Kind = "INTERNAL"
		env.Error.Code = "INTERNAL"
		env.Error.Message = err.Error()
		status = http.StatusInternalServerError
	}
	writeJSON(w, status, env)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

type reqIDKey struct{}

func requestIDFromContext(r *http.Request) string {
	if v, ok := r.Context().Value(reqIDKey{}).(string); ok {
		return v
	}
	return ""
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

func (s *Server) withRequestLog(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rid := newRequestID()
		ctx := context.WithValue(r.Context(), reqIDKey{}, rid)
		r = r.WithContext(ctx)
		w.Header().Set("X-Request-Id", rid)
		sw := &statusWriter{ResponseWriter: w, status: 200}
		start := time.Now()
		h.ServeHTTP(sw, r)
		s.Logger.Info("http", "request_id", rid, "method", r.Method,
			"path", r.URL.Path, "status", sw.status, "dur_ms", time.Since(start).Milliseconds())
	})
}

func newRequestID() string {
	return fmt.Sprintf("req-%d", time.Now().UnixNano())
}

// PersistVersion is called after every successful mutation to durably record
// the new generation. A persistence failure is logged but does not undo the
// in-memory transition; the next successful mutation re-persists. This trades
// strict durability for availability on a locked DB and is documented.
func (s *Server) persistVersion(ctx context.Context, snap *router.Snapshot, change string) {
	membersJSON, _ := json.Marshal(snap.Members)
	alloc := snap.Ring.Allocation()
	allocJSON, _ := json.Marshal(map[string]any{
		"counts": alloc.Counts, "base": alloc.Base, "extra": alloc.Extra,
		"total": alloc.Total, "strategy": alloc.Strategy,
		"ideal": alloc.Ideal, "remainder": alloc.Remainder,
		"vnodes_per_weight": alloc.VNodesPerW, "capped_to": alloc.CappedTo,
	})
	err := s.Store.InsertRingVersion(ctx, store.RingVersionRow{
		Version: snap.Version, CreatedAt: snap.ChangedAt, Change: change,
		MembersJSON: membersJSON, AllocationJSON: allocJSON,
		Fingerprint: snap.Fingerprint,
	})
	if err != nil {
		s.Logger.Warn("persist ring version failed", "version", snap.Version, "err", err)
	}
}

// PersistInitial writes the config revision and version-1 rows at startup.
func (s *Server) PersistInitial(ctx context.Context, rawConfig []byte, source, sha string, snap *router.Snapshot) error {
	if _, err := s.Store.InsertConfigRevision(ctx, store.ConfigRevision{
		LoadedAt: time.Now(), Source: source, SHA256: sha,
		Body: string(rawConfig), Note: "startup",
	}); err != nil {
		return err
	}
	s.persistVersion(ctx, snap, "initial_load")
	return nil
}
