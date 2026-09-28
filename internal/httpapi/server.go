// Package httpapi 是标准库 net/http 适配器：把离线判定核心暴露为 HTTP API。
//
// 端点：
//
//	GET  /healthz                       存活检查
//	GET  /v1/status                     当前协调视图状态（版本/端点数/策略数）
//	POST /v1/admin/ingest               装载离线数据包（快照+策略集合）并重新协调
//	POST /v1/probe                      单点判定
//	POST /v1/matrix                     批量/穷举连通矩阵
//	GET  /v1/decisions?limit=N          最近判定诊断记录（脱敏）
//
// 所有响应带 X-Request-ID；错误体为统一结构 {error:{code,message,request_id}}。
package httpapi

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"time"

	"netpolreach/internal/diag"
	"netpolreach/internal/engine"
	"netpolreach/internal/ingest"
	"netpolreach/internal/model"
	"netpolreach/internal/reconcile"
	"netpolreach/internal/store"
)

// 错误码（稳定字符串，故障测试直接断言）。
const (
	CodeBadRequest       = "BAD_REQUEST"
	CodeValidationFailed = "VALIDATION_FAILED"
	CodeNotReady         = "NOT_READY"
	CodeStoreUnavailable = "STORE_UNAVAILABLE"
	CodeStoreCorrupt     = "STORE_CORRUPT"
	CodeVersionConflict  = "VERSION_CONFLICT"
	CodeInternal         = "INTERNAL"
)

// Server 组装存储、协调器与诊断 logger。
type Server struct {
	Store store.Store
	Rec   *reconcile.Reconciler
	Log   *diag.Logger
}

type errorBody struct {
	Error struct {
		Code      string `json:"code"`
		Message   string `json:"message"`
		RequestID string `json:"request_id"`
	} `json:"error"`
}

