package fakecloud

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"resourcecontroller/internal/diag"
)

// Service is the HTTP front of the actual resource service.
type Service struct {
	log *diag.Logger

	mu     sync.Mutex
	data   *store
	faults map[string]*fault // keyed by operation: create|update|get|delete
}

// NewService builds an empty actual-resource service.
func NewService(log *diag.Logger) *Service {
	return &Service{
		log:    log,
		data:   newStore(),
		faults: map[string]*fault{},
	}
}

// Handler returns the routed HTTP handler, ready to be served in-process or
// by the standalone binary.
func (s *Service) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /v1/widgets", s.handleList)
	mux.HandleFunc("PUT /v1/widgets/{id}", s.handlePut)
	mux.HandleFunc("GET /v1/widgets/{id}", s.handleGet)
	mux.HandleFunc("DELETE /v1/widgets/{id}", s.handleDelete)
	mux.HandleFunc("PUT /internal/faults/{op}", s.handleFaultSet)
	mux.HandleFunc("DELETE /internal/faults/{op}", s.handleFaultClearOne)
	mux.HandleFunc("DELETE /internal/faults", s.handleFaultClearAll)
	mux.HandleFunc("GET /internal/status", s.handleStatus)
	return s.log.Middleware(mux)
}

type errorBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": errorBody{Code: code, Message: msg}})
}

func (s *Service) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

// upsertRequest is the body of PUT /v1/widgets/{id}.
type upsertRequest struct {
	Name string `json:"name"`
	Spec Spec   `json:"spec"`
}

func (s *Service) handlePut(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	id := r.PathValue("id")
	if id == "" {
		writeError(w, http.StatusBadRequest, "MissingId", "resource id is required")
		return
	}
	var req upsertRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "BadBody", "invalid JSON body")
		return
	}
	if req.Name == "" || req.Spec.Replicas < 0 || req.Spec.Color == "" {
		writeError(w, http.StatusBadRequest, "ValidationFailed",
			"name, positive color and non-negative replicas are required")
		return
	}
	idempotencyKey := r.Header.Get("Idempotency-Key")

	// "slow" applies to both create and update paths.
	_ = s.consumeSlow("create")

	s.data.mu.Lock()
	defer s.data.mu.Unlock()

	existing, ok := s.data.byID[id]
	if !ok {
		// Idempotency-key collision on a different physical id means someone
		// retried with a conflicting key; refuse rather than create twice.
		if idempotencyKey != "" {
			for _, other := range s.data.byID {
				if other.IdempotencyKey == idempotencyKey && other.ID != id {
					writeError(w, http.StatusConflict, "IdempotencyConflict",
						"idempotency key already used by another resource")
					return
				}
			}
		}
		// createFail: the service fails *before* committing anything.
		if s.faultActive("create", FaultCreateFail) {
			s.bumpFault("create")
			s.log.Warn(ctx, "fakecloud create injected failure",
				"externalId", id, "kind", FaultCreateFail)
			writeError(w, http.StatusServiceUnavailable, "CreateFailed",
				"injected failure: create rejected by service")
			return
		}
		now := time.Now().UTC()
		res := &Resource{
			ID:             id,
			Name:           req.Name,
			IdempotencyKey: idempotencyKey,
			Version:        1,
			Spec:           req.Spec,
			SpecFP:         Fingerprint(req.Spec),
			CreatedAt:      now,
			UpdatedAt:      now,
		}
		s.data.byID[id] = res
		s.log.Info(ctx, "fakecloud resource created",
			"externalId", id, "idempotencyKey", idempotencyKey,
			"secretToken", diag.Secret(req.Spec.SecretToken), "version", res.Version)

		// createResponseLost: the resource is committed durably, but the
		// client is told the outcome is unknown. A correct controller must
		// claim by GET rather than create again.
		if s.faultActive("create", FaultCreateResponseLost) {
			s.bumpFault("create")
			s.log.Warn(ctx, "fakecloud create response lost after commit",
				"externalId", id, "kind", FaultCreateResponseLost)
			writeError(w, http.StatusInternalServerError, "ResponseLost",
				"injected failure: create committed but response was lost")
			return
		}
		writeJSON(w, http.StatusCreated, res)
		return
	}

	// Resource exists: this is either an idempotent create replay or an update.
	if ifMatch := r.Header.Get("If-Match"); ifMatch != "" {
		want, err := strconv.ParseInt(strings.TrimSpace(ifMatch), 10, 64)
		if err != nil {
			writeError(w, http.StatusBadRequest, "BadVersion", "If-Match must be an integer")
			return
		}
		if want != existing.Version {
			writeError(w, http.StatusConflict, "VersionConflict",
				fmt.Sprintf("expected version %d but current is %d", want, existing.Version))
			return
		}
	}
	// Idempotent replay of the original create: same key, same spec.
	if existing.IdempotencyKey != "" && existing.IdempotencyKey == idempotencyKey &&
		existing.SpecFP == Fingerprint(req.Spec) {
		writeJSON(w, http.StatusOK, existing)
		return
	}

	// A real spec update. Capture the pre-update state while a stale-get fault
	// is armed, so a later GET can plausibly return the old observation.
	if s.faultActive("get", FaultStaleGet) {
		s.faults["get"].snapshot = s.storeCloneLocked(existing)
	}
	before := existing.Version
	existing.Spec = req.Spec
	existing.SpecFP = Fingerprint(req.Spec)
	existing.Version++
	existing.UpdatedAt = time.Now().UTC()
	s.log.Info(ctx, "fakecloud resource updated",
		"externalId", id, "versionBefore", before, "versionAfter", existing.Version,
		"secretToken", diag.Secret(req.Spec.SecretToken))
	writeJSON(w, http.StatusOK, existing)
}

