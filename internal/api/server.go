// Package api 暴露 RIB 的 HTTP 接口：管理（增删改查、批量替换）、
// 查询（最长前缀匹配 + 递归解析 + 匹配链）以及事件日志回放对照。
//
// 每个响应都带 request_id；每个写请求的事件与状态在单个 SQLite 事务内
// 落盘后才更新内存 RIB（或先改内存再落盘失败时回滚——实现选择后者，
// 见 Upsert/Delete/Replace 处理器注释）。
package api

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"

	"rib/internal/config"
	"rib/internal/diag"
	"rib/internal/netmodel"
	"rib/internal/replay"
	"rib/internal/rib"
	"rib/internal/store"
)

// Server 持有处理请求所需依赖。
type Server struct {
	RIB    *rib.RIB
	Store  *store.Store
	Cfg    config.Config
	Logger *slog.Logger
}

// envelope 是所有响应的统一外壳，便于客户端稳定解析。
type envelope struct {
	RequestID string    `json:"request_id"`
	Success   bool      `json:"success"`
	Version   int64     `json:"version,omitempty"`
	Data      any       `json:"data,omitempty"`
	Error     *apiError `json:"error,omitempty"`
}

type apiError struct {
	Code    string   `json:"code"`
	Message string   `json:"message"`
	Diag    []string `json:"diag,omitempty"`
}

// routeDTO 是写入接口的入参：前缀用字符串接收，便于返回规范化差异。
type routeDTO struct {
	ID            string `json:"id"`
	Prefix        string `json:"prefix"`
	AdminDistance int    `json:"admin_distance"`
	Metric        int    `json:"metric"`
	Protocol      string `json:"protocol"`
	Nexthop       nhDTO  `json:"nexthop"`
}

type nhDTO struct {
	Kind      string `json:"kind"`
	Address   string `json:"address,omitempty"`
	Interface string `json:"interface,omitempty"`
}

type replaceDTO struct {
	V4 []routeDTO `json:"v4"`
	V6 []routeDTO `json:"v6"`
}

// Routes 注册全部路由。
func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("GET /v1/routes", s.handleList)
	mux.HandleFunc("POST /v1/routes", s.handleUpsert)
	mux.HandleFunc("DELETE /v1/routes", s.handleDelete)
	mux.HandleFunc("POST /v1/routes/replace", s.handleReplace)
	mux.HandleFunc("GET /v1/lookup", s.handleLookup)
	mux.HandleFunc("GET /v1/events", s.handleEvents)
	mux.HandleFunc("POST /v1/replay", s.handleReplay)
	return s.withRequestID(mux)
}

// withRequestID 为每个请求建立诊断上下文并记录访问日志。
func (s *Server) withRequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rid := r.Header.Get("X-Request-ID")
		d := diag.NewContext(rid, s.Cfg.RedactDiag)
		no := diag.EventNo()
		ctx := diag.IntoContext(r.Context(), d)
		ctx = context.WithValue(ctx, serverKey{}, s)

		next.ServeHTTP(w, r.WithContext(ctx))

		s.Logger.Info("http",
			slog.Int64("event_no", no),
			slog.String("request_id", d.RequestID),
			slog.String("method", r.Method),
			slog.String("path", r.URL.Path),
			slog.String("remote", r.RemoteAddr),
		)
	})
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeOK(w, http.StatusOK, r, map[string]any{
		"status":  "ok",
		"version": s.RIB.Version(),
	})
}

func (s *Server) handleList(w http.ResponseWriter, r *http.Request) {
	fam := netmodel.AFIPv4
	if r.URL.Query().Get("family") == "ipv6" {
		fam = netmodel.AFIPv6
	}
	routes := s.RIB.Routes(fam)
	if routes == nil {
		routes = []netmodel.Route{}
	}
	writeOK(w, http.StatusOK, r, map[string]any{
		"family": fam.String(), "count": len(routes), "routes": routes,
	})
}

