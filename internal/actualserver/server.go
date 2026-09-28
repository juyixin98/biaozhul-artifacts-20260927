package actualserver

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"crcontroller/internal/actualstore"
	"crcontroller/internal/logx"
)

// Server is the actual resource plane.
type Server struct {
	store  *actualstore.Store
	faults *faultManager
	log    *logx.Logger
	mux    *http.ServeMux
}

// New wires handlers against an actual-plane store.
func New(st *actualstore.Store, logger *logx.Logger) *Server {
	s := &Server{
		store:  st,
		faults: newFaultManager(),
		log:    logger,
		mux:    http.NewServeMux(),
	}
	s.routes()
	return s
}

// Handler exposes the router.
func (s *Server) Handler() http.Handler { return s.mux }

func (s *Server) routes() {
	s.mux.HandleFunc("/healthz", s.health)

	// V1 resource API.
	s.mux.HandleFunc("/v1/resources", s.create)
	s.mux.HandleFunc("/v1/resources/", func(w http.ResponseWriter, r *http.Request) {
		rest := strings.TrimPrefix(r.URL.Path, "/v1/resources/")
		switch {
		case strings.HasPrefix(rest, "by-owner/"):
			owner := strings.TrimPrefix(rest, "by-owner/")
			if strings.Contains(owner, "/") {
				writeJSON(w, http.StatusNotFound, errorBody("not found"))
				return
			}
			s.getByOwner(w, r, owner)
		default:
			id := strings.SplitN(rest, "/", 2)[0]
			switch r.Method {
			case http.MethodGet:
				s.get(w, r, id)
			case http.MethodPut:
				s.update(w, r, id)
			case http.MethodDelete:
				s.delete(w, r, id)
			default:
				writeJSON(w, http.StatusMethodNotAllowed, errorBody("method not allowed"))
			}
		}
	})

	// Test-only diagnostics and fault control.
	s.mux.HandleFunc("/admin/faults", s.faultsHandler)
	s.mux.HandleFunc("/admin/faults/", s.faultItemHandler)
	s.mux.HandleFunc("/admin/counters", s.countersHandler)
	s.mux.HandleFunc("/admin/request-log", s.requestLogHandler)
	s.mux.HandleFunc("/admin/resources", s.listResources)
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type createReq struct {
	ID         string         `json:"id"`
	OwnerUID   string         `json:"ownerUID"`
	Generation int64          `json:"generation"`
	SpecHash   string         `json:"specHash"`
	Spec       map[string]any `json:"spec"`
}

func (s *Server) create(w http.ResponseWriter, r *http.Request) {
	rid := ensureRequestID(w, r)
	if r.Method != http.MethodPost {
		s.fail(w, r, http.StatusMethodNotAllowed, rid, "", errorBody("method not allowed"))
		return
	}
	var req createReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.fail(w, r, http.StatusBadRequest, rid, "", errorBody("invalid JSON"))
		return
	}
	if req.ID == "" || req.OwnerUID == "" {
		s.fail(w, r, http.StatusBadRequest, rid, "",
			errorBody("id and ownerUID are required"))
		return
	}

	// Fault: lost create response. The row is committed first; the client
	// must claim it via /by-owner instead of creating a duplicate.
	if fault, ok := s.faults.get(req.OwnerUID); ok && fault == FaultCreateResponseLoss {
		created, err := s.store.Create(actualstore.CreateInput{
			ID: req.ID, OwnerUID: req.OwnerUID, Generation: req.Generation,
			SpecHash: req.SpecHash, Spec: req.Spec,
		})
		committed := err == nil || errors.Is(err, actualstore.ErrAlreadyOwned)
		_, _ = s.store.BumpCounter(r.Context(), "creates.committed", 1)
		_, _ = s.store.BumpCounter(r.Context(), "creates.attempted", 1)
		if created != nil {
			s.log.Warn("actual", "create committed but response will be lost",
				map[string]any{
					"requestID": rid, "ownerUID": req.OwnerUID,
					"id": created.ID, "spec": req.Spec, "fault": fault,
				})
		}
		w.Header().Set("X-Fault", fault)
		w.Header().Set("X-Request-ID", rid)
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(errorBody(
			"fault: create committed, response lost (claim by ownerUID)"))
		_ = s.logRequest(r, http.StatusInternalServerError, rid, fault, req.Spec)
		_ = committed
		return
	}

	created, err := s.store.Create(actualstore.CreateInput{
		ID: req.ID, OwnerUID: req.OwnerUID, Generation: req.Generation,
		SpecHash: req.SpecHash, Spec: req.Spec,
	})
	if errors.Is(err, actualstore.ErrAlreadyOwned) {
		// Idempotent create: surface the existing row with 409 so the client
		// claims it. No second resource is ever created.
		_, _ = s.store.BumpCounter(r.Context(), "creates.duplicate_suppressed", 1)
		_, _ = s.store.BumpCounter(r.Context(), "creates.attempted", 1)
		w.Header().Set("X-Request-ID", rid)
		writeJSON(w, http.StatusConflict, created)
		_ = s.logRequest(r, http.StatusConflict, rid, "", req.Spec)
		return
	}
	if err != nil {
		s.fail(w, r, http.StatusInternalServerError, rid, "",
			errorBody("create failed: "+err.Error()))
		return
	}
	_, _ = s.store.BumpCounter(r.Context(), "creates.attempted", 1)
	_, _ = s.store.BumpCounter(r.Context(), "creates.committed", 1)
	w.Header().Set("X-Request-ID", rid)
	writeJSON(w, http.StatusCreated, created)
	_ = s.logRequest(r, http.StatusCreated, rid, "", req.Spec)
	s.log.Info("actual", "resource created", map[string]any{
		"requestID": rid, "ownerUID": req.OwnerUID, "id": created.ID,
		"spec": req.Spec,
	})
}