func (s *Service) handleGet(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	id := r.PathValue("id")

	_ = s.consumeSlow("get")

	s.data.mu.Lock()
	defer s.data.mu.Unlock()

	if s.faultActive("get", FaultGetFail) {
		s.bumpFault("get")
		s.log.Warn(ctx, "fakecloud get injected failure", "externalId", id)
		writeError(w, http.StatusServiceUnavailable, "GetFailed", "injected failure on get")
		return
	}

	res, ok := s.data.byID[id]
	if !ok {
		writeError(w, http.StatusNotFound, "NotFound", "no such resource")
		return
	}
	// staleGet: return the captured pre-update observation once.
	if s.faultActive("get", FaultStaleGet) {
		if ft := s.faults["get"]; ft.snapshot != nil {
			old := ft.snapshot
			ft.snapshot = nil
			s.bumpFault("get")
			s.log.Warn(ctx, "fakecloud get returned stale observation",
				"externalId", id, "servedVersion", old.Version, "currentVersion", res.Version)
			writeJSON(w, http.StatusOK, old)
			return
		}
	}
	writeJSON(w, http.StatusOK, s.storeCloneLocked(res))
}

func (s *Service) handleDelete(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	id := r.PathValue("id")

	_ = s.consumeSlow("delete")

	s.data.mu.Lock()
	defer s.data.mu.Unlock()

	if _, ok := s.data.byID[id]; !ok {
		// Deletes are idempotent: deleting a missing resource is success.
		w.WriteHeader(http.StatusNoContent)
		return
	}
	if s.faultActive("delete", FaultDeleteFail) {
		s.bumpFault("delete")
		s.log.Warn(ctx, "fakecloud delete injected failure", "externalId", id)
		writeError(w, http.StatusServiceUnavailable, "DeleteFailed",
			"injected failure: external resource still present")
		return
	}
	delete(s.data.byID, id)
	s.log.Info(ctx, "fakecloud resource deleted", "externalId", id)
	w.WriteHeader(http.StatusNoContent)
}

func (s *Service) handleList(w http.ResponseWriter, r *http.Request) {
	s.data.mu.Lock()
	out := make([]*Resource, 0, len(s.data.byID))
	for _, res := range s.data.byID {
		out = append(out, s.storeCloneLocked(res))
	}
	s.data.mu.Unlock()
	writeJSON(w, http.StatusOK, map[string]any{"items": out})
}