func (s *Server) handleUpsert(w http.ResponseWriter, r *http.Request) {
	dto, d, ok := decode[routeDTO](w, r)
	if !ok {
		return
	}
	rt, perr := toRoute(dto)
	if perr != nil {
		d.Note("reject upsert: %s", perr.Error())
		writeErr(w, http.StatusBadRequest, r, classify(perr), perr.Error(), d)
		return
	}
	if rt.Prefix.String() != dto.Prefix {
		d.Note("accept with normalization: prefix %s canonicalized to %s", dto.Prefix, rt.Prefix.String())
	}

	// 先在内存应用（含校验/选路结构更新），再落盘；落盘失败则删除刚写
	// 入的候选回滚。对夹具流量足够，且保持事件/状态一致。
	if err := s.RIB.Upsert(rt); err != nil {
		d.Note("reject upsert: %s", err.Error())
		writeErr(w, http.StatusBadRequest, r, classify(err), err.Error(), d)
		return
	}
	seq, err := s.Store.CommitUpsert(r.Context(), rt, s.RIB.Version(), d.RequestID)
	if err != nil {
		s.rollbackUpsert(rt)
		d.Note("reject upsert: persistence failed: %s", err.Error())
		writeErr(w, http.StatusServiceUnavailable, r, "persistence_failed", err.Error(), d)
		return
	}
	d.Note("accept upsert seq=%d version=%d", seq, s.RIB.Version())
	writeOK(w, http.StatusCreated, r, map[string]any{"route": rt, "event_seq": seq})
}

func (s *Server) rollbackUpsert(rt netmodel.Route) {
	// 尽力回滚：删除刚写入的候选；失败只记录，进程可由重放修复。
	if err := s.RIB.Delete(rt.Prefix, rt.ID); err != nil {
		s.Logger.Error("rollback upsert failed", slog.String("route", rt.ID), slog.Any("err", err))
	}
}

func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request) {
	prefix := r.URL.Query().Get("prefix")
	id := r.URL.Query().Get("id")
	d := diag.FromContext(r.Context())
	if prefix == "" || id == "" {
		writeErr(w, http.StatusBadRequest, r, "bad_request", "prefix and id query params are required", d)
		return
	}
	p, err := netmodel.ParsePrefix(prefix)
	if err != nil {
		d.Note("reject delete: invalid prefix %q", prefix)
		writeErr(w, http.StatusBadRequest, r, "invalid_prefix", err.Error(), d)
		return
	}
	removed, existed := popRoute(s.RIB, p, id)
	if !existed {
		writeErr(w, http.StatusNotFound, r, "not_found", rib.ErrNotFound.Error(), d)
		return
	}
	seq, err := s.Store.CommitDelete(r.Context(), int(p.Family()), p.String(), id, s.RIB.Version(), d.RequestID)
	if err != nil {
		_ = s.RIB.Upsert(removed) // 回滚
		d.Note("reject delete: persistence failed: %s", err.Error())
		writeErr(w, http.StatusServiceUnavailable, r, "persistence_failed", err.Error(), d)
		return
	}
	d.Note("accept delete seq=%d version=%d", seq, s.RIB.Version())
	writeOK(w, http.StatusOK, r, map[string]any{"deleted": removed, "event_seq": seq})
}

// popRoute 从 RIB 删除并取回被删路由（回滚用）。第二返回值表示是否存在。
func popRoute(r *rib.RIB, p netmodel.Prefix, id string) (netmodel.Route, bool) {
	var found netmodel.Route
	existed := false
	for _, rt := range r.Routes(p.Family()) {
		if rt.Prefix.String() == p.String() && rt.ID == id {
			found, existed = rt, true
			break
		}
	}
	if !existed {
		return found, false
	}
	return found, r.Delete(p, id) == nil
}

