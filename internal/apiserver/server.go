// Package apiserver exposes the desired-state plane over HTTP: resource CRUD
// with optimistic concurrency, a controller-only status/finalizer subresource,
// an events trigger and read-only diagnostics. Specs containing sensitive
// fields are served redacted unless the caller presents the shared
// controller credential (a localhost-only fixture, not real auth).
package apiserver

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"crcontroller/internal/logx"
	"crcontroller/internal/model"
	"crcontroller/internal/store"
)

// Server is the desired-state HTTP API.
type Server struct {
	store          *store.Store
	log            *logx.Logger
	controllerAuth string // shared secret; empty disables privileged view
	eventSink      func(uid string)
	apiFaults      *apiFaultManager
	mux            *http.ServeMux
}

// Option configures a Server.
type Option func(*Server)

// WithControllerAuth sets the shared secret granting raw spec visibility and
// status/finalizer writes.
func WithControllerAuth(secret string) Option {
	return func(s *Server) { s.controllerAuth = secret }
}

// WithEventSink registers a callback invoked whenever a resource mutates
// (spec/finalizers/deletion). The in-process controller uses it for immediate
// wakeups; it is best-effort and never blocks the HTTP handler.
func WithEventSink(fn func(uid string)) Option {
	return func(s *Server) { s.eventSink = fn }
}

// New constructs the API server.
func New(st *store.Store, logger *logx.Logger, opts ...Option) *Server {
	s := &Server{store: st, log: logger, apiFaults: newAPIFaultManager()}
	for _, o := range opts {
		o(s)
	}
	s.mux = http.NewServeMux()
	s.routes()
	return s
}

// Handler exposes the router wrapped in request-ID middleware.
func (s *Server) Handler() http.Handler { return requestIDMiddleware(s.mux) }

func (s *Server) routes() {
	s.mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	s.mux.HandleFunc("/api/v1/namespaces/", s.namespaced)
	s.mux.HandleFunc("/api/v1/resources", s.list)
	s.mux.HandleFunc("/api/v1/resources/", func(w http.ResponseWriter, r *http.Request) {
		uid := strings.TrimPrefix(r.URL.Path, "/api/v1/resources/")
		if uid == "" || strings.Contains(uid, "/") {
			writeJSON(w, http.StatusNotFound, errorBody("not found"))
			return
		}
		s.byUID(w, r, uid)
	})
	s.mux.HandleFunc("/api/v1/events", s.event)

	// Test-only desired-plane fault control.
	s.mux.HandleFunc("/admin/faults", s.adminFaults)
}

// namespaced handles /api/v1/namespaces/{ns}/resources[/{name}...].
func (s *Server) namespaced(w http.ResponseWriter, r *http.Request) {
	rest := strings.TrimPrefix(r.URL.Path, "/api/v1/namespaces/")
	parts := strings.Split(rest, "/")
	// Expected: {ns}/resources or {ns}/resources/{name}/...
	if len(parts) < 2 || parts[1] != "resources" {
		writeJSON(w, http.StatusNotFound, errorBody("not found"))
		return
	}
	namespace := parts[0]
	if len(parts) == 2 {
		// POST create (namespace derived from path)
		if r.Method != http.MethodPost {
			writeJSON(w, http.StatusMethodNotAllowed, errorBody("method not allowed"))
			return
		}
		s.create(w, r, namespace)
		return
	}
	name := parts[2]
	sub := ""
	if len(parts) >= 4 {
		sub = parts[3]
	}
	switch {
	case sub == "" && r.Method == http.MethodGet:
		s.getByName(w, r, namespace, name)
	case sub == "" && r.Method == http.MethodPut:
		s.putSpec(w, r, namespace, name)
	case sub == "" && r.Method == http.MethodDelete:
		s.del(w, r, namespace, name)
	case sub == "finalizers":
		s.finalizers(w, r, namespace, name)
	case sub == "status":
		s.status(w, r, namespace, name)
	default:
		writeJSON(w, http.StatusNotFound, errorBody("not found"))
	}
}

// --- resource handlers ---

type createPayload struct {
	Name        string            `json:"name"`
	Namespace   string            `json:"namespace"`
	Spec        map[string]any    `json:"spec"`
	Annotations map[string]string `json:"annotations"`
}

