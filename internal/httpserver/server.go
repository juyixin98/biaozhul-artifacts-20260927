// Package httpserver 把服务与协调循环暴露为标准库 HTTP 接口。
//
// 错误语义：
//
//	400 请求体/字段非法；404 资源不存在；409 已有在途发布或名称冲突；
//	422 发布策略无法执行（返回当前失败类别）；500 内部错误。
//
// 所有响应都回带 X-Request-Id，日志与发布事件使用同一请求身份关联。
package httpserver

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"rollingdeploy/internal/controller"
	"rollingdeploy/internal/model"
	"rollingdeploy/internal/procman"
	"rollingdeploy/internal/service"
	"rollingdeploy/internal/store"
)

func randShort() string {
	var b [5]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}

// Server 装配存储、服务、控制器与模拟进程管理器。
type Server struct {
	St           *store.Store
	Svc          *service.Service
	Ctrl         *controller.Controller
	PM           *procman.Manager
	Log          *slog.Logger
	AutoRollback bool

	mu      sync.Mutex
	current string // 正在滴答的 rollout id（协调循环与手动触发互斥）
}

type ctxKey string

const reqIDKey ctxKey = "request_id"

// Router 构建路由。
func (s *Server) Router() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /api/apps", s.handleCreateApp)
	mux.HandleFunc("GET /api/apps", s.handleListApps)
	mux.HandleFunc("GET /api/apps/{name}", s.handleGetApp)
	mux.HandleFunc("POST /api/apps/{name}/deployments", s.handleDeploy)
	mux.HandleFunc("POST /api/apps/{name}/rollback", s.handleRollback)
	mux.HandleFunc("POST /api/apps/{name}/reconcile", s.handleReconcile)
	mux.HandleFunc("GET /api/apps/{name}/rollouts", s.handleListRollouts)
	mux.HandleFunc("GET /api/rollouts/{id}", s.handleGetRollout)
	mux.HandleFunc("GET /api/rollouts/{id}/events", s.handleRolloutEvents)
	mux.HandleFunc("POST /api/debug/procman/reset", s.handleProcReset)
	mux.HandleFunc("POST /api/debug/procman/wipe-host", s.handleProcWipe)
	mux.HandleFunc("GET /api/debug/procman/list", s.handleProcList)
	return s.requestID(s.recoverPanic(mux))
}

// RunBackground 以固定间隔驱动所有在途发布，直到 ctx 取消。
func (s *Server) RunBackground(ctx context.Context, interval time.Duration) {
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := s.ReconcileAll(ctx, ""); err != nil {
				s.Log.Error("background reconcile failed", "err", err)
			}
		}
	}
}

// ReconcileAll 推进所有在途发布各一个滴答；requestID 为空表示循环自身触发。
func (s *Server) ReconcileAll(ctx context.Context, requestID string) error {
	rs, err := s.St.ListInFlightRollouts(ctx)
	if err != nil {
		return err
	}
	for _, r := range rs {
		if _, err := s.driveTick(ctx, r.ID, requestID); err != nil && !errors.Is(err, controller.ErrConflict) {
			s.Log.Error("drive tick failed", "rollout", r.ID, "err", err)
		}
	}
	return nil
}

// driveTick 串行执行一个滴答，并在发布失败且开启自动回退时把回退作为新操作发起。
func (s *Server) driveTick(ctx context.Context, rolloutID, requestID string) (controller.TickResult, error) {
	s.mu.Lock()
	if s.current != "" {
		s.mu.Unlock()
		return controller.TickResult{}, fmt.Errorf("reconcile already running for %s", s.current)
	}
	s.current = rolloutID
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.current = ""
		s.mu.Unlock()
	}()
	if requestID == "" {
		requestID = "tick:" + rolloutID
	}
	res, err := s.Ctrl.Tick(ctx, rolloutID, requestID)
	if err != nil {
		return res, err
	}
	if res.Status == model.StatusFailed && s.AutoRollback {
		r, gerr := s.St.RolloutGet(ctx, rolloutID)
		if gerr == nil && r.PrevRevisionID != "" {
			app, aerr := s.St.AppGet(ctx, r.AppID)
			if aerr == nil {
				if _, rerr := s.Svc.Rollback(ctx, app.Name, "auto-rollback:"+rolloutID); rerr != nil {
					s.Log.Warn("auto rollback not started", "failed_rollout", rolloutID, "err", rerr)
				}
			}
		}
	}
	return res, nil
}