func (s *Server) handleReplace(w http.ResponseWriter, r *http.Request) {
	dto, d, ok := decode[replaceDTO](w, r)
	if !ok {
		return
	}
	req, err := toReplace(dto)
	if err != nil {
		d.Note("reject replace: %s", err.Error())
		writeErr(w, http.StatusBadRequest, r, classify(err), err.Error(), d)
		return
	}

	// 快照旧表用于落盘失败回滚。
	oldV4, oldV6 := s.RIB.Routes(netmodel.AFIPv4), s.RIB.Routes(netmodel.AFIPv6)
	if err := s.RIB.ReplaceAll(req); err != nil {
		d.Note("reject replace: %s", err.Error())
		writeErr(w, http.StatusBadRequest, r, classify(err), err.Error(), d)
		return
	}
	seq, err := s.Store.CommitReplace(r.Context(),
		store.ReplacePayload{V4: req.V4, V6: req.V6}, s.RIB.Version(), d.RequestID)
	if err != nil {
		_ = s.RIB.ReplaceAll(rib.ReplaceRequest{V4: oldV4, V6: oldV6})
		d.Note("reject replace: persistence failed: %s", err.Error())
		writeErr(w, http.StatusServiceUnavailable, r, "persistence_failed", err.Error(), d)
		return
	}
	d.Note("accept replace seq=%d version=%d v4=%d v6=%d",
		seq, s.RIB.Version(), len(req.V4), len(req.V6))
	writeOK(w, http.StatusOK, r, map[string]any{
		"event_seq": seq,
		"counts":    map[string]int{"v4": len(req.V4), "v6": len(req.V6)},
	})
}

func (s *Server) handleLookup(w http.ResponseWriter, r *http.Request) {
	target := r.URL.Query().Get("target")
	d := diag.FromContext(r.Context())
	if target == "" {
		writeErr(w, http.StatusBadRequest, r, "bad_request", "target query parameter is required", d)
		return
	}
	addr, err := parseAddr(target)
	if err != nil {
		d.Note("reject lookup: invalid target %q", target)
		writeErr(w, http.StatusBadRequest, r, "invalid_address", err.Error(), d)
		return
	}
	res := s.RIB.Lookup(addr)
	// 先记录请求级概要（脱敏），再把核心诊断与请求诊断合并进响应，
	// 保证这条“最终结论”也出现在返回体里。
	switch res.Status {
	case rib.StatusForwarded:
		d.Note("lookup %s -> forwarded via %s (depth=%d)", diag.RedactAddr(addr), res.Egress, res.Depth)
	case rib.StatusNoRoute:
		d.Note("lookup %s -> no_route", diag.RedactAddr(addr))
	default:
		d.Note("lookup %s -> %s (depth=%d)", diag.RedactAddr(addr), res.Status, res.Depth)
	}
	res.Diag = append(res.Diag, d.EntriesText()...)
	writeOK(w, http.StatusOK, r, res)
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	d := diag.FromContext(r.Context())
	since := int64(0)
	limit := s.Cfg.ReplayLimit
	events, err := s.Store.EventsSince(r.Context(), since, limit)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, r, "store_error", err.Error(), d)
		return
	}
	if events == nil {
		events = []store.Event{}
	}
	writeOK(w, http.StatusOK, r, map[string]any{"events": events})
}

func (s *Server) handleReplay(w http.ResponseWriter, r *http.Request) {
	d := diag.FromContext(r.Context())
	current := append(s.RIB.Routes(netmodel.AFIPv4), s.RIB.Routes(netmodel.AFIPv6)...)
	eng := replay.New(s.Store)
	rep, err := eng.Run(r.Context(), 0, s.Cfg.ReplayLimit, current, s.Cfg.MaxDepth)
	if err != nil {
		writeErr(w, http.StatusInternalServerError, r, "replay_failed", err.Error(), d)
		return
	}
	status := http.StatusOK
	if !rep.Consistent {
		status = http.StatusConflict
		d.Note("replay inconsistent: %d mismatch(es)", len(rep.Mismatches))
	} else {
		d.Note("replay consistent: %d event(s) replayed", rep.EventsPlayed)
	}
	writeOK(w, status, r, rep)
}