func (s *Server) create(w http.ResponseWriter, r *http.Request, ns string) {
	rid := requestID(r)
	var p createPayload
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		fail(w, http.StatusBadRequest, rid, "invalid JSON")
		return
	}
	p.Namespace = ns
	if p.Name == "" {
		fail(w, http.StatusBadRequest, rid, "name required")
		return
	}
	if p.Spec == nil {
		p.Spec = map[string]any{}
	}
	uid := newUID(ns, p.Name)
	obj, err := s.store.Create(r.Context(), store.CreateInput{
		UID: uid, Namespace: p.Namespace, Name: p.Name, Spec: p.Spec,
		Annotations: p.Annotations,
	})
	if errors.Is(err, store.ErrConflict) {
		fail(w, http.StatusConflict, rid, "resource already exists")
		return
	}
	if err != nil {
		s.log.Error("apiserver", "create failed", map[string]any{
			"requestID": rid, "error": err.Error(),
		})
		fail(w, http.StatusInternalServerError, rid, err.Error())
		return
	}
	s.emit(obj.UID)
	s.log.Info("apiserver", "resource created", map[string]any{
		"requestID": rid, "uid": obj.UID, "spec": obj.Spec,
	})
	writeJSON(w, http.StatusCreated, s.view(obj, privileged(r, s.controllerAuth)))
}

func (s *Server) getByName(w http.ResponseWriter, r *http.Request, ns, name string) {
	rid := requestID(r)
	obj, err := s.store.GetByName(r.Context(), ns, name)
	if errors.Is(err, store.ErrNotFound) {
		fail(w, http.StatusNotFound, rid, "not found")
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, rid, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, s.view(obj, privileged(r, s.controllerAuth)))
}

func (s *Server) list(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r)
	include := r.URL.Query().Get("includeTerminating") == "true"
	objs, err := s.store.List(r.Context(), include, queryInt(r, "limit", 500))
	if err != nil {
		fail(w, http.StatusInternalServerError, rid, err.Error())
		return
	}
	views := make([]resourceView, 0, len(objs))
	priv := privileged(r, s.controllerAuth)
	for _, o := range objs {
		views = append(views, s.view(o, priv))
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": views})
}

func (s *Server) byUID(w http.ResponseWriter, r *http.Request, uid string) {
	rid := requestID(r)
	obj, err := s.store.Get(r.Context(), uid)
	if errors.Is(err, store.ErrNotFound) {
		fail(w, http.StatusNotFound, rid, "not found")
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, rid, err.Error())
		return
	}
	switch r.Method {
	case http.MethodGet:
		writeJSON(w, http.StatusOK, s.view(obj, privileged(r, s.controllerAuth)))
	case http.MethodDelete:
		s.deleteUID(w, r, obj)
	default:
		fail(w, http.StatusMethodNotAllowed, rid, "method not allowed")
	}
}

type putPayload struct {
	Spec map[string]any `json:"spec"`
}

func (s *Server) putSpec(w http.ResponseWriter, r *http.Request, ns, name string) {
	rid := requestID(r)
	cur, err := s.store.GetByName(r.Context(), ns, name)
	if errors.Is(err, store.ErrNotFound) {
		fail(w, http.StatusNotFound, rid, "not found")
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, rid, err.Error())
		return
	}
	var p putPayload
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		fail(w, http.StatusBadRequest, rid, "invalid JSON")
		return
	}
	if p.Spec == nil {
		p.Spec = map[string]any{}
	}
	rv := expectedVersion(r, cur.ResourceVer)
	updated, err := s.store.UpdateSpec(r.Context(), store.UpdateSpecInput{
		UID: cur.UID, ResourceVersion: rv, Spec: p.Spec,
	})
	if err != nil {
		s.mapWriteError(w, r, rid, err)
		return
	}
	s.emit(updated.UID)
	s.log.Info("apiserver", "spec updated", map[string]any{
		"requestID": rid, "uid": updated.UID, "generation": updated.Generation,
		"resourceVersion": updated.ResourceVer, "spec": updated.Spec,
	})
	writeJSON(w, http.StatusOK, s.view(updated, privileged(r, s.controllerAuth)))
}

