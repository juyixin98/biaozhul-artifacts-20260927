// Package api exposes desired-state management over HTTP. The API only
// mutates user intent (spec, generation, deletion timestamp); status is
// owned by the reconcile loop. All spec writes are compare-and-swap on the
// resource version supplied via If-Match, so a stale client gets 409 and
// must reload instead of clobbering newer intent.
package api

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"

	"resourcecontroller/internal/diag"
	"resourcecontroller/internal/model"
	"resourcecontroller/internal/reconcile"
	"resourcecontroller/internal/store"
)

// Server wires the store and enqueuer to HTTP handlers.
type Server struct {
	Store    *store.Store
	Enqueuer reconcile.Enqueuer
	Log      *diag.Logger
}

// Handler returns the routed API handler.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	mux.HandleFunc("GET /api/v1/widgets", s.list)
	mux.HandleFunc("POST /api/v1/widgets", s.create)
	mux.HandleFunc("GET /api/v1/widgets/{name}", s.get)
	mux.HandleFunc("PUT /api/v1/widgets/{name}", s.update)
	mux.HandleFunc("DELETE /api/v1/widgets/{name}", s.delete_)
	return s.Log.Middleware(mux)
}

// Wire types. Inbound spec carries the secret; outbound views never echo it.

type specInput struct {
	Replicas    *int   `json:"replicas"`
	Color       string `json:"color"`
	SecretToken string `json:"secretToken"`
}

type createInput struct {
	Metadata struct {
		Name string `json:"name"`
	} `json:"metadata"`
	Spec specInput `json:"spec"`
}

type updateInput struct {
	Spec specInput `json:"spec"`
}

type specView struct {
	Replicas       int    `json:"replicas"`
	Color          string `json:"color"`
	SecretRedacted string `json:"secretRedacted,omitempty"`
}

// widgetView is the externally served representation. The secret token is
// never returned; only a fixed redaction is shown when one is set.
type widgetView struct {
	Metadata model.ObjectMeta   `json:"metadata"`
	Spec     specView           `json:"spec"`
	Status   model.WidgetStatus `json:"status"`
}

func toView(w *model.Widget) widgetView {
	v := widgetView{Metadata: w.Meta, Status: w.Status}
	v.Spec.Replicas = w.Spec.Replicas
	v.Spec.Color = w.Spec.Color
	if w.Spec.SecretToken != "" {
		v.Spec.SecretRedacted = diag.Redact(w.Spec.SecretToken)
	}
	return v
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, code, msg string) {
	writeJSON(w, status, map[string]any{"error": struct {
		Code    string `json:"code"`
		Message string `json:"message"`
	}{Code: code, Message: msg}})
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func validateSpec(in specInput) (model.WidgetSpec, error) {
	if in.Replicas == nil {
		return model.WidgetSpec{}, errors.New("spec.replicas is required")
	}
	if *in.Replicas < 0 {
		return model.WidgetSpec{}, errors.New("spec.replicas must be non-negative")
	}
	if strings.TrimSpace(in.Color) == "" {
		return model.WidgetSpec{}, errors.New("spec.color is required")
	}
	return model.WidgetSpec{Replicas: *in.Replicas, Color: strings.TrimSpace(in.Color), SecretToken: in.SecretToken}, nil
}

func validName(name string) bool {
	if name == "" || len(name) > 63 {
		return false
	}
	for _, r := range name {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '-', r == '.':
		default:
			return false
		}
	}
	return true
}

func (s *Server) create(w http.ResponseWriter, r *http.Request) {
	var in createInput
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&in); err != nil {
		writeError(w, http.StatusBadRequest, "BadBody", "invalid JSON body")
		return
	}
	name := in.Metadata.Name
	if !validName(name) {
		writeError(w, http.StatusBadRequest, "InvalidName",
			"metadata.name must be 1-63 chars of [a-z0-9.-]")
		return
	}
	spec, err := validateSpec(in.Spec)
	if err != nil {
		writeError(w, http.StatusBadRequest, "ValidationFailed", err.Error())
		return
	}
	now := time.Now().UTC()
	wid := &model.Widget{
		Meta: model.ObjectMeta{
			Name:            name,
			UID:             "uid-" + strconv.FormatInt(now.UnixNano(), 36) + "-" + name,
			Generation:      1,
			ResourceVersion: 1,
			CreatedAt:       now,
			UpdatedAt:       now,
		},
		Spec: spec,
		Status: model.WidgetStatus{
			Phase: model.PhasePending,
		},
	}
	if err := s.Store.Create(r.Context(), wid); err != nil {
		if errors.Is(err, store.ErrAlreadyExists) {
			writeError(w, http.StatusConflict, "AlreadyExists", "resource "+name+" already exists")
			return
		}
		s.Log.Error(r.Context(), "create failed", "resource", name, "error", err.Error())
		writeError(w, http.StatusInternalServerError, "InternalError", "could not persist resource")
		return
	}
	s.Enqueuer.Enqueue(name)
	s.Log.Info(r.Context(), "resource desired created", "resource", name, "generation", wid.Meta.Generation,
		"secretToken", diag.Secret(spec.SecretToken))
	w.Header().Set("Location", "/api/v1/widgets/"+name)
	writeJSON(w, http.StatusCreated, toView(wid))
}

