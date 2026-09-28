package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"time"

	"github.com/local/evictioncoordinator/internal/coordinator"
	"github.com/local/evictioncoordinator/internal/domain"
	"github.com/local/evictioncoordinator/internal/store"
)

// Deps are the server's dependencies.
type Deps struct {
	Store       *store.Store
	Coordinator *coordinator.Coordinator
	// ObservationEmitter is the local synthetic adapter hook used by the
	// observation/failure ingestion endpoints. It keeps transport independent
	// of fixture scripting: the server just records facts.
	EmitObservation func(ctx context.Context, inst domain.Instance, ready bool, epoch int64) error
	EmitFailure     func(ctx context.Context, instanceID, reason string, epoch int64) error
	Logger          *slog.Logger
}

// NewServer wires routes.
func NewServer(d Deps) *Server {
	if d.Logger == nil {
		d.Logger = slog.Default()
	}
	s := &Server{log: d.Logger}
	mux := http.NewServeMux()

	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

	mux.HandleFunc("POST /api/v1/groups", s.createGroup(d))
	mux.HandleFunc("GET /api/v1/groups/{namespace}/{name}", s.getGroup(d))
	mux.HandleFunc("POST /api/v1/groups/{namespace}/{name}/selectors:bump", s.bumpSelector(d))

	mux.HandleFunc("POST /api/v1/observations", s.recordObservation(d))
	mux.HandleFunc("POST /api/v1/failures", s.recordFailure(d))

	mux.HandleFunc("POST /api/v1/namespaces/{namespace}/groups/{group}/eviction", s.evict(d))

	mux.HandleFunc("GET /api/v1/approvals/{id}", s.getApproval(d))
	mux.HandleFunc("POST /api/v1/approvals/{id}/result", s.reportResult(d))
	mux.HandleFunc("POST /api/v1/approvals/{id}/reclaim", s.reclaim(d))
	mux.HandleFunc("GET /api/v1/reclaimable", s.listReclaimable(d))

	s.Handler = s.withRequestID(s.logging(mux))
	return s
}

type groupReq struct {
	Name      string            `json:"name"`
	Namespace string            `json:"namespace"`
	Replicas  int32             `json:"replicas"`
	Budget    budgetSpec        `json:"budget"`
	Selector  map[string]string `json:"selector"`
}

type budgetSpec struct {
	Mode    string `json:"mode"`
	Value   int32  `json:"value"`
	Percent int32  `json:"percent"`
}

func (s *Server) createGroup(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		var req groupReq
		if err := decodeJSON(r, &req); err != nil {
			errorBody(w, http.StatusBadRequest, reqID, "bad_request", err.Error())
			return
		}
		g := domain.Group{
			Name:           req.Name,
			Namespace:      req.Namespace,
			Replicas:       req.Replicas,
			BudgetMode:     domain.BudgetMode(req.Budget.Mode),
			BudgetValue:    req.Budget.Value,
			BudgetPercent:  req.Budget.Percent,
			SelectorLabels: req.Selector,
		}
		if err := g.Validate(); err != nil {
			errorBody(w, http.StatusBadRequest, reqID, "bad_request", err.Error())
			return
		}
		saved, err := d.Store.UpsertGroup(r.Context(), g, false)
		if err != nil {
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		writeJSON(w, http.StatusCreated, groupResp(saved, reqID))
	}
}

func groupResp(g domain.Group, reqID string) map[string]any {
	return map[string]any{
		"request_id":     reqID,
		"namespace":      g.Namespace,
		"name":           g.Name,
		"replicas":       g.Replicas,
		"selector_epoch": g.SelectorEpoch,
		"budget": map[string]any{
			"mode":    string(g.BudgetMode),
			"value":   g.BudgetValue,
			"percent": g.BudgetPercent,
		},
		// selector keys only in responses; values are workload data.
		"selector_keys": keysOf(g.SelectorLabels),
	}
}

func keysOf(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func (s *Server) getGroup(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		ns, name := r.PathValue("namespace"), r.PathValue("name")
		g, err := d.Store.GetGroup(r.Context(), ns, name)
		if errors.Is(err, store.ErrNotFound) {
			errorBody(w, http.StatusNotFound, reqID, string(domain.CatGroupNotFound), "group not found")
			return
		}
		if err != nil {
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, groupResp(g, reqID))
	}
}