func (s *Server) del(w http.ResponseWriter, r *http.Request, ns, name string) {
	rid := requestID(r)
	cur, err := s.store.GetByName(r.Context(), ns, name)
	if errors.Is(err, store.ErrNotFound) {
		// Deletion is idempotent: if a record has been fully purged, 404 is
		// acceptable; while terminating it still returns the object.
		fail(w, http.StatusNotFound, rid, "not found")
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, rid, err.Error())
		return
	}
	s.deleteUID(w, r, cur)
}

func (s *Server) deleteUID(w http.ResponseWriter, r *http.Request, cur *model.Object) {
	rid := requestID(r)
	// Already terminating with no finalizers: complete the physical removal.
	if cur.Terminating() && len(cur.Finalizers) == 0 {
		if err := s.store.Purge(r.Context(), cur.UID); err != nil {
			fail(w, http.StatusInternalServerError, rid, err.Error())
			return
		}
		s.log.Info("apiserver", "record purged", map[string]any{
			"requestID": rid, "uid": cur.UID,
		})
		w.Header().Set("X-Request-ID", rid)
		w.WriteHeader(http.StatusNotFound)
		_ = json.NewEncoder(w).Encode(errorBody("purged"))
		return
	}
	if cur.Terminating() {
		// Still protected by finalizers: report the live object.
		writeJSON(w, http.StatusOK, s.view(cur, privileged(r, s.controllerAuth)))
		return
	}
	updated, err := s.store.Delete(r.Context(), cur.UID)
	if err != nil {
		fail(w, http.StatusInternalServerError, rid, err.Error())
		return
	}
	s.emit(updated.UID)
	s.log.Info("apiserver", "deletion requested", map[string]any{
		"requestID": rid, "uid": cur.UID, "finalizers": updated.Finalizers,
	})
	writeJSON(w, http.StatusOK, s.view(updated, privileged(r, s.controllerAuth)))
}

type finalizerPayload struct {
	Action          string `json:"action"` // "add" | "remove"
	Finalizer       string `json:"finalizer"`
	ResourceVersion int64  `json:"resourceVersion"`
}

func (s *Server) finalizers(w http.ResponseWriter, r *http.Request, ns, name string) {
	rid := requestID(r)
	if r.Method != http.MethodPatch && r.Method != http.MethodPost {
		fail(w, http.StatusMethodNotAllowed, rid, "use PATCH/POST")
		return
	}
	if !privileged(r, s.controllerAuth) {
		fail(w, http.StatusForbidden, rid, "controller credential required")
		return
	}
	cur, err := s.store.GetByName(r.Context(), ns, name)
	if errors.Is(err, store.ErrNotFound) {
		fail(w, http.StatusNotFound, rid, "not found")
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, rid, err.Error())
		return
	}
	var p finalizerPayload
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		fail(w, http.StatusBadRequest, rid, "invalid JSON")
		return
	}
	if p.Finalizer == "" {
		p.Finalizer = model.FinalizerController
	}
	if p.Finalizer != model.FinalizerController {
		fail(w, http.StatusBadRequest, rid, "unknown finalizer")
		return
	}
	fin := append([]string{}, cur.Finalizers...)
	switch p.Action {
	case "add":
		if !contains(fin, p.Finalizer) {
			fin = append(fin, p.Finalizer)
		}
	case "remove":
		fin = without(fin, p.Finalizer)
	default:
		fail(w, http.StatusBadRequest, rid, "action must be add|remove")
		return
	}
	rv := p.ResourceVersion
	if rv == 0 {
		rv = expectedVersion(r, cur.ResourceVer)
	}
	updated, err := s.store.SetFinalizers(r.Context(),
		store.SetFinalizersInput{UID: cur.UID, ResourceVersion: rv, Finalizers: fin})
	if err != nil {
		s.mapWriteError(w, r, rid, err)
		return
	}
	s.emit(updated.UID)
	s.log.Info("apiserver", "finalizer patched", map[string]any{
		"requestID": rid, "uid": cur.UID, "action": p.Action,
		"finalizers": updated.Finalizers, "resourceVersion": updated.ResourceVer,
	})
	writeJSON(w, http.StatusOK, s.view(updated, true))
}

type statusPayload struct {
	ResourceVersion    int64             `json:"resourceVersion"`
	ObservedGeneration int64             `json:"observedGeneration"`
	ExternalID         string            `json:"externalID"`
	State              string            `json:"state"`
	Conditions         []model.Condition `json:"conditions"`
}

