// Package httpapi exposes the field-apply backend over HTTP using only the
// standard library. All errors share one JSON envelope and map to the error
// category contract.
package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"fieldapply/internal/coord"
	"fieldapply/internal/model"
	"fieldapply/internal/store"
)

// Server wires the store and coordinator to HTTP.
type Server struct {
	Store store.Store
	Coord *coord.Coordinator
	Mux   *http.ServeMux
}

func New(st store.Store, co *coord.Coordinator) *Server {
	s := &Server{Store: st, Coord: co, Mux: http.NewServeMux()}
	s.routes()
	return s
}

func (s *Server) routes() {
	s.Mux.HandleFunc("GET /healthz", s.health)
	s.Mux.HandleFunc("POST /v1/resources", s.create)
	s.Mux.HandleFunc("GET /v1/resources/{id}", s.get)
	s.Mux.HandleFunc("POST /v1/resources/{id}/apply", s.apply)
	s.Mux.HandleFunc("GET /v1/resources/{id}/history", s.history)
	s.Mux.HandleFunc("GET /v1/resources/{id}/owners", s.owners)
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type createReq struct {
	ID      string          `json:"id"`
	Body    json.RawMessage `json:"body"`
	Manager string          `json:"manager"`
	Schema  model.Schema    `json:"schema"`
}

func (s *Server) create(w http.ResponseWriter, r *http.Request) {
	var req createReq
	if err := decodeStrict(r, &req); err != nil {
		writeError(w, err)
		return
	}
	if strings.TrimSpace(req.ID) == "" {
		writeError(w, &model.Error{Category: model.CatInvalidInput, Code: "id_required",
			Message: "resource id must not be empty"})
		return
	}
	body := req.Body
	if len(body) == 0 {
		body = json.RawMessage(`{}`)
	}
	if !isJSONObject(body) {
		writeError(w, &model.Error{Category: model.CatInvalidInput, Code: "body_not_object",
			Message: "body must be a JSON object"})
		return
	}
	if req.Manager != "" {
		if err := validateBodyAgainstSchema(body, req.Schema); err != nil {
			writeError(w, err)
			return
		}
	}
	snap, err := s.Store.Create(r.Context(), req.ID, body, body, req.Manager, req.Schema)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, snapshotView(snap))
}

func (s *Server) get(w http.ResponseWriter, r *http.Request) {
	snap, err := s.Store.Snapshot(r.Context(), r.PathValue("id"))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, snapshotView(snap))
}

type applyReq struct {
	Manager string          `json:"manager"`
	Body    json.RawMessage `json:"body"`
	Force   bool            `json:"force"`
	BaseRev *int64          `json:"baseRevision"`
	Reason  string          `json:"reason"`
}

func (s *Server) apply(w http.ResponseWriter, r *http.Request) {
	var req applyReq
	if err := decodeStrict(r, &req); err != nil {
		writeError(w, err)
		return
	}
	if !isJSONObject(req.Body) {
		writeError(w, &model.Error{Category: model.CatInvalidInput, Code: "body_not_object",
			Message: "apply body must be a JSON object"})
		return
	}
	base := int64(-1)
	if req.BaseRev != nil {
		base = *req.BaseRev
	}
	out, err := s.Coord.Apply(r.Context(), coord.ApplyRequest{
		ResourceID: r.PathValue("id"),
		Manager:    req.Manager,
		Config:     req.Body,
		Force:      req.Force,
		BaseRev:    base,
		Reason:     req.Reason,
	})
	if err != nil {
		writeError(w, err)
		return
	}
	var live any
	_ = json.Unmarshal(out.Live, &live)
	resp := map[string]any{
		"runId":     out.RunID,
		"revision":  out.Revision,
		"live":      live,
		"changes":   out.Changes,
		"newOwners": out.NewOwners,
		"forced":    out.Forced,
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) history(w http.ResponseWriter, r *http.Request) {
	limit := 50
	entries, err := s.Store.History(r.Context(), r.PathValue("id"), limit)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"history": entries})
}