func (s *Server) getByOwner(w http.ResponseWriter, r *http.Request, owner string) {
	rid := ensureRequestID(w, r)
	res, err := s.store.GetByOwner(owner)
	if errors.Is(err, actualstore.ErrNotFound) {
		s.fail(w, r, http.StatusNotFound, rid, "", errorBody("not found"))
		return
	}
	if err != nil {
		s.fail(w, r, http.StatusInternalServerError, rid, "",
			errorBody(err.Error()))
		return
	}
	w.Header().Set("X-Request-ID", rid)
	writeJSON(w, http.StatusOK, res)
	_ = s.logRequest(r, http.StatusOK, rid, "", nil)
}

func (s *Server) get(w http.ResponseWriter, r *http.Request, id string) {
	rid := ensureRequestID(w, r)
	res, err := s.store.Get(id)
	if errors.Is(err, actualstore.ErrNotFound) {
		s.fail(w, r, http.StatusNotFound, rid, "", errorBody("not found"))
		return
	}
	if err != nil {
		s.fail(w, r, http.StatusInternalServerError, rid, "",
			errorBody(err.Error()))
		return
	}
	// Fault: serve an older snapshot (last pre-update version).
	if fault, ok := s.faults.get(res.OwnerUID); ok && fault == FaultStaleGet {
		if snap, hasSnap, serr := s.store.LatestSnapshot(id); serr == nil && hasSnap {
			w.Header().Set("X-Served-Snapshot", "true")
			w.Header().Set("X-Current-Version", strconv.FormatInt(res.Version, 10))
			w.Header().Set("X-Fault", fault)
			w.Header().Set("X-Request-ID", rid)
			writeJSON(w, http.StatusOK, snap)
			_ = s.logRequest(r, http.StatusOK, rid, fault, nil)
			s.log.Warn("actual", "serving stale snapshot", map[string]any{
				"requestID": rid, "id": id,
				"snapshotVersion": snap.Version, "currentVersion": res.Version,
			})
			return
		}
	}
	w.Header().Set("X-Request-ID", rid)
	writeJSON(w, http.StatusOK, res)
	_ = s.logRequest(r, http.StatusOK, rid, "", nil)
}

