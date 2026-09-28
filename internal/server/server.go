// Package server is the HTTP boundary. It validates requests, loads schemas,
// calls the store, maps the typed error contract to status codes and keeps
// every response tied to an X-Run-Id so a failing interaction can be replayed
// from the structured logs.
package server

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"fieldmerge/internal/adapter"
	"fieldmerge/internal/apperr"
	"fieldmerge/internal/log"
	"fieldmerge/internal/reconcile"
	"fieldmerge/internal/schema"
	"fieldmerge/internal/store"
)

type Server struct {
	st        *store.Store
	loop      *reconcile.Loop
	log       *logx.Logger
	maxBody   int64
	now       func() time.Time
	idCounter func() string
}

type Options struct {
	MaxBodyBytes int64
}

func New(st *store.Store, loop *reconcile.Loop, logger *logx.Logger, opts Options) *Server {
	mb := opts.MaxBodyBytes
	if mb <= 0 {
		mb = 2 << 20 // 2 MiB
	}
	return &Server{
		st: st, loop: loop, log: logger, maxBody: mb,
		now: func() time.Time { return time.Now().UTC() },
		idCounter: func() string {
			return runSeq()
		},
	}
}

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.handleHealth)
	mux.HandleFunc("/v1/schemas/", s.handleSchema)
	mux.HandleFunc("/v1/resources", s.handleList)
	mux.HandleFunc("/v1/", s.handleResource)
	return s.withRunID(s.recoverPanic(mux))
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

type schemaReq struct {
	Lists map[string]schema.ListDecl `json:"lists"`
}

func (s *Server) handleSchema(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/v1/schemas/"), "/")
	if len(parts) != 1 || parts[0] == "" {
		s.fail(w, r, apperr.New(apperr.InvalidInput, "bad_path", "use /v1/schemas/{kind}"))
		return
	}
	kind := parts[0]
	switch r.Method {
	case http.MethodPut:
		var req schemaReq
		if err := s.readBody(w, r, &req); err != nil {
			s.fail(w, r, err)
			return
		}
		if req.Lists == nil {
			req.Lists = map[string]schema.ListDecl{}
		}
		sc, err := schema.New(kind, req.Lists)
		if err != nil {
			s.fail(w, r, err)
			return
		}
		if err := s.st.PutSchema(ctx, sc); err != nil {
			s.fail(w, r, err)
			return
		}
		s.log.Info("schema_put", map[string]any{"kind": kind, "lists": req.Lists})
		writeJSON(w, http.StatusOK, map[string]any{"kind": kind, "lists": sc.Lists})
	case http.MethodGet:
		sc, err := s.st.GetSchema(ctx, kind)
		if err != nil {
			s.fail(w, r, err)
			return
		}
		writeJSON(w, http.StatusOK, sc)
	default:
		s.fail(w, r, apperr.New(apperr.InvalidInput, "method_not_allowed", "%s not allowed", r.Method))
	}
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		s.fail(w, r, apperr.New(apperr.InvalidInput, "method_not_allowed", "%s not allowed", r.Method))
		return
	}
	list, err := s.st.List(r.Context(), r.URL.Query().Get("kind"))
	if err != nil {
		s.fail(w, r, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"resources": list})
}

type applyReq struct {
	Manager string          `json:"manager"`
	Force   bool            `json:"force"`
	Config  json.RawMessage `json:"config"`
}

func (s *Server) handleResource(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	// /v1/{kind}/{name}[/apply|/ownership|/history]
	rest := strings.TrimPrefix(r.URL.Path, "/v1/")
	parts := strings.Split(rest, "/")
	if len(parts) < 2 || parts[0] == "" || parts[1] == "" {
		s.fail(w, r, apperr.New(apperr.InvalidInput, "bad_path",
			"use /v1/{kind}/{name}[/apply|/ownership|/history]"))
		return
	}
	kind, name := parts[0], parts[1]
	action := ""
	if len(parts) == 3 {
		action = parts[2]
	} else if len(parts) > 3 {
		s.fail(w, r, apperr.New(apperr.InvalidInput, "bad_path", "too many path segments"))
		return
	}

	switch {
	case action == "" && r.Method == http.MethodGet:
		res, err := s.st.Get(ctx, kind, name)
		if err != nil {
			s.fail(w, r, err)
			return
		}
		if res == nil {
			s.fail(w, r, apperr.New(apperr.NotFound, "resource_missing", "%s/%s not found", kind, name))
			return
		}
		writeJSON(w, http.StatusOK, res)
	case action == "apply" && r.Method == http.MethodPost:
		s.doApply(w, r, kind, name)
	case action == "ownership" && r.Method == http.MethodGet:
		res, err := s.st.Get(ctx, kind, name)
		if err != nil {
			s.fail(w, r, err)
			return
		}
		if res == nil {
			s.fail(w, r, apperr.New(apperr.NotFound, "resource_missing", "%s/%s not found", kind, name))
			return
		}
		claims, err := s.st.Ownership(ctx, kind, name)
		if err != nil {
			s.fail(w, r, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"kind": kind, "name": name, "ownership": claims})
	case action == "history" && r.Method == http.MethodGet:
		h, err := s.st.History(ctx, kind, name, 50)
		if err != nil {
			s.fail(w, r, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"kind": kind, "name": name, "history": h})
	default:
		s.fail(w, r, apperr.New(apperr.InvalidInput, "method_not_allowed",
			"%s %s not allowed", r.Method, r.URL.Path))
	}
}