func (s *Server) bumpSelector(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		ns, name := r.PathValue("namespace"), r.PathValue("name")
		g, oldEpoch, newEpoch, err := d.Coordinator.ChangeSelector(r.Context(), ns, name, "")
		if errors.Is(err, store.ErrNotFound) {
			errorBody(w, http.StatusNotFound, reqID, string(domain.CatGroupNotFound), "group not found")
			return
		}
		if err != nil {
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"request_id":         reqID,
			"group":              g.Key(),
			"old_selector_epoch": oldEpoch,
			"new_selector_epoch": newEpoch,
			"note":               "pending approvals under the old epoch are now 'stale' and remain charged until explicitly reclaimed",
		})
	}
}

type observationReq struct {
	Instance instanceRef `json:"instance"`
	Ready    bool        `json:"ready"`
	Epoch    int64       `json:"epoch"`
}

type instanceRef struct {
	ID        string            `json:"id"`
	Namespace string            `json:"namespace"`
	Group     string            `json:"group"`
	Labels    map[string]string `json:"labels"`
}

func (s *Server) recordObservation(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		var req observationReq
		if err := decodeJSON(r, &req); err != nil {
			errorBody(w, http.StatusBadRequest, reqID, "bad_request", err.Error())
			return
		}
		inst := domain.Instance{
			ID: req.Instance.ID, Namespace: req.Instance.Namespace,
			Group: req.Instance.Group, Labels: req.Instance.Labels,
		}
		if d.EmitObservation == nil {
			errorBody(w, http.StatusServiceUnavailable, reqID, "internal", "observation emitter not wired")
			return
		}
		if err := d.EmitObservation(r.Context(), inst, req.Ready, req.Epoch); err != nil {
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, map[string]any{
			"request_id": reqID, "instance_id": inst.ID, "ready": req.Ready, "epoch": req.Epoch,
		})
	}
}

type failureReq struct {
	InstanceID string `json:"instance_id"`
	Reason     string `json:"reason"`
	Epoch      int64  `json:"epoch"`
}

func (s *Server) recordFailure(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		var req failureReq
		if err := decodeJSON(r, &req); err != nil {
			errorBody(w, http.StatusBadRequest, reqID, "bad_request", err.Error())
			return
		}
		if d.EmitFailure == nil {
			errorBody(w, http.StatusServiceUnavailable, reqID, "internal", "failure emitter not wired")
			return
		}
		if err := d.EmitFailure(r.Context(), req.InstanceID, req.Reason, req.Epoch); err != nil {
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, map[string]any{
			"request_id": reqID, "instance_id": req.InstanceID,
			"category": "involuntary_failure_recorded", "epoch": req.Epoch,
		})
	}
}

type evictionReq struct {
	InstanceID  string `json:"instance_id"`
	ClientEpoch int64  `json:"client_selector_epoch,omitempty"`
	HasEpoch    bool   `json:"has_client_epoch,omitempty"`
}

func (s *Server) evict(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		ns, group := r.PathValue("namespace"), r.PathValue("group")
		var req evictionReq
		if err := decodeJSON(r, &req); err != nil {
			errorBody(w, http.StatusBadRequest, reqID, "bad_request", err.Error())
			return
		}
		if req.HasEpoch == false && req.ClientEpoch != 0 {
			req.HasEpoch = true
		}
		dec, err := d.Coordinator.Evict(r.Context(), coordinator.Request{
			Namespace:   ns,
			Group:       group,
			InstanceID:  req.InstanceID,
			RequestID:   reqID,
			ClientEpoch: req.ClientEpoch,
			HasEpoch:    req.HasEpoch,
		})
		if err != nil {
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		// Ensure response request_id matches the decision id.
		status := http.StatusOK
		if dec.Accepted {
			status = http.StatusAccepted
		} else {
			switch dec.Category {
			case domain.CatGroupNotFound:
				status = http.StatusNotFound
			case domain.CatUnknownReadiness:
				status = http.StatusConflict
			case domain.CatBudgetExhausted, domain.CatAlreadyEvicting,
				domain.CatInstanceFailed, domain.CatStaleEpoch, domain.CatInstanceNotMember:
				status = http.StatusUnprocessableEntity
			}
		}
		writeJSON(w, status, dec)
	}
}