func (s *Server) writeError(w http.ResponseWriter, r *http.Request, status int, code, msg string) {
	var b errorBody
	b.Error.Code = code
	b.Error.Message = msg
	b.Error.RequestID = requestIDFromCtx(r.Context())
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(b)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// Routes 构造带中间件的路由（标准库 ServeMux，Go 1.22+ 方法路由）。
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /v1/status", s.handleStatus)
	mux.HandleFunc("POST /v1/admin/ingest", s.handleIngest)
	mux.HandleFunc("POST /v1/probe", s.handleProbe)
	mux.HandleFunc("POST /v1/matrix", s.handleMatrix)
	mux.HandleFunc("GET /v1/decisions", s.handleListDecisions)
	return s.requestID(s.recoverPanic(mux))
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if err := s.Store.Ping(r.Context()); err != nil {
		s.writeStoreError(w, r, http.StatusServiceUnavailable, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	st := s.Rec.Status()
	resp := map[string]any{
		"ready":          st.Ready,
		"label_version":  st.LabelVersion,
		"policy_version": st.PolicyVersion,
		"version_match":  st.VersionMatch,
		"endpoints":      st.Endpoints,
		"policies":       st.Policies,
		"request_id":     requestIDFromCtx(r.Context()),
	}
	if st.LastError != "" {
		resp["last_error"] = st.LastError
	}
	if !st.Ready {
		writeJSON(w, http.StatusServiceUnavailable, resp)
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

type ingestResponse struct {
	Ready         bool   `json:"ready"`
	LabelVersion  string `json:"label_version"`
	PolicyVersion string `json:"policy_version"`
	VersionMatch  bool   `json:"version_match"`
	Endpoints     int    `json:"endpoints"`
	Policies      int    `json:"policies"`
	Warning       string `json:"warning,omitempty"`
	RequestID     string `json:"request_id"`
}

func (s *Server) handleIngest(w http.ResponseWriter, r *http.Request) {
	overwrite := strings.EqualFold(r.URL.Query().Get("overwrite"), "true")

	var b ingest.Bundle
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&b); err != nil {
		s.writeError(w, r, http.StatusBadRequest, CodeBadRequest, "请求体不是合法的数据包 JSON: "+err.Error())
		return
	}
	if err := ingest.ValidateBundle(b); err != nil {
		s.writeError(w, r, http.StatusUnprocessableEntity, CodeValidationFailed, err.Error())
		return
	}

	ctx := r.Context()
	if err := s.Store.SaveSnapshot(ctx, b.Snapshot, overwrite); err != nil {
		s.writeStoreError(w, r, http.StatusConflict, err)
		return
	}
	if err := s.Store.SavePolicySet(ctx, b.PolicySet, overwrite); err != nil {
		s.writeStoreError(w, r, http.StatusConflict, err)
		return
	}

	v, err := s.Rec.Reconcile(ctx)
	if err != nil {
		// 已校验的数据理论上不会到这里；按存储/内部错误处理，且不声称 ready。
		s.writeError(w, r, http.StatusInternalServerError, CodeInternal, "协调失败: "+err.Error())
		return
	}

	resp := ingestResponse{
		Ready:         true,
		LabelVersion:  v.Snapshot.LabelVersion,
		PolicyVersion: v.PolicySet.Version,
		VersionMatch:  v.Snapshot.PolicyVersion == v.PolicySet.Version,
		Endpoints:     len(v.Snapshot.Endpoints),
		Policies:      len(v.PolicySet.Policies),
		RequestID:     requestIDFromCtx(ctx),
	}
	if !resp.VersionMatch {
		resp.Warning = "snapshot.policy_version 与 policy_set.version 不一致；判定将返回 UNKNOWN(" +
			engine.ReasonVersionMismatch + ")"
	}
	s.Log.Reconcile(true, "ingest ok",
		"request_id", resp.RequestID,
		"label_version", resp.LabelVersion, "policy_version", resp.PolicyVersion,
		"version_match", resp.VersionMatch, "endpoints", resp.Endpoints, "policies", resp.Policies)
	writeJSON(w, http.StatusOK, resp)
}

type probeRequest struct {
	Probe model.Probe `json:"probe"`
}

type probeResponse struct {
	Decision  engine.Decision `json:"decision"`
	RequestID string          `json:"request_id"`
}

func (s *Server) handleProbe(w http.ResponseWriter, r *http.Request) {
	v, _ := s.readyView(w, r)
	if v == nil {
		return
	}
	var req probeRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		s.writeError(w, r, http.StatusBadRequest, CodeBadRequest, "请求体 JSON 非法: "+err.Error())
		return
	}
	if err := engine.ValidateProbe(req.Probe); err != nil {
		s.writeError(w, r, http.StatusBadRequest, CodeBadRequest, err.Error())
		return
	}
	d := v.Engine.Decide(req.Probe)
	s.recordDecision(r.Context(), r, d)
	writeJSON(w, http.StatusOK, probeResponse{Decision: d, RequestID: requestIDFromCtx(r.Context())})
}

type matrixRequest struct {
	Probes           []model.Probe `json:"probes,omitempty"`
	EnumerateNumeric bool          `json:"enumerate_numeric,omitempty"`
	IncludeSelf      bool          `json:"include_self,omitempty"`
}

type matrixResponse struct {
	LabelVersion  string           `json:"label_version"`
	PolicyVersion string           `json:"policy_version"`
	VersionMatch  bool             `json:"version_match"`
	Count         int              `json:"count"`
	Results       []engine.Decision `json:"results"`
	RequestID     string           `json:"request_id"`
}

func (s *Server) handleMatrix(w http.ResponseWriter, r *http.Request) {
	v, _ := s.readyView(w, r)
	if v == nil {
		return
	}
	var req matrixRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		s.writeError(w, r, http.StatusBadRequest, CodeBadRequest, "请求体 JSON 非法: "+err.Error())
		return
	}

	probes := append([]model.Probe{}, req.Probes...)
	if req.EnumerateNumeric {
		probes = append(probes, engine.EnumerateNumericProbes(v.Snapshot, req.IncludeSelf)...)
	}
	if len(probes) == 0 {
		s.writeError(w, r, http.StatusBadRequest, CodeBadRequest,
			"必须提供 probes 或置 enumerate_numeric=true")
		return
	}
	// 显式探测也逐个校验，错误请求不静默吞掉。
	for i, p := range probes {
		if err := engine.ValidateProbe(p); err != nil {
			s.writeError(w, r, http.StatusBadRequest, CodeBadRequest,
				"probes["+itoa(i)+"] 非法: "+err.Error())
			return
		}
	}

	results := v.Engine.Matrix(probes)
	// 诊断逐条记录（小集合）；批量时这是可接受的本地开销。
	for _, d := range results {
		s.recordDecision(r.Context(), r, d)
	}
	writeJSON(w, http.StatusOK, matrixResponse{
		LabelVersion:  v.Snapshot.LabelVersion,
		PolicyVersion: v.PolicySet.Version,
		VersionMatch:  v.Snapshot.PolicyVersion == v.PolicySet.Version,
		Count:         len(results),
		Results:       results,
		RequestID:     requestIDFromCtx(r.Context()),
	})
}

func (s *Server) handleListDecisions(w http.ResponseWriter, r *http.Request) {
	limit := 100
	if l := r.URL.Query().Get("limit"); l != "" {
		if n, err := parseInt(l); err == nil {
			limit = n
		}
	}
	recs, err := s.Store.ListDecisions(r.Context(), limit)
	if err != nil {
		s.writeStoreError(w, r, http.StatusInternalServerError, err)
		return
	}
	if recs == nil {
		recs = []store.DecisionRecord{}
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"decisions":  recs,
		"request_id": requestIDFromCtx(r.Context()),
	})
}

