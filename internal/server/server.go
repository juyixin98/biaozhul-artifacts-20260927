// Package server exposes the routing service over HTTP using only the
// standard library net/http (Go 1.22+ ServeMux patterns).
package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"sort"
	"sync"

	"flexhash/internal/config"
	"flexhash/internal/fherr"
	"flexhash/internal/flow"
	"flexhash/internal/hashring"
	"flexhash/internal/replay"
	"flexhash/internal/store"
)

// Service wires manager, store and config together.
type Service struct {
	mgr         *hashring.Manager
	st          *store.Store
	bucketCount int

	mu            sync.Mutex // serializes config/health writes
	currentConfig *config.Config
}

// NewService wires dependencies.
func NewService(mgr *hashring.Manager, st *store.Store, bucketCount int) *Service {
	return &Service{mgr: mgr, st: st, bucketCount: bucketCount}
}

// Routes registers all handlers on mux.
func (s *Service) Routes(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/lookup", s.handleLookup)
	mux.HandleFunc("POST /v1/config", s.handleUpdateConfig)
	mux.HandleFunc("GET /v1/config", s.handleGetConfig)
	mux.HandleFunc("POST /v1/members/{id}/health", s.handleSetHealth)
	mux.HandleFunc("GET /v1/assignments", s.handleAssignments)
	mux.HandleFunc("GET /v1/shares", s.handleShares)
	mux.HandleFunc("GET /v1/replay/verify", s.handleReplayVerify)
	mux.HandleFunc("GET /v1/replay/state", s.handleReplayState)
	mux.HandleFunc("GET /v1/runs", s.handleRuns)
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

// ---- lookup ----

type lookupResponse struct {
	Bucket        int    `json:"bucket"`
	Owner         string `json:"owner"`
	Chosen        string `json:"chosen"`
	Address       string `json:"address"`
	Failover      bool   `json:"failover"`
	ConfigVersion int64  `json:"config_version"`
	HealthRev     int64  `json:"health_revision"`
}

func (s *Service) handleLookup(w http.ResponseWriter, r *http.Request) {
	const op = "server.Lookup"
	body, err := readBody(r)
	if err != nil {
		writeErr(w, fherr.Wrap(fherr.KindInput, op, "cannot read body", err))
		return
	}
	t, err := flow.DecodeRequest(body)
	if err != nil {
		writeErr(w, err)
		return
	}
	key, err := t.Key()
	if err != nil {
		writeErr(w, err)
		return
	}
	d, err := s.mgr.Resolve(key)
	if err != nil {
		writeErr(w, err)
		return
	}
	if d.Chosen == "" {
		writeErr(w, fherr.New(fherr.KindUnavailable, op,
			"no healthy next hop with positive weight for this flow"))
		return
	}
	writeJSON(w, http.StatusOK, lookupResponse{
		Bucket:        d.Bucket,
		Owner:         d.Owner,
		Chosen:        d.Chosen,
		Address:       d.Address,
		Failover:      d.Failover,
		ConfigVersion: d.ConfigVersion,
		HealthRev:     d.HealthRev,
	})
}

// ---- config management ----

type updateConfigReq struct {
	Members []config.Member `json:"members"`
}

type updateConfigResp struct {
	Version     int64           `json:"version"`
	Moved       int             `json:"moved_buckets"`
	MovedSample []int           `json:"moved_sample"`
	Quota       map[string]int  `json:"quota"`
	Members     []config.Member `json:"members"`
}

func (s *Service) handleUpdateConfig(w http.ResponseWriter, r *http.Request) {
	const op = "server.UpdateConfig"
	body, err := readBody(r)
	if err != nil {
		writeErr(w, fherr.Wrap(fherr.KindInput, op, "cannot read body", err))
		return
	}
	var req updateConfigReq
	if err := json.Unmarshal(body, &req); err != nil {
		writeErr(w, fherr.Wrap(fherr.KindInput, op, "invalid JSON: "+err.Error(), err))
		return
	}
	next := &config.Config{
		BucketCount: s.bucketCount,
		Members:     req.Members,
	}
	if err := config.ValidateMembers(req.Members, s.bucketCount); err != nil {
		writeErr(w, err)
		return
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	ring, _, snapErr := s.mgr.Snapshot()
	if snapErr != nil {
		writeErr(w, snapErr)
		return
	}
	newVersion := ring.Version + 1

	coreMembers := toCoreMembers(req.Members)
	newRing, moved, applyErr := s.mgr.ApplyConfig(newVersion, coreMembers)
	if applyErr != nil {
		writeErr(w, applyErr)
		return
	}
	if err := s.persistConfig(r.Context(), newVersion, req.Members, newRing); err != nil {
		// Persistence failed after in-memory swap. Mark the process state as
		// diverged; the next restart replay will use storage truth. We
		// surface a 500 rather than silently accepting.
		writeErr(w, fherr.Wrap(fherr.KindResourceExhausted, op,
			"config applied in memory but persistence failed", err))
		return
	}
	s.currentConfig = mergeRuntimeConfig(s.currentConfig, next)

	sample := moved
	if len(sample) > 20 {
		sample = sample[:20]
	}
	writeJSON(w, http.StatusOK, updateConfigResp{
		Version:     newVersion,
		Moved:       len(moved),
		MovedSample: sample,
		Quota:       newRing.Quota(),
		Members:     sortedConfigMembers(req.Members),
	})
}

func (s *Service) persistConfig(ctx context.Context, version int64, ms []config.Member, ring *hashring.Ring) error {
	membersJSON, err := json.Marshal(toCoreMembers(ms))
	if err != nil {
		return fherr.Wrap(fherr.KindComputationFailed, "server.persistConfig",
			"encode members", err)
	}
	rows := make([]store.AssignmentRow, 0, ring.BucketCount)
	for _, a := range ring.Assignments() {
		rows = append(rows, store.AssignmentRow{
			Version: version, Bucket: a.Bucket, Member: a.Member,
		})
	}
	return s.st.SaveConfig(ctx, store.ConfigSnapshot{
		Version:     version,
		BucketCount: ring.BucketCount,
		MembersJSON: membersJSON,
	}, rows)
}

type configView struct {
	Version     int64           `json:"version"`
	BucketCount int             `json:"bucket_count"`
	HealthRev   int64           `json:"health_revision"`
	Members     []config.Member `json:"members"`
}

func (s *Service) handleGetConfig(w http.ResponseWriter, r *http.Request) {
	ring, hrev, err := s.mgr.Snapshot()
	if err != nil {
		writeErr(w, err)
		return
	}
	ms := make([]config.Member, 0, len(ring.Members))
	for _, m := range ring.Members {
		ms = append(ms, config.Member{
			ID: m.ID, Address: m.Address, Weight: m.Weight, Healthy: m.Healthy,
		})
	}
	sort.Slice(ms, func(i, j int) bool { return ms[i].ID < ms[j].ID })
	writeJSON(w, http.StatusOK, configView{
		Version:     ring.Version,
		BucketCount: ring.BucketCount,
		HealthRev:   hrev,
		Members:     ms,
	})
}

// ---- health ----

type healthReq struct {
	Healthy bool `json:"healthy"`
}

type healthResp struct {
	Member        string `json:"member"`
	Healthy       bool   `json:"healthy"`
	HealthRev     int64  `json:"health_revision"`
	ConfigVersion int64  `json:"config_version"`
}

func (s *Service) handleSetHealth(w http.ResponseWriter, r *http.Request) {
	const op = "server.SetHealth"
	id := r.PathValue("id")
	body, err := readBody(r)
	if err != nil {
		writeErr(w, fherr.Wrap(fherr.KindInput, op, "cannot read body", err))
		return
	}
	var req healthReq
	if err := json.Unmarshal(body, &req); err != nil {
		writeErr(w, fherr.Wrap(fherr.KindInput, op, "invalid JSON: "+err.Error(), err))
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	rev, err := s.mgr.SetHealth(id, req.Healthy)
	if err != nil {
		writeErr(w, err)
		return
	}
	if err := s.st.SaveHealth(r.Context(), rev, id, req.Healthy); err != nil {
		writeErr(w, fherr.Wrap(fherr.KindResourceExhausted, op,
			"health applied in memory but persistence failed", err))
		return
	}
	ring, _, _ := s.mgr.Snapshot()
	writeJSON(w, http.StatusOK, healthResp{
		Member: id, Healthy: req.Healthy, HealthRev: rev, ConfigVersion: ring.Version,
	})
}

// ---- assignments & shares ----

type assignmentResp struct {
	Version int64          `json:"version"`
	Buckets int            `json:"bucket_count"`
	Owners  map[string]int `json:"owners_bucket_count"`
}

func (s *Service) handleAssignments(w http.ResponseWriter, r *http.Request) {
	ring, hrev, err := s.mgr.Snapshot()
	if err != nil {
		writeErr(w, err)
		return
	}
	_ = hrev
	writeJSON(w, http.StatusOK, assignmentResp{
		Version: ring.Version,
		Buckets: ring.BucketCount,
		Owners:  ring.Quota(),
	})
}

// shareEntry distinguishes the two meanings of "share":
//   - BucketShare: fraction of the 1024 structural buckets owned.
//   - EffectiveShare: fraction of live failover-aware traffic the member can
//     currently serve (structural share plus failover capacity while others
//     are down), as observed against a caller-supplied flow set via the
//     evaluation endpoint; here we report the structural view and live
//     eligibility flags. The distribution tests compute the realized share.
type shareEntry struct {
	Weight            int     `json:"weight"`
	Healthy           bool    `json:"healthy"`
	BucketQuota       int     `json:"bucket_quota"`
	BucketShare       float64 `json:"bucket_share"`
	ConfigWeightShare float64 `json:"config_weight_share"`
}

type sharesResp struct {
	Version     int64                 `json:"version"`
	TotalWeight int                   `json:"total_weight"`
	BucketCount int                   `json:"bucket_count"`
	Shares      map[string]shareEntry `json:"shares"`
	Note        string                `json:"note"`
}

func (s *Service) handleShares(w http.ResponseWriter, r *http.Request) {
	ring, _, err := s.mgr.Snapshot()
	if err != nil {
		writeErr(w, err)
		return
	}
	total := 0
	for _, m := range ring.Members {
		total += m.Weight
	}
	out := map[string]shareEntry{}
	for id, m := range ring.Members {
		q := ring.Quota()[id]
		e := shareEntry{
			Weight:      m.Weight,
			Healthy:     m.Healthy,
			BucketQuota: q,
			BucketShare: float64(q) / float64(ring.BucketCount),
		}
		if total > 0 {
			e.ConfigWeightShare = float64(m.Weight) / float64(total)
		}
		out[id] = e
	}
	writeJSON(w, http.StatusOK, sharesResp{
		Version:     ring.Version,
		TotalWeight: total,
		BucketCount: ring.BucketCount,
		Shares:      out,
		Note: "bucket_share is structural ownership; realized traffic share " +
			"depends on the actual flow distribution and on failover when " +
			"members are down. Use GET /v1/replay/state or the evaluation " +
			"test harness for realized shares.",
	})
}

// ---- replay ----

func (s *Service) handleReplayVerify(w http.ResponseWriter, r *http.Request) {
	_, rep, err := replay.VerifyAssignments(r.Context(), s.st)
	if err != nil {
		writeErr(w, err)
		return
	}
	status := http.StatusOK
	result := "consistent"
	if len(rep.Mismatches) > 0 {
		status = http.StatusConflict
		result = "diverged"
	}
	writeJSON(w, status, map[string]any{
		"result":                result,
		"events_replayed":       rep.EventsReplayed,
		"final_version":         rep.FinalVersion,
		"final_health_revision": rep.FinalHealthRev,
		"mismatches":            rep.Mismatches,
	})
}

func (s *Service) handleReplayState(w http.ResponseWriter, r *http.Request) {
	mgr, rep, err := replay.Rebuild(r.Context(), s.st)
	if err != nil {
		writeErr(w, err)
		return
	}
	ring, hrev := mgr.Current()
	health := map[string]bool{}
	for id, m := range ring.Members {
		health[id] = m.Healthy
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"events_replayed": rep.EventsReplayed,
		"final_version":   ring.Version,
		"health_revision": hrev,
		"bucket_count":    ring.BucketCount,
		"quota":           ring.Quota(),
		"health":          health,
	})
}

// ---- run logs ----

func (s *Service) handleRuns(w http.ResponseWriter, r *http.Request) {
	logs, err := s.st.RunLogs(r.Context(), 100)
	if err != nil {
		writeErr(w, err)
		return
	}
	out := make([]map[string]any, 0, len(logs))
	for _, l := range logs {
		var detail any
		_ = json.Unmarshal(l.DetailJSON, &detail)
		out = append(out, map[string]any{
			"run_id": l.RunID, "test_name": l.TestName,
			"result": l.Result, "detail": detail, "created_at": l.CreatedAt,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"runs": out})
}

// ---- helpers ----

func readBody(r *http.Request) ([]byte, error) {
	r.Body = http.MaxBytesReader(nil, r.Body, 1<<20)
	b, err := io.ReadAll(r.Body)
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			return nil, fherr.New(fherr.KindInput, "server.readBody",
				"request body exceeds 1 MiB")
		}
		return nil, fherr.Wrap(fherr.KindInput, "server.readBody", "cannot read body", err)
	}
	return b, nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

type errBody struct {
	Error string `json:"error"`
	Kind  string `json:"kind"`
}

func writeErr(w http.ResponseWriter, err error) {
	kind := fherr.KindOf(err)
	status := http.StatusInternalServerError
	switch kind {
	case fherr.KindInput:
		status = http.StatusBadRequest
	case fherr.KindStateConflict:
		status = http.StatusConflict
	case fherr.KindResourceExhausted:
		status = http.StatusServiceUnavailable
	case fherr.KindComputationFailed:
		status = http.StatusInternalServerError
	case fherr.KindUnavailable:
		status = http.StatusServiceUnavailable
	default:
		status = http.StatusInternalServerError
	}
	if errors.Is(err, context.Canceled) {
		status = 499
	}
	writeJSON(w, status, errBody{Error: err.Error(), Kind: kind.String()})
}

func toCoreMembers(ms []config.Member) []hashring.Member {
	out := make([]hashring.Member, 0, len(ms))
	for _, m := range ms {
		out = append(out, hashring.Member{
			ID: m.ID, Address: m.Address, Weight: m.Weight, Healthy: m.Healthy,
		})
	}
	return out
}

func sortedConfigMembers(ms []config.Member) []config.Member {
	out := append([]config.Member(nil), ms...)
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out
}

func mergeRuntimeConfig(base *config.Config, next *config.Config) *config.Config {
	if base == nil {
		return next
	}
	cp := *base
	cp.Members = next.Members
	return &cp
}

// SetCurrentConfig records the boot configuration (called by main on startup).
func (s *Service) SetCurrentConfig(c *config.Config) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.currentConfig = c
}