type updateReq struct {
	Generation int64          `json:"generation"`
	SpecHash   string         `json:"specHash"`
	Spec       map[string]any `json:"spec"`
}

func (s *Server) update(w http.ResponseWriter, r *http.Request, id string) {
	rid := ensureRequestID(w, r)
	cur, err := s.store.Get(id)
	if errors.Is(err, actualstore.ErrNotFound) {
		s.fail(w, r, http.StatusNotFound, rid, "", errorBody("not found"))
		return
	}
	if err != nil {
		s.fail(w, r, http.StatusInternalServerError, rid, "",
			errorBody(err.Error()))
		return
	}
	var req updateReq
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		s.fail(w, r, http.StatusBadRequest, rid, "", errorBody("invalid JSON"))
		return
	}
	expected := parseExpected(r.URL.Query().Get("expectedVersion"))

	if fault, ok := s.faults.get(cur.OwnerUID); ok && fault == FaultUpdateConflict {
		w.Header().Set("X-Fault", fault)
		w.Header().Set("X-Request-ID", rid)
		w.WriteHeader(http.StatusPreconditionFailed)
		_ = json.NewEncoder(w).Encode(errorBody(
			"fault: version conflict injected; currentVersion=" +
				strconv.FormatInt(cur.Version, 10)))
		_, _ = s.store.BumpCounter(r.Context(), "updates.conflict", 1)
		_, _ = s.store.BumpCounter(r.Context(), "updates.attempted", 1)
		_ = s.logRequest(r, http.StatusPreconditionFailed, rid, fault, req.Spec)
		return
	}

	in := actualstore.UpdateInput{
		ID: id, ExpectedVer: expected, Generation: req.Generation,
		SpecHash: req.SpecHash, Spec: req.Spec,
	}

	// Every update snapshots the prior row first, so the stale-get fault can
	// later serve the version this controller has already advanced past.
	if fault, ok := s.faults.get(cur.OwnerUID); ok && fault == FaultUpdateResponseLoss {
		updated, uerr := s.store.SnapshotBeforeUpdate(in)
		if errors.Is(uerr, actualstore.ErrVersionConflict) {
			s.preconditionFailed(w, r, rid, cur, FaultUpdateConflict, req.Spec)
			return
		}
		if uerr != nil {
			s.fail(w, r, http.StatusInternalServerError, rid, "",
				errorBody(uerr.Error()))
			return
		}
		_, _ = s.store.BumpCounter(r.Context(), "updates.attempted", 1)
		_, _ = s.store.BumpCounter(r.Context(), "updates.committed", 1)
		w.Header().Set("X-Fault", fault)
		w.Header().Set("X-Request-ID", rid)
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(errorBody(
			"fault: update committed, response lost"))
		_ = s.logRequest(r, http.StatusInternalServerError, rid, fault, req.Spec)
		s.log.Warn("actual", "update committed, response lost", map[string]any{
			"requestID": rid, "id": id, "newVersion": updated.Version,
		})
		return
	}

	updated, uerr := s.store.SnapshotBeforeUpdate(in)
	if errors.Is(uerr, actualstore.ErrVersionConflict) {
		s.preconditionFailed(w, r, rid, cur, "", req.Spec)
		return
	}
	if uerr != nil {
		s.fail(w, r, http.StatusInternalServerError, rid, "",
			errorBody(uerr.Error()))
		return
	}
	_, _ = s.store.BumpCounter(r.Context(), "updates.attempted", 1)
	_, _ = s.store.BumpCounter(r.Context(), "updates.committed", 1)
	w.Header().Set("X-Request-ID", rid)
	writeJSON(w, http.StatusOK, updated)
	_ = s.logRequest(r, http.StatusOK, rid, "", req.Spec)
}