func (s *Server) doApply(w http.ResponseWriter, r *http.Request, kind, name string) {
	ctx := r.Context()
	var req applyReq
	if err := s.readBody(w, r, &req); err != nil {
		s.fail(w, r, err)
		return
	}
	if req.Manager == "" {
		s.fail(w, r, apperr.New(apperr.InvalidInput, "manager_required", "manager must be non-empty"))
		return
	}
	if len(req.Config) == 0 {
		s.fail(w, r, apperr.New(apperr.InvalidInput, "config_required", "config is required"))
		return
	}
	sc, err := s.st.GetSchema(ctx, kind)
	if err != nil {
		s.fail(w, r, err)
		return
	}
	runID := runIDFromCtx(ctx)
	s.log.Info("apply_received", map[string]any{
		"kind": kind, "name": name, "manager": req.Manager,
		"force": req.Force, "config_bytes": len(req.Config),
	})

	out, applyErr := s.st.Apply(ctx, store.ApplyRequest{
		Kind: kind, Name: name, Manager: req.Manager, Force: req.Force,
		Config: req.Config, Schema: sc, RunID: runID,
	})
	if applyErr != nil {
		if res, ok := store.ConflictResult(applyErr); ok {
			s.log.Warn("apply_conflict", map[string]any{
				"kind": kind, "name": name, "manager": req.Manager,
				"conflicts": res.Conflict,
			})
			ae := apperr.New(apperr.Conflict, "ownership_conflict",
				"%d field(s) owned by other managers; resubmit with force to take over",
				len(res.Conflict))
			ae.Details = map[string]any{
				"conflicts": res.Conflict,
				"preview":   res.Live,
			}
			s.fail(w, r, ae)
			return
		}
		s.fail(w, r, applyErr)
		return
	}

	res, _ := s.st.Get(ctx, kind, name)
	applyStatus := "pending"
	if res != nil {
		applyStatus = res.Status
	}
	s.log.Info("apply_committed", map[string]any{
		"kind": kind, "name": name, "revision": out.Revision,
		"changes": out.Result.Changes, "pruned": out.Result.PrunedOwnership,
	})

	enqueued := true
	if s.loop != nil {
		if err := s.loop.Enqueue(kind, name); err != nil {
			enqueued = false
			s.log.Warn("reconcile_enqueue_backpressure", map[string]any{
				"kind": kind, "name": name, "err": err.Error(),
			})
		}
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"kind": kind, "name": name, "revision": out.Revision,
		"live":             out.Result.Live,
		"ownership":        out.Result.Claims,
		"changes":          out.Result.Changes,
		"pruned_stale":     out.Result.PrunedOwnership,
		"status":           applyStatus,
		"reconcile_queued": enqueued,
	})
}

// ---- plumbing ----

func (s *Server) readBody(w http.ResponseWriter, r *http.Request, dst any) error {
	if r.Body == nil {
		return apperr.New(apperr.InvalidInput, "empty_body", "request body is empty")
	}
	lr := io.LimitReader(r.Body, s.maxBody+1)
	data, err := io.ReadAll(lr)
	if err != nil {
		return apperr.New(apperr.InvalidInput, "body_read", "cannot read body: %v", err)
	}
	if int64(len(data)) > s.maxBody {
		return apperr.New(apperr.ResourceExhausted, "payload_too_large",
			"request body exceeds %d bytes", s.maxBody)
	}
	if err := json.Unmarshal(data, dst); err != nil {
		return apperr.New(apperr.InvalidInput, "bad_json", "request body is not valid JSON: %v", err)
	}
	return nil
}

func statusFor(cat apperr.Category) int {
	switch cat {
	case apperr.InvalidInput:
		return http.StatusBadRequest
	case apperr.NotFound:
		return http.StatusNotFound
	case apperr.Conflict:
		return http.StatusConflict
	case apperr.ResourceExhausted:
		return http.StatusRequestEntityTooLarge
	case apperr.Unavailable:
		return http.StatusServiceUnavailable
	default:
		return http.StatusInternalServerError
	}
}

func (s *Server) fail(w http.ResponseWriter, r *http.Request, err error) {
	ae, ok := apperr.As(err)
	if !ok {
		ae = apperr.New(apperr.Internal, "unexpected", "%s", err.Error())
	}
	status := statusFor(ae.Category)
	body := map[string]any{
		"error": map[string]any{
			"category": string(ae.Category),
			"code":     ae.Code,
			"message":  ae.Message,
		},
	}
	if ae.Details != nil {
		body["error"].(map[string]any)["details"] = ae.Details
	}
	runID := runIDFromCtx(r.Context())
	s.log.Log(mapLevel(ae.Category), "request_failed", map[string]any{
		"method": r.Method, "path": r.URL.Path, "status": status,
		"category": string(ae.Category), "code": ae.Code, "message": ae.Message,
	})
	writeJSONWith(w, status, body, runID)
}

func mapLevel(c apperr.Category) string {
	switch c {
	case apperr.Conflict:
		return "warn"
	case apperr.Internal, apperr.ComputeFailure, apperr.Unavailable:
		return "error"
	default:
		return "info"
	}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	writeJSONWith(w, status, v, "")
}

func writeJSONWith(w http.ResponseWriter, status int, v any, runID string) {
	w.Header().Set("Content-Type", "application/json")
	if runID != "" {
		w.Header().Set("X-Run-Id", runID)
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

var _ = context.Background
var _ adapter.Adapter
var _ = errors.New