func (s *Server) getApproval(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		a, err := d.Coordinator.GetApproval(r.Context(), r.PathValue("id"))
		if errors.Is(err, store.ErrNotFound) {
			errorBody(w, http.StatusNotFound, reqID, string(domain.CatApprovalNotFound), "approval not found")
			return
		}
		if err != nil {
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, approvalResp(a, reqID))
	}
}

func approvalResp(a domain.Approval, reqID string) map[string]any {
	resp := map[string]any{
		"request_id":  reqID,
		"approval_id": a.ID,
		"group":       a.Namespace + "/" + a.Group,
		"instance_id": a.InstanceID,
		"epoch":       a.Epoch,
		"state":       string(a.State),
		"reserved_at": a.ReservedAt.UTC().Format(time.RFC3339),
		"expires_at":  a.ExpiresAt.UTC().Format(time.RFC3339),
		"reclaimed":   a.ReclaimedAt != nil,
	}
	if a.ReclaimedAt != nil {
		resp["reclaimed_at"] = a.ReclaimedAt.UTC().Format(time.RFC3339)
	}
	return resp
}

type resultReq struct {
	Succeeded bool   `json:"succeeded"`
	Reason    string `json:"reason"`
}

func (s *Server) reportResult(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		var req resultReq
		if err := decodeJSON(r, &req); err != nil {
			errorBody(w, http.StatusBadRequest, reqID, "bad_request", err.Error())
			return
		}
		a, err := d.Coordinator.ReportResult(r.Context(), r.PathValue("id"), req.Succeeded, req.Reason)
		if errors.Is(err, coordinator.ErrLifecycle) {
			errorBody(w, http.StatusConflict, reqID, string(domain.CatApprovalState), "approval is not pending")
			return
		}
		if errors.Is(err, store.ErrNotFound) {
			errorBody(w, http.StatusNotFound, reqID, string(domain.CatApprovalNotFound), "approval not found")
			return
		}
		if err != nil {
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, approvalResp(a, reqID))
	}
}

type reclaimReq struct {
	Confirm bool `json:"confirm"`
}

func (s *Server) reclaim(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		id := r.PathValue("id")
		var req reclaimReq
		if err := decodeJSON(r, &req); err != nil {
			errorBody(w, http.StatusBadRequest, reqID, "bad_request", err.Error())
			return
		}
		if !req.Confirm {
			// We deliberately require an explicit confirm; return the exact
			// approval id the client must echo (here the id in the path *is*
			// the confirmation token).
			errorBody(w, http.StatusBadRequest, reqID, string(domain.CatReclaimConfirmation),
				`reclaim requires {"confirm":true} and approval id echo; no budget was released`)
			return
		}
		a, err := d.Coordinator.Reclaim(r.Context(), id, id)
		switch {
		case errors.Is(err, coordinator.ErrConfirmationRequired):
			errorBody(w, http.StatusBadRequest, reqID, string(domain.CatReclaimConfirmation), err.Error())
			return
		case errors.Is(err, coordinator.ErrLifecycle):
			errorBody(w, http.StatusConflict, reqID, string(domain.CatApprovalState), err.Error())
			return
		case errors.Is(err, store.ErrNotFound):
			errorBody(w, http.StatusNotFound, reqID, string(domain.CatApprovalNotFound), "approval not found")
			return
		case err != nil:
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		writeJSON(w, http.StatusOK, approvalResp(a, reqID))
	}
}

func (s *Server) listReclaimable(d Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		reqID := requestIDFrom(r.Context())
		ns := r.URL.Query().Get("namespace")
		group := r.URL.Query().Get("group")
		list, err := d.Coordinator.Reclaimable(r.Context(), ns, group)
		if err != nil {
			errorBody(w, http.StatusInternalServerError, reqID, "internal", err.Error())
			return
		}
		items := make([]map[string]any, 0, len(list))
		for _, a := range list {
			items = append(items, map[string]any{
				"approval_id": a.ID,
				"group":       a.Namespace + "/" + a.Group,
				"instance_id": a.InstanceID,
				"epoch":       a.Epoch,
				"state":       string(a.State),
			})
		}
		writeJSON(w, http.StatusOK, map[string]any{"request_id": reqID, "reclaimable": items})
	}
}

// keep encoding/json referenced even if future edits drop a use.
var _ = json.Marshal