func (s *Server) preconditionFailed(w http.ResponseWriter, r *http.Request,
	rid string, cur *actualstore.Resource, fault string, spec map[string]any) {
	w.Header().Set("X-Request-ID", rid)
	if fault != "" {
		w.Header().Set("X-Fault", fault)
	}
	w.WriteHeader(http.StatusPreconditionFailed)
	_ = json.NewEncoder(w).Encode(map[string]any{
		"error":          "version conflict",
		"currentVersion": cur.Version,
	})
	_, _ = s.store.BumpCounter(r.Context(), "updates.attempted", 1)
	_, _ = s.store.BumpCounter(r.Context(), "updates.conflict", 1)
	_ = s.logRequest(r, http.StatusPreconditionFailed, rid, fault, spec)
}

func (s *Server) delete(w http.ResponseWriter, r *http.Request, id string) {
	rid := ensureRequestID(w, r)
	cur, err := s.store.Get(id)
	if errors.Is(err, actualstore.ErrNotFound) {
		// Idempotent delete: already gone.
		w.Header().Set("X-Request-ID", rid)
		w.WriteHeader(http.StatusNoContent)
		_, _ = s.store.BumpCounter(r.Context(), "deletes.attempted", 1)
		_, _ = s.store.BumpCounter(r.Context(), "deletes.not_found", 1)
		_ = s.logRequest(r, http.StatusNoContent, rid, "", nil)
		return
	}
	if err != nil {
		s.fail(w, r, http.StatusInternalServerError, rid, "",
			errorBody(err.Error()))
		return
	}

	if fault, ok := s.faults.get(cur.OwnerUID); ok && fault == FaultDeleteFailed {
		w.Header().Set("X-Fault", fault)
		w.Header().Set("X-Request-ID", rid)
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(errorBody("fault: delete failed (row kept)"))
		_, _ = s.store.BumpCounter(r.Context(), "deletes.attempted", 1)
		_, _ = s.store.BumpCounter(r.Context(), "deletes.failed", 1)
		_ = s.logRequest(r, http.StatusInternalServerError, rid, fault, nil)
		return
	}

	if fault, ok := s.faults.get(cur.OwnerUID); ok && fault == FaultDeleteResponseLoss {
		derr := s.store.Delete(id, 0)
		if derr != nil && !errors.Is(derr, actualstore.ErrNotFound) {
			s.fail(w, r, http.StatusInternalServerError, rid, "",
				errorBody(derr.Error()))
			return
		}
		w.Header().Set("X-Fault", fault)
		w.Header().Set("X-Request-ID", rid)
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(errorBody(
			"fault: delete committed, response lost"))
		_, _ = s.store.BumpCounter(r.Context(), "deletes.attempted", 1)
		_, _ = s.store.BumpCounter(r.Context(), "deletes.committed", 1)
		_ = s.logRequest(r, http.StatusInternalServerError, rid, fault, nil)
		return
	}

	expected := parseExpected(r.URL.Query().Get("expectedVersion"))
	if derr := s.store.Delete(id, expected); derr != nil {
		if errors.Is(derr, actualstore.ErrVersionConflict) {
			w.Header().Set("X-Request-ID", rid)
			w.WriteHeader(http.StatusPreconditionFailed)
			_ = json.NewEncoder(w).Encode(map[string]any{
				"error":          "version conflict",
				"currentVersion": cur.Version,
			})
			_, _ = s.store.BumpCounter(r.Context(), "deletes.attempted", 1)
			_ = s.logRequest(r, http.StatusPreconditionFailed, rid, "", nil)
			return
		}
		if errors.Is(derr, actualstore.ErrNotFound) {
			w.Header().Set("X-Request-ID", rid)
			w.WriteHeader(http.StatusNoContent)
			return
		}
		s.fail(w, r, http.StatusInternalServerError, rid, "",
			errorBody(derr.Error()))
		return
	}
	w.Header().Set("X-Request-ID", rid)
	w.WriteHeader(http.StatusNoContent)
	_, _ = s.store.BumpCounter(r.Context(), "deletes.attempted", 1)
	_, _ = s.store.BumpCounter(r.Context(), "deletes.committed", 1)
	_ = s.logRequest(r, http.StatusNoContent, rid, "", nil)
}