func (s *Server) status(w http.ResponseWriter, r *http.Request, ns, name string) {
	rid := requestID(r)
	if r.Method != http.MethodPut && r.Method != http.MethodPost {
		fail(w, http.StatusMethodNotAllowed, rid, "use PUT/POST")
		return
	}
	if !privileged(r, s.controllerAuth) {
		fail(w, http.StatusForbidden, rid, "controller credential required")
		return
	}
	cur, err := s.store.GetByName(r.Context(), ns, name)
	if errors.Is(err, store.ErrNotFound) {
		fail(w, http.StatusNotFound, rid, "not found")
		return
	}
	if err != nil {
		fail(w, http.StatusInternalServerError, rid, err.Error())
		return
	}
	var p statusPayload
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		fail(w, http.StatusBadRequest, rid, "invalid JSON")
		return
	}
	rv := p.ResourceVersion
	if rv == 0 {
		rv = expectedVersion(r, cur.ResourceVer)
	}
	// Deterministic one-shot 409 for conflict-requeue tests.
	if s.apiFaults.take(cur.UID, FaultStatusConflictOnce) {
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": "conflict", "requestID": rid,
			"reason": "injected one-shot status conflict",
		})
		s.log.Warn("apiserver", "injected status conflict", map[string]any{
			"requestID": rid, "uid": cur.UID,
		})
		return
	}
	updated, err := s.store.UpdateStatus(r.Context(), store.StatusPatch{
		UID: cur.UID, ResourceVersion: rv,
		ObservedGen: p.ObservedGeneration, ExternalID: p.ExternalID,
		State: p.State, Conditions: p.Conditions,
	})
	if err != nil {
		s.mapWriteError(w, r, rid, err)
		return
	}
	s.log.Info("apiserver", "status updated", map[string]any{
		"requestID": rid, "uid": cur.UID,
		"observedGeneration": p.ObservedGeneration, "state": p.State,
		"resourceVersion": updated.ResourceVer,
	})
	writeJSON(w, http.StatusOK, s.view(updated, true))
}

// events lets any watcher (tests, curl) nudge the controller.
type eventPayload struct {
	UID       string `json:"uid"`
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	Type      string `json:"type"`
}

func (s *Server) event(w http.ResponseWriter, r *http.Request) {
	rid := requestID(r)
	if r.Method != http.MethodPost {
		fail(w, http.StatusMethodNotAllowed, rid, "use POST")
		return
	}
	var p eventPayload
	if err := json.NewDecoder(r.Body).Decode(&p); err != nil {
		fail(w, http.StatusBadRequest, rid, "invalid JSON")
		return
	}
	uid := p.UID
	if uid == "" && p.Namespace != "" && p.Name != "" {
		if obj, err := s.store.GetByName(r.Context(), p.Namespace, p.Name); err == nil {
			uid = obj.UID
		}
	}
	if uid == "" {
		fail(w, http.StatusBadRequest, rid, "uid or namespace+name required")
		return
	}
	s.emit(uid)
	s.log.Info("apiserver", "event received", map[string]any{
		"requestID": rid, "uid": uid, "type": p.Type,
	})
	writeJSON(w, http.StatusAccepted, map[string]string{"queued": uid})
}

// mapWriteError converts store errors into HTTP responses, preserving the
// distinction tests assert on: 409 conflicts vs 422 refused vs 409 stale.
func (s *Server) mapWriteError(w http.ResponseWriter, r *http.Request,
	rid string, err error) {
	switch {
	case errors.Is(err, store.ErrNotFound):
		fail(w, http.StatusNotFound, rid, "not found")
	case errors.Is(err, store.ErrConflict):
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": "conflict", "requestID": rid,
			"reason": "resourceVersion mismatch or stale observation",
		})
	case errors.Is(err, store.ErrRefused):
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{
			"error": "refused", "requestID": rid, "detail": err.Error(),
		})
	case errors.Is(err, store.ErrTerminating):
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": "conflict", "requestID": rid, "reason": "object is terminating",
		})
	default:
		fail(w, http.StatusInternalServerError, rid, err.Error())
	}
}

func (s *Server) emit(uid string) {
	if s.eventSink != nil {
		go s.eventSink(uid)
	}
}