// ---------- 中间件 ----------

func (s *Server) requestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-Id")
		if id == "" {
			id = "req-" + randShort()
		}
		w.Header().Set("X-Request-Id", id)
		ctx := context.WithValue(r.Context(), reqIDKey, id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func (s *Server) recoverPanic(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				reqID, _ := r.Context().Value(reqIDKey).(string)
				s.Log.Error("panic recovered", "request_id", reqID, "path", r.URL.Path, "panic", rec)
				writeError(w, http.StatusInternalServerError, "internal_error",
					fmt.Sprintf("internal error (request %s)", reqID), reqID)
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func reqID(r *http.Request) string {
	id, _ := r.Context().Value(reqIDKey).(string)
	return id
}

// ---------- 响应辅助 ----------

type errBody struct {
	Error     string `json:"error"`
	Message   string `json:"message"`
	RequestID string `json:"request_id"`
	Category  string `json:"category,omitempty"`
}

func writeError(w http.ResponseWriter, code int, kind, msg, requestID string) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(errBody{Error: kind, Message: msg, RequestID: requestID})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func decode(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		writeError(w, http.StatusBadRequest, "bad_request", "invalid JSON: "+err.Error(), reqID(r))
		return false
	}
	return true
}

func mapSvcError(w http.ResponseWriter, r *http.Request, err error) {
	id := reqID(r)
	switch {
	case errors.Is(err, service.ErrBadRequest):
		writeError(w, http.StatusBadRequest, "bad_request", err.Error(), id)
	case errors.Is(err, service.ErrNotFound):
		writeError(w, http.StatusNotFound, "not_found", err.Error(), id)
	case errors.Is(err, service.ErrConflict):
		writeError(w, http.StatusConflict, "conflict", err.Error(), id)
	default:
		slog.Error("service error", "request_id", id, "err", err)
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), id)
	}
}

// ---------- 处理器 ----------

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

type policyIn struct {
	Replicas       int `json:"replicas"`
	MaxSurge       int `json:"max_surge"`
	MaxUnavailable int `json:"max_unavailable"`
	ReadyThreshold int `json:"ready_threshold"`
	FailureLimit   int `json:"failure_limit"`
	ProgressTicks  int `json:"progress_ticks"`
}

type createAppIn struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	policyIn
}

func (s *Server) handleCreateApp(w http.ResponseWriter, r *http.Request) {
	var in createAppIn
	if !decode(w, r, &in) {
		return
	}
	c, err := s.Svc.CreateApp(r.Context(), service.CreateAppRequest{
		Name: in.Name, Version: in.Version, Replicas: in.Replicas,
		Policy: service.Policy{
			Replicas: in.Replicas, MaxSurge: in.MaxSurge, MaxUnavailable: in.MaxUnavailable,
			ReadyThreshold: in.ReadyThreshold, FailureLimit: in.FailureLimit,
			ProgressTicks: in.ProgressTicks,
		},
	}, reqID(r))
	if err != nil {
		mapSvcError(w, r, err)
		return
	}
	s.Log.Info("app created", "request_id", reqID(r), "app", in.Name, "rollout", c.RolloutID)
	writeJSON(w, http.StatusAccepted, c)
}

type deployIn struct {
	Version string `json:"version"`
	policyIn
}

