// Package adapter is the stdlib-HTTP edge: JSON in/out, category-aware status
// mapping, and the read endpoints used for polling and audit replay.
package adapter

import (
	"encoding/json"
	"errors"
	"net/http"

	"admission/internal/coordinator"
	"admission/internal/model"
	"admission/internal/storage"
)

// Handler wires HTTP routes to a coordinator and store.
type Handler struct {
	coord *coordinator.Coordinator
	store *storage.Store
	mux   *http.ServeMux
}

// NewHandler builds the router.
func NewHandler(coord *coordinator.Coordinator, store *storage.Store) *Handler {
	h := &Handler{coord: coord, store: store, mux: http.NewServeMux()}
	h.routes()
	return h
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) { h.mux.ServeHTTP(w, r) }

func (h *Handler) routes() {
	h.mux.HandleFunc("/v1/requests", h.postRequest)
	h.mux.HandleFunc("/v1/requests/", h.subRequest)
	h.mux.HandleFunc("/v1/resources", h.listResources)
	h.mux.HandleFunc("/v1/resources/", h.getResource)
	h.mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
}

func (h *Handler) postRequest(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, model.ReasonInvalidInput, "use POST")
		return
	}
	var req model.Request
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, model.ReasonInvalidInput, "decode request: "+err.Error())
		return
	}
	if err := req.Validate(); err != nil {
		writeError(w, http.StatusBadRequest, model.ReasonInvalidInput, err.Error())
		return
	}
	res, err := h.coord.Admit(r.Context(), req)
	if err != nil {
		writeAdapterError(w, err)
		return
	}
	// An allowance is 200. A policy denial is a completed admission decision,
	// but its status reflects the failure category (422 validation, 429
	// exhaustion, 409 state conflict) so callers do not have to parse the body
	// to distinguish failure classes.
	status := http.StatusOK
	if res.Response.Decision == model.DecisionDenied {
		status = res.Response.Reason.HTTPStatus()
	}
	writeJSON(w, status, res.Response)
}

// subRequest dispatches /v1/requests/{uid}[/audits].
func (h *Handler) subRequest(w http.ResponseWriter, r *http.Request) {
	uid, suffix := splitPath(r.URL.Path)
	if uid == "" {
		writeError(w, http.StatusBadRequest, model.ReasonInvalidInput, "uid is required")
		return
	}
	switch {
	case r.Method == http.MethodGet && suffix == "":
		row, err := h.store.GetRequest(r.Context(), uid)
		if err != nil {
			writeAdapterError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"uid": row.UID, "status": row.Status, "operation": row.Operation,
			"reason": row.Reason, "message": row.Message,
			"finalDigest": row.FinalDigest, "attempts": row.Attempts,
		})
	case r.Method == http.MethodGet && suffix == "audits":
		records, err := h.store.LoadAudits(r.Context(), uid)
		if err != nil {
			writeAdapterError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"uid": uid, "audits": records})
	default:
		writeError(w, http.StatusMethodNotAllowed, model.ReasonInvalidInput, "unsupported route")
	}
}

func (h *Handler) listResources(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, model.ReasonInvalidInput, "use GET")
		return
	}
	rows, err := h.store.ListResources(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, model.ReasonComputeFailure, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"resources": rows})
}

func (h *Handler) getResource(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, model.ReasonInvalidInput, "use GET")
		return
	}
	rest := r.URL.Path[len("/v1/resources/"):]
	// /v1/resources/{kind}/{namespace}/{name}
	parts := splitN(rest, "/", 3)
	if len(parts) != 3 || parts[0] == "" || parts[2] == "" {
		writeError(w, http.StatusBadRequest, model.ReasonInvalidInput,
			"resource path must be /v1/resources/{kind}/{namespace}/{name}")
		return
	}
	row, err := h.store.GetResource(r.Context(), parts[0], parts[1], parts[2])
	if err != nil {
		writeAdapterError(w, err)
		return
	}
	var doc map[string]any
	_ = json.Unmarshal([]byte(row.Doc), &doc)
	writeJSON(w, http.StatusOK, map[string]any{"resource": doc})
}

func writeAdapterError(w http.ResponseWriter, err error) {
	var in *coordinator.InputError
	if errors.As(err, &in) {
		writeError(w, http.StatusBadRequest, model.ReasonInvalidInput, in.Error())
		return
	}
	var st *coordinator.StateError
	if errors.As(err, &st) {
		writeError(w, st.Reason.HTTPStatus(), st.Reason, st.Error())
		return
	}
	if errors.Is(err, storage.ErrNotFound) {
		writeError(w, http.StatusNotFound, model.ReasonStateConflict, err.Error())
		return
	}
	if errors.Is(err, storage.ErrConflict) {
		writeError(w, http.StatusConflict, model.ReasonStateConflict, err.Error())
		return
	}
	writeError(w, http.StatusInternalServerError, model.ReasonComputeFailure, err.Error())
}

// ErrorBody is the stable error contract: machine-readable reason + category.
type ErrorBody struct {
	Error    string       `json:"error"`
	Reason   model.Reason `json:"reason"`
	Category string       `json:"category"`
}

func writeError(w http.ResponseWriter, status int, reason model.Reason, msg string) {
	writeJSON(w, status, ErrorBody{Error: msg, Reason: reason, Category: reason.Category()})
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func splitPath(path string) (uid, suffix string) {
	rest := path[len("/v1/requests/"):]
	for i := 0; i < len(rest); i++ {
		if rest[i] == '/' {
			return rest[:i], rest[i+1:]
		}
	}
	return rest, ""
}

func splitN(s, sep string, n int) []string {
	var out []string
	for len(out) < n-1 {
		i := indexOfStr(s, sep)
		if i < 0 {
			break
		}
		out = append(out, s[:i])
		s = s[i+len(sep):]
	}
	out = append(out, s)
	return out
}

func indexOfStr(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