// ---- admin / diagnostics ----

type faultSetReq struct {
	OwnerUID string `json:"ownerUID"`
	Fault    string `json:"fault"`
}

func (s *Server) faultsHandler(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, s.faults.snapshot())
	case http.MethodPost, http.MethodPut:
		var req faultSetReq
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(w, http.StatusBadRequest, errorBody("invalid JSON"))
			return
		}
		if req.OwnerUID == "" {
			writeJSON(w, http.StatusBadRequest, errorBody("ownerUID required"))
			return
		}
		if req.Fault != "" && !ValidFaults[req.Fault] {
			writeJSON(w, http.StatusBadRequest, errorBody("unknown fault: "+req.Fault))
			return
		}
		s.faults.set(req.OwnerUID, req.Fault)
		writeJSON(w, http.StatusOK, map[string]any{
			"ownerUID": req.OwnerUID, "fault": req.Fault,
		})
	case http.MethodDelete:
		s.faults.clear()
		w.WriteHeader(http.StatusNoContent)
	default:
		writeJSON(w, http.StatusMethodNotAllowed, errorBody("method not allowed"))
	}
}

func (s *Server) faultItemHandler(w http.ResponseWriter, r *http.Request) {
	owner := strings.TrimPrefix(r.URL.Path, "/admin/faults/")
	if owner == "" || strings.Contains(owner, "/") {
		writeJSON(w, http.StatusNotFound, errorBody("not found"))
		return
	}
	switch r.Method {
	case http.MethodGet:
		if f, ok := s.faults.get(owner); ok {
			writeJSON(w, http.StatusOK, map[string]string{"ownerUID": owner, "fault": f})
			return
		}
		writeJSON(w, http.StatusNotFound, errorBody("no fault"))
	case http.MethodDelete:
		s.faults.set(owner, "")
		w.WriteHeader(http.StatusNoContent)
	default:
		writeJSON(w, http.StatusMethodNotAllowed, errorBody("method not allowed"))
	}
}

func (s *Server) countersHandler(w http.ResponseWriter, r *http.Request) {
	counters, err := s.store.Counters()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorBody(err.Error()))
		return
	}
	writeJSON(w, http.StatusOK, counters)
}

func (s *Server) requestLogHandler(w http.ResponseWriter, r *http.Request) {
	limit := 100
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	entries, err := s.store.RequestLog(limit)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorBody(err.Error()))
		return
	}
	writeJSON(w, http.StatusOK, entries)
}

func (s *Server) listResources(w http.ResponseWriter, r *http.Request) {
	rows, err := s.store.List(1000)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, errorBody(err.Error()))
		return
	}
	// Independent verification view: specs are included because this admin
	// endpoint exists for tests/fixtures on localhost only.
	writeJSON(w, http.StatusOK, rows)
}

// ---- plumbing ----

func (s *Server) fail(w http.ResponseWriter, r *http.Request, status int,
	rid string, fault string, body any) {
	w.Header().Set("X-Request-ID", rid)
	if fault != "" {
		w.Header().Set("X-Fault", fault)
	}
	writeJSON(w, status, body)
	_ = s.logRequest(r, status, rid, fault, nil)
}

func (s *Server) logRequest(r *http.Request, status int, rid, fault string,
	body map[string]any) error {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	redacted := ""
	if body != nil {
		b, _ := json.Marshal(logx.RedactSpec(body))
		redacted = string(b)
	}
	return s.store.LogRequest(ctx, actualstore.RequestLogEntry{
		RequestID: rid, Method: r.Method, Path: r.URL.Path,
		StatusCode: status, Fault: fault, BodyRedacted: redacted,
		CreatedAt: time.Now().UTC().Format(time.RFC3339Nano),
	})
}

func parseExpected(v string) int64 {
	if v == "" {
		return 0
	}
	n, err := strconv.ParseInt(v, 10, 64)
	if err != nil {
		return 0
	}
	return n
}