func (s *Server) owners(w http.ResponseWriter, r *http.Request) {
	snap, err := s.Store.Snapshot(r.Context(), r.PathValue("id"))
	if err != nil {
		writeError(w, err)
		return
	}
	type share struct {
		Path     string   `json:"path"`
		Managers []string `json:"managers"`
	}
	var out []share
	for path := range snap.Owners {
		out = append(out, share{Path: path, Managers: snap.Owners.Managers(path)})
	}
	// deterministic order
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j].Path < out[i].Path {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"owners": out})
}

// -----------------------------------------------------------------------------
// helpers
// -----------------------------------------------------------------------------

type resourceView struct {
	ID        string          `json:"id"`
	Revision  int64           `json:"revision"`
	Live      json.RawMessage `json:"live"`
	Schema    model.Schema    `json:"schema"`
	CreatedAt string          `json:"createdAt"`
	UpdatedAt string          `json:"updatedAt"`
}

func snapshotView(s *store.Snapshot) resourceView {
	return resourceView{
		ID: s.ID, Revision: s.Revision, Live: s.Live, Schema: s.Schema,
		CreatedAt: s.CreatedAt.Format("2006-01-02T15:04:05.999999999Z07:00"),
		UpdatedAt: s.UpdatedAt.Format("2006-01-02T15:04:05.999999999Z07:00"),
	}
}

func decodeStrict(r *http.Request, dst any) error {
	defer r.Body.Close()
	dec := json.NewDecoder(io.LimitReader(r.Body, 4<<20))
	if err := dec.Decode(dst); err != nil {
		return &model.Error{Category: model.CatInvalidInput, Code: "invalid_json",
			Message: "request body must be a single JSON value: " + err.Error()}
	}
	if dec.More() {
		return &model.Error{Category: model.CatInvalidInput, Code: "multiple_json_values",
			Message: "request body must contain exactly one JSON value"}
	}
	return nil
}

func isJSONObject(raw json.RawMessage) bool {
	t := strings.TrimSpace(string(raw))
	return strings.HasPrefix(t, "{")
}

func validateBodyAgainstSchema(body json.RawMessage, schema model.Schema) error {
	v, err := model.DecodeValue(body)
	if err != nil {
		return &model.Error{Category: model.CatInvalidInput, Code: "invalid_json", Message: err.Error()}
	}
	if err := schema.Validate(v); err != nil {
		return &model.Error{Category: model.CatInvalidInput, Code: "schema_invalid", Message: err.Error()}
	}
	return nil
}

// errBody is the single error envelope every endpoint returns.
type errBody struct {
	Error struct {
		Category  string           `json:"category"`
		Code      string           `json:"code"`
		Message   string           `json:"message"`
		Conflicts []model.Conflict `json:"conflicts,omitempty"`
	} `json:"error"`
}

func writeError(w http.ResponseWriter, err error) {
	var body errBody
	if se, ok := model.AsError(err); ok {
		body.Error.Category = string(se.Category)
		body.Error.Code = se.Code
		body.Error.Message = se.Message
		body.Error.Conflicts = se.Conflicts
	} else {
		body.Error.Category = string(model.CatComputationFailure)
		body.Error.Code = "unclassified"
		body.Error.Message = err.Error()
	}
	writeJSON(w, statusFor(err), body)
}

func statusFor(err error) int {
	if se, ok := model.AsError(err); ok {
		switch se.Category {
		case model.CatInvalidInput:
			return http.StatusBadRequest
		case model.CatStateConflict:
			return http.StatusConflict
		case model.CatNotFound:
			return http.StatusNotFound
		case model.CatResourceExhausted:
			return http.StatusServiceUnavailable
		case model.CatComputationFailure:
			return http.StatusInternalServerError
		}
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return http.StatusServiceUnavailable
	}
	return http.StatusInternalServerError
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