func (s *Server) handleDeploy(w http.ResponseWriter, r *http.Request) {
	var in deployIn
	if !decode(w, r, &in) {
		return
	}
	c, err := s.Svc.Deploy(r.Context(), r.PathValue("name"), in.Version, service.Policy{
		Replicas: in.Replicas, MaxSurge: in.MaxSurge, MaxUnavailable: in.MaxUnavailable,
		ReadyThreshold: in.ReadyThreshold, FailureLimit: in.FailureLimit,
		ProgressTicks: in.ProgressTicks,
	}, reqID(r))
	if err != nil {
		mapSvcError(w, r, err)
		return
	}
	s.Log.Info("deployment created", "request_id", reqID(r), "app", r.PathValue("name"),
		"version", in.Version, "rollout", c.RolloutID)
	writeJSON(w, http.StatusAccepted, c)
}

func (s *Server) handleRollback(w http.ResponseWriter, r *http.Request) {
	c, err := s.Svc.Rollback(r.Context(), r.PathValue("name"), reqID(r))
	if err != nil {
		mapSvcError(w, r, err)
		return
	}
	s.Log.Info("rollback created as new rollout", "request_id", reqID(r),
		"app", r.PathValue("name"), "rollout", c.RolloutID)
	writeJSON(w, http.StatusAccepted, c)
}