// ---- 编解码与映射 ----

func decode[T any](w http.ResponseWriter, r *http.Request) (T, *diag.Context, bool) {
	var zero T
	d := diag.FromContext(r.Context())
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&zero); err != nil {
		d.Note("reject: request body is not valid JSON for %T: %s", zero, err.Error())
		writeErr(w, http.StatusBadRequest, r, "bad_json", err.Error(), d)
		return zero, d, false
	}
	return zero, d, true
}

func toRoute(d routeDTO) (netmodel.Route, error) {
	p, err := netmodel.ParsePrefix(d.Prefix)
	if err != nil {
		return netmodel.Route{}, err
	}
	nh := netmodel.Nexthop{
		Kind:  netmodel.NHKind(strings.ToLower(d.Nexthop.Kind)),
		Iface: d.Nexthop.Interface,
	}
	if d.Nexthop.Address != "" {
		a, err := parseAddr(d.Nexthop.Address)
		if err != nil {
			return netmodel.Route{}, err
		}
		nh.Address = &a
	}
	rt := netmodel.Route{
		ID:            d.ID,
		Prefix:        p,
		Nexthop:       nh,
		AdminDistance: d.AdminDistance,
		Metric:        d.Metric,
		Protocol:      strings.ToLower(d.Protocol),
	}
	return rt, rt.Validate()
}

func toReplace(d replaceDTO) (rib.ReplaceRequest, error) {
	req := rib.ReplaceRequest{}
	convert := func(in []routeDTO) ([]netmodel.Route, error) {
		out := make([]netmodel.Route, 0, len(in))
		for _, item := range in {
			rt, err := toRoute(item)
			if err != nil {
				return nil, err
			}
			out = append(out, rt)
		}
		return out, nil
	}
	var err error
	if req.V4, err = convert(d.V4); err != nil {
		return req, err
	}
	if req.V6, err = convert(d.V6); err != nil {
		return req, err
	}
	return req, nil
}

func parseAddr(s string) (addr netipAddr, err error) {
	return parseNetipAddr(s)
}

// classify 把核心/模型错误映射为稳定错误码。
func classify(err error) string {
	switch {
	case errors.Is(err, netmodel.ErrInvalidPrefix):
		return "invalid_prefix"
	case errors.Is(err, netmodel.ErrEmptyID):
		return "empty_id"
	case errors.Is(err, netmodel.ErrBadNexthopKind):
		return "bad_nexthop_kind"
	case errors.Is(err, netmodel.ErrNHAddrRequired),
		errors.Is(err, netmodel.ErrNHIfaceRequired),
		errors.Is(err, netmodel.ErrNHAddrNotAllowed):
		return "bad_nexthop"
	case errors.Is(err, netmodel.ErrAFMismatch):
		return "address_family_mismatch"
	case errors.Is(err, netmodel.ErrBadDistance), errors.Is(err, netmodel.ErrBadMetric):
		return "bad_route_attribute"
	case errors.Is(err, rib.ErrNotFound):
		return "not_found"
	default:
		return "bad_request"
	}
}

func writeOK(w http.ResponseWriter, status int, r *http.Request, data any) {
	srv, _ := r.Context().Value(serverKey{}).(*Server)
	var version int64
	if srv != nil {
		version = srv.RIB.Version()
	}
	d := diag.FromContext(r.Context())
	writeJSON(w, status, envelope{
		RequestID: d.RequestID, Success: true,
		Version: version, Data: data,
	})
}

type serverKey struct{}

func writeErr(w http.ResponseWriter, status int, r *http.Request, code, msg string, d *diag.Context) {
	writeJSON(w, status, envelope{
		RequestID: d.RequestID, Success: false,
		Error: &apiError{Code: code, Message: msg, Diag: d.EntriesText()},
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