func (s *Service) handleFaultSet(w http.ResponseWriter, r *http.Request) {
	op := r.PathValue("op")
	if op != "create" && op != "update" && op != "get" && op != "delete" {
		writeError(w, http.StatusBadRequest, "UnknownOp", "op must be create|update|get|delete")
		return
	}
	var cfg FaultConfig
	if err := json.NewDecoder(r.Body).Decode(&cfg); err != nil {
		writeError(w, http.StatusBadRequest, "BadBody", "invalid JSON body")
		return
	}
	switch cfg.Kind {
	case FaultCreateResponseLost, FaultCreateFail, FaultDeleteFail, FaultStaleGet, FaultGetFail, FaultSlow:
	default:
		writeError(w, http.StatusBadRequest, "UnknownFault", "unknown fault kind: "+cfg.Kind)
		return
	}
	times := cfg.Times
	if times <= 0 {
		times = 1
	}
	s.mu.Lock()
	s.faults[op] = &fault{Kind: cfg.Kind, Remaining: times, DelayMS: cfg.DelayMS}
	s.mu.Unlock()
	s.log.Info(r.Context(), "fault armed", "op", op, "kind", cfg.Kind, "times", times)
	writeJSON(w, http.StatusOK, map[string]any{"armed": true, "op": op, "kind": cfg.Kind, "times": times})
}

func (s *Service) handleFaultClearOne(w http.ResponseWriter, r *http.Request) {
	op := r.PathValue("op")
	s.mu.Lock()
	delete(s.faults, op)
	s.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

func (s *Service) handleFaultClearAll(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.faults = map[string]*fault{}
	s.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

func (s *Service) handleStatus(w http.ResponseWriter, r *http.Request) {
	s.data.mu.Lock()
	count := len(s.data.byID)
	s.data.mu.Unlock()
	s.mu.Lock()
	armed := map[string]string{}
	for op, f := range s.faults {
		armed[op] = fmt.Sprintf("%s(remaining=%d)", f.Kind, f.Remaining)
	}
	s.mu.Unlock()
	writeJSON(w, http.StatusOK, FaultStatus{Armed: armed, ResourceCount: count})
}

// ---- fault helpers ----

var errFaultFired = errors.New("fault fired")

func (s *Service) faultActive(op, kind string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	f, ok := s.faults[op]
	return ok && f.Kind == kind && f.Remaining > 0
}

// bumpFault decrements the armed fault and disarms it when exhausted. Callers
// must hold data.mu (fault bookkeeping is deliberately small and the two
// locks are always taken in order data -> faults).
func (s *Service) bumpFault(op string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if f, ok := s.faults[op]; ok {
		f.Remaining--
		if f.Remaining <= 0 && f.Kind != FaultStaleGet {
			delete(s.faults, op)
		} else if f.Remaining <= 0 {
			f.Remaining = 0
		}
	}
}

// consumeSlow applies an injected delay and returns errFaultFired when fired.
func (s *Service) consumeSlow(op string) error {
	s.mu.Lock()
	f, ok := s.faults[op]
	isSlow := ok && f.Kind == FaultSlow && f.Remaining > 0
	delay := 0
	if isSlow {
		delay = f.DelayMS
		f.Remaining--
		if f.Remaining <= 0 {
			delete(s.faults, op)
		}
	}
	s.mu.Unlock()
	if isSlow {
		if delay <= 0 {
			delay = 200
		}
		time.Sleep(time.Duration(delay) * time.Millisecond)
		return errFaultFired
	}
	return nil
}

func (s *Service) storeCloneLocked(r *Resource) *Resource { return s.data.clone(r) }

// Count returns the number of resources; useful for tests using the service
// in process.
func (s *Service) Count() int {
	s.data.mu.Lock()
	defer s.data.mu.Unlock()
	return len(s.data.byID)
}

// Reset removes all resources and faults (test helper).
func (s *Service) Reset(_ context.Context) {
	s.data.mu.Lock()
	s.data.byID = map[string]*Resource{}
	s.data.mu.Unlock()
	s.mu.Lock()
	s.faults = map[string]*fault{}
	s.mu.Unlock()
}