func (s *Server) get(w http.ResponseWriter, r *http.Request) {
	wid, err := s.Store.Get(r.Context(), r.PathValue("name"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "NotFound", "no such resource")
			return
		}
		writeError(w, http.StatusInternalServerError, "InternalError", "could not load resource")
		return
	}
	writeJSON(w, http.StatusOK, toView(wid))
}

func (s *Server) list(w http.ResponseWriter, r *http.Request) {
	items, err := s.Store.List(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "InternalError", "could not list resources")
		return
	}
	views := make([]widgetView, 0, len(items))
	for _, item := range items {
		views = append(views, toView(item))
	}
	writeJSON(w, http.StatusOK, map[string]any{"items": views})
}

func parseIfMatch(r *http.Request) (int64, bool) {
	h := strings.TrimSpace(r.Header.Get("If-Match"))
	if h == "" {
		return 0, false
	}
	v, err := strconv.ParseInt(h, 10, 64)
	if err != nil {
		return 0, false
	}
	return v, true
}

func (s *Server) update(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	rv, hasRV := parseIfMatch(r)
	if !hasRV {
		writeError(w, http.StatusBadRequest, "MissingVersion",
			"If-Match: <resourceVersion> is required for updates")
		return
	}
	var in updateInput
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&in); err != nil {
		writeError(w, http.StatusBadRequest, "BadBody", "invalid JSON body")
		return
	}
	newSpec, err := validateSpec(in.Spec)
	if err != nil {
		writeError(w, http.StatusBadRequest, "ValidationFailed", err.Error())
		return
	}

	cur, err := s.Store.Get(r.Context(), name)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "NotFound", "no such resource")
			return
		}
		writeError(w, http.StatusInternalServerError, "InternalError", "could not load resource")
		return
	}
	if cur.Meta.DeletionTimestamp != nil {
		writeError(w, http.StatusConflict, "Deleting",
			"resource is being deleted; spec updates are refused")
		return
	}
	// A PUT must carry the full spec; when the secret field is omitted we keep
	// the previously stored secret rather than wiping it silently.
	if in.Spec.SecretToken == "" {
		newSpec.SecretToken = cur.Spec.SecretToken
	}

	updated := *cur
	updated.Spec = newSpec
	updated.Meta.Generation++
	saved, err := s.Store.SaveSpec(r.Context(), &updated, rv)
	if err != nil {
		if errors.Is(err, store.ErrVersionConflict) {
			writeError(w, http.StatusConflict, "VersionConflict",
				"resource version "+strconv.FormatInt(rv, 10)+" is stale; reload and retry")
			return
		}
		s.Log.Error(r.Context(), "update failed", "resource", name, "error", err.Error())
		writeError(w, http.StatusInternalServerError, "InternalError", "could not persist resource")
		return
	}
	s.Enqueuer.Enqueue(name)
	s.Log.Info(r.Context(), "resource desired updated", "resource", name,
		"generation", saved.Meta.Generation, "oldGeneration", cur.Meta.Generation,
		"secretToken", diag.Secret(newSpec.SecretToken))
	writeJSON(w, http.StatusOK, toView(saved))
}

func (s *Server) delete_(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	cur, err := s.Store.Get(r.Context(), name)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			// Deletes are idempotent at the API edge.
			w.WriteHeader(http.StatusNoContent)
			return
		}
		writeError(w, http.StatusInternalServerError, "InternalError", "could not load resource")
		return
	}
	if rv, hasRV := parseIfMatch(r); hasRV && rv != cur.Meta.ResourceVersion {
		writeError(w, http.StatusConflict, "VersionConflict",
			"resource version "+strconv.FormatInt(rv, 10)+" is stale; reload and retry")
		return
	}
	if cur.Meta.DeletionTimestamp == nil {
		now := time.Now().UTC()
		cur.Meta.DeletionTimestamp = &now
		cur.Status.Phase = model.PhaseDeleting
		saved, err := s.Store.SaveSpec(r.Context(), cur, cur.Meta.ResourceVersion)
		if err != nil {
			if errors.Is(err, store.ErrVersionConflict) {
				writeError(w, http.StatusConflict, "VersionConflict", "resource changed concurrently; retry")
				return
			}
			writeError(w, http.StatusInternalServerError, "InternalError", "could not mark deletion")
			return
		}
		cur = saved
	}
	s.Enqueuer.Enqueue(name)
	s.Log.Info(r.Context(), "resource deletion requested", "resource", name,
		"finalizers", len(cur.Meta.Finalizers))
	writeJSON(w, http.StatusAccepted, toView(cur))
}