// handleReconcile 手动推进应用当前在途发布一个滴答（测试与复现用的确定性入口）。
func (s *Server) handleReconcile(w http.ResponseWriter, r *http.Request) {
	app, err := s.St.AppGetByName(r.Context(), r.PathValue("name"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "not_found", "app not found", reqID(r))
			return
		}
		mapSvcError(w, r, err)
		return
	}
	ro, err := s.St.ActiveRollout(r.Context(), app.ID)
	if err != nil {
		mapSvcError(w, r, err)
		return
	}
	if ro == nil {
		writeError(w, http.StatusConflict, "no_in_flight", "no rollout in flight for app", reqID(r))
		return
	}
	res, err := s.driveTick(r.Context(), ro.ID, reqID(r))
	if err != nil {
		if errors.Is(err, controller.ErrConflict) {
			writeError(w, http.StatusConflict, "conflict", err.Error(), reqID(r))
			return
		}
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// ---------- 查询视图 ----------

type instanceView struct {
	ID          string `json:"id"`
	RevisionID  string `json:"revision_id"`
	Version     string `json:"version"`
	Phase       string `json:"phase"`
	ReadyStreak int    `json:"ready_streak"`
	Available   bool   `json:"available"`
}

type rolloutView struct {
	ID              string `json:"id"`
	Op              string `json:"op"`
	RevisionID      string `json:"revision_id"`
	Version         string `json:"version"`
	PrevRevisionID  string `json:"prev_revision_id"`
	Replicas        int    `json:"replicas"`
	MaxSurge        int    `json:"max_surge"`
	MaxUnavailable  int    `json:"max_unavailable"`
	ReadyThreshold  int    `json:"ready_threshold"`
	Status          string `json:"status"`
	FailureCategory string `json:"failure_category,omitempty"`
	FailureReason   string `json:"failure_reason,omitempty"`
	Ticks           int    `json:"ticks"`
}

type appView struct {
	ID              string         `json:"id"`
	Name            string         `json:"name"`
	Replicas        int            `json:"replicas"`
	CurrentVersion  string         `json:"current_version"`
	CurrentRevision string         `json:"current_revision"`
	Status          string         `json:"status"`
	FailureCategory string         `json:"failure_category,omitempty"`
	FailureReason   string         `json:"failure_reason,omitempty"`
	Snapshot        model.Snapshot `json:"snapshot"`
	Instances       []instanceView `json:"instances"`
	ActiveRollout   *rolloutView   `json:"active_rollout,omitempty"`
	LastRollout     *rolloutView   `json:"last_rollout,omitempty"`
}

func (s *Server) buildAppView(ctx context.Context, app *model.App) (appView, error) {
	revs, err := s.St.ListRevisions(ctx, app.ID)
	if err != nil {
		return appView{}, err
	}
	revVer := map[string]string{}
	for _, rv := range revs {
		revVer[rv.ID] = rv.Version
	}
	insts, err := s.St.InstancesByApp(ctx, app.ID, true)
	if err != nil {
		return appView{}, err
	}
	ro, err := s.St.ActiveRollout(ctx, app.ID)
	if err != nil {
		return appView{}, err
	}
	v := appView{ID: app.ID, Name: app.Name, Replicas: app.Replicas, Instances: []instanceView{}}
	var targetRev string
	if ro != nil {
		targetRev = ro.RevisionID
		v.ActiveRollout = rolloutToView(ro, revVer)
		v.Status = ro.Status
		v.FailureCategory = ro.FailureCategory
		v.FailureReason = ro.FailureReason
		v.Snapshot = model.ComputeSnapshot(insts, targetRev, ro.Replicas, ro.MaxSurge,
			ro.MaxUnavailable, ro.Ticks)
	} else {
		// 无在途发布：以最近一次发布为准呈现终态。
		rs, _ := s.St.ListRollouts(ctx, app.ID)
		if len(rs) > 0 {
			last := rs[len(rs)-1]
			targetRev = last.RevisionID
			v.Status = last.Status
			v.FailureCategory = last.FailureCategory
			v.FailureReason = last.FailureReason
			v.CurrentRevision = last.RevisionID
			v.CurrentVersion = revVer[last.RevisionID]
			v.Snapshot = model.ComputeSnapshot(insts, targetRev, last.Replicas, last.MaxSurge,
				last.MaxUnavailable, last.Ticks)
		}
	}
	for _, in := range insts {
		iv := instanceView{
			ID: in.ID, RevisionID: in.RevisionID, Version: revVer[in.RevisionID],
			Phase: in.Phase, ReadyStreak: in.ReadyStreak, Available: in.Phase == model.PhaseReady,
		}
		v.Instances = append(v.Instances, iv)
	}
	// 当前版本 = 最近成功发布的版本（失败/进行中不改变“当前版本”）；
	// last_rollout 始终暴露最近一次发布（含失败终态），便于黑盒断言失败结果。
	if rs, _ := s.St.ListRollouts(ctx, app.ID); len(rs) > 0 {
		v.LastRollout = rolloutToView(rs[len(rs)-1], revVer)
		for i := len(rs) - 1; i >= 0; i-- {
			if rs[i].Status == model.StatusSucceeded {
				v.CurrentRevision = rs[i].RevisionID
				v.CurrentVersion = revVer[rs[i].RevisionID]
				break
			}
		}
	}
	return v, nil
}

func rolloutToView(r *model.Rollout, revVer map[string]string) *rolloutView {
	return &rolloutView{
		ID: r.ID, Op: r.Op, RevisionID: r.RevisionID, Version: revVer[r.RevisionID],
		PrevRevisionID: r.PrevRevisionID, Replicas: r.Replicas, MaxSurge: r.MaxSurge,
		MaxUnavailable: r.MaxUnavailable, ReadyThreshold: r.ReadyThreshold, Status: r.Status,
		FailureCategory: r.FailureCategory, FailureReason: r.FailureReason, Ticks: r.Ticks,
	}
}

func (s *Server) handleGetApp(w http.ResponseWriter, r *http.Request) {
	app, err := s.St.AppGetByName(r.Context(), r.PathValue("name"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "not_found", "app not found", reqID(r))
			return
		}
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	v, err := s.buildAppView(r.Context(), app)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	writeJSON(w, http.StatusOK, v)
}

func (s *Server) handleListApps(w http.ResponseWriter, r *http.Request) {
	// 演示规模小，直接读 SQLite 的 apps 表。
	rows, err := s.St.DB().QueryContext(r.Context(), `SELECT id FROM apps ORDER BY name`)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	defer rows.Close()
	out := []appView{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
			return
		}
		app, err := s.St.AppGet(r.Context(), id)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
			return
		}
		v, err := s.buildAppView(r.Context(), app)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
			return
		}
		out = append(out, v)
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) handleListRollouts(w http.ResponseWriter, r *http.Request) {
	app, err := s.St.AppGetByName(r.Context(), r.PathValue("name"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "not_found", "app not found", reqID(r))
			return
		}
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	rs, err := s.St.ListRollouts(r.Context(), app.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	revs, _ := s.St.ListRevisions(r.Context(), app.ID)
	revVer := map[string]string{}
	for _, rv := range revs {
		revVer[rv.ID] = rv.Version
	}
	out := make([]*rolloutView, 0, len(rs))
	for _, ro := range rs {
		out = append(out, rolloutToView(ro, revVer))
	}
	writeJSON(w, http.StatusOK, out)
}

func (s *Server) handleGetRollout(w http.ResponseWriter, r *http.Request) {
	ro, err := s.St.RolloutGet(r.Context(), r.PathValue("id"))
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			writeError(w, http.StatusNotFound, "not_found", "rollout not found", reqID(r))
			return
		}
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	revs, _ := s.St.ListRevisions(r.Context(), ro.AppID)
	revVer := map[string]string{}
	for _, rv := range revs {
		revVer[rv.ID] = rv.Version
	}
	writeJSON(w, http.StatusOK, rolloutToView(ro, revVer))
}