// readyView 取当前视图；未就绪时写出 503 并返回 nil。
func (s *Server) readyView(w http.ResponseWriter, r *http.Request) (*reconcile.View, error) {
	v, err := s.Rec.Current()
	if errors.Is(err, reconcile.ErrNotReady) {
		s.writeError(w, r, http.StatusServiceUnavailable, CodeNotReady,
			"尚未形成可判定视图：请先 POST /v1/admin/ingest 装载快照与策略集合")
		return nil, err
	}
	if err != nil {
		s.writeError(w, r, http.StatusInternalServerError, CodeInternal, err.Error())
		return nil, err
	}
	return v, nil
}

func (s *Server) writeStoreError(w http.ResponseWriter, r *http.Request, status int, err error) {
	switch {
	case errors.Is(err, store.ErrUnavailable):
		s.writeError(w, r, http.StatusServiceUnavailable, CodeStoreUnavailable, err.Error())
	case errors.Is(err, store.ErrCorrupt):
		s.writeError(w, r, http.StatusInternalServerError, CodeStoreCorrupt, err.Error())
	case errors.Is(err, store.ErrAlreadyExists):
		s.writeError(w, r, http.StatusConflict, CodeVersionConflict,
			"数据已存在；如需替换请加 ?overwrite=true")
	case errors.Is(err, store.ErrNotFound):
		s.writeError(w, r, http.StatusServiceUnavailable, CodeNotReady, err.Error())
	default:
		s.writeError(w, r, status, CodeInternal, err.Error())
	}
}

// recordDecision 脱敏落库并写结构化诊断；落库失败不影响判定响应。
func (s *Server) recordDecision(ctx context.Context, r *http.Request, d engine.Decision) {
	reqID := requestIDFromCtx(ctx)
	payload, err := diag.RedactDecisionJSON(d)
	if err != nil {
		payload = []byte(`{"redaction_error":true}`)
	}
	rec := store.DecisionRecord{
		RequestID:     reqID,
		CreatedAt:     time.Now().UTC(),
		LabelVersion:  d.LabelVersion,
		PolicyVersion: d.PolicyVersion,
		ProbeKey:      d.Probe.Key(),
		Verdict:       string(d.Verdict),
		Reason:        d.Reason,
		PayloadJSON:   string(payload),
	}
	if err := s.Store.InsertDecision(ctx, rec); err != nil {
		s.Log.Slog().Warn("decision record insert failed",
			"request_id", reqID, "error", err.Error())
	}
	s.Log.Decision(reqID, d)
}

func parseInt(s string) (int, error) {
	n := 0
	if s == "" {
		return 0, errors.New("empty")
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return 0, errors.New("not int")
		}
		n = n*10 + int(c-'0')
	}
	return n, nil
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}