type eventView struct {
	ID         int64          `json:"id"`
	RequestID  string         `json:"request_id"`
	Kind       string         `json:"kind"`
	InstanceID string         `json:"instance_id,omitempty"`
	Note       string         `json:"note"`
	Snapshot   model.Snapshot `json:"snapshot"`
	At         time.Time      `json:"at"`
}

func (s *Server) handleRolloutEvents(w http.ResponseWriter, r *http.Request) {
	evs, err := s.St.ListEventsByRollout(r.Context(), r.PathValue("id"))
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	out := make([]eventView, 0, len(evs))
	for _, e := range evs {
		out = append(out, eventView{
			ID: e.ID, RequestID: e.RequestID, Kind: e.Kind, InstanceID: e.InstanceID,
			Note: e.Note, Snapshot: e.Snapshot, At: e.CreatedAt,
		})
	}
	writeJSON(w, http.StatusOK, out)
}

// ---------- 调试夹具控制（本地依赖，明确隔离用） ----------

func (s *Server) handleProcReset(w http.ResponseWriter, r *http.Request) {
	if err := s.PM.Reset(); err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	s.Log.Warn("synthetic process manager reset", "request_id", reqID(r))
	writeJSON(w, http.StatusOK, map[string]string{"status": "reset"})
}

func (s *Server) handleProcWipe(w http.ResponseWriter, r *http.Request) {
	if err := s.PM.WipeHost(); err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	s.Log.Warn("synthetic host wipe: all managed processes removed", "request_id", reqID(r))
	writeJSON(w, http.StatusOK, map[string]string{"status": "wiped"})
}

type procListView struct {
	ProcID     string `json:"proc_id"`
	AppVersion string `json:"app_version"`
	Behavior   string `json:"behavior"`
	Running    bool   `json:"running"`
	Checks     int    `json:"checks"`
}

// handleProcList 只读列出模拟器进程（演示与黑盒测试用）。
func (s *Server) handleProcList(w http.ResponseWriter, r *http.Request) {
	ps, err := s.PM.List()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "internal_error", err.Error(), reqID(r))
		return
	}
	out := make([]procListView, 0, len(ps))
	for _, p := range ps {
		out = append(out, procListView{
			ProcID: p.ProcID, AppVersion: p.AppVersion, Behavior: p.Behavior,
			Running: p.Running, Checks: p.Checks,
		})
	}
	writeJSON(w, http.StatusOK, out)
}
