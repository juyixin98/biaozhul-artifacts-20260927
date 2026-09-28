// Package service 编排资源写操作：创建应用、发起发布、回退。
//
// 每个写操作在单个数据库事务内完成，并携带调用方请求身份记录首个事件，
// 保证“关联请求身份、展示处理位置”的可解释性要求。
package service

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"

	"rollingdeploy/internal/ids"
	"rollingdeploy/internal/model"
	"rollingdeploy/internal/store"
)

// 业务错误（HTTP 层据此映射状态码与错误类别）。
var (
	ErrNotFound   = errors.New("resource not found")
	ErrConflict   = errors.New("rollout already in flight")
	ErrBadRequest = errors.New("invalid request")
)

// Policy 是一次发布的滚动参数；零值字段由 defaults 填充。
type Policy struct {
	Replicas       int `json:"replicas"`
	MaxSurge       int `json:"max_surge"`
	MaxUnavailable int `json:"max_unavailable"`
	ReadyThreshold int `json:"ready_threshold"`
	FailureLimit   int `json:"failure_limit"`
	ProgressTicks  int `json:"progress_ticks"`
}

// Defaults 是服务级默认参数（来自配置）。
type Defaults struct {
	MaxSurge       int
	MaxUnavailable int
	ReadyThreshold int
	FailureLimit   int
	ProgressTicks  int
}

// Service 编排资源变更。
type Service struct {
	St       *store.Store
	Defaults Defaults
}

var nameRe = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)
var versionRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`)

// CreateAppRequest 创建应用并发布首个版本。
type CreateAppRequest struct {
	Name     string
	Version  string
	Replicas int
	Policy   Policy
}

// Created 是写操作的返回摘要。
type Created struct {
	AppID      string `json:"app_id"`
	RevisionID string `json:"revision_id"`
	RolloutID  string `json:"rollout_id"`
	Op         string `json:"op"`
}

func (s *Service) fillPolicy(p Policy) (surge, unavail, thr, lim, ticks int) {
	surge = p.MaxSurge
	if surge == 0 {
		surge = s.Defaults.MaxSurge
	}
	unavail = p.MaxUnavailable
	if unavail == 0 {
		unavail = s.Defaults.MaxUnavailable
	}
	thr = p.ReadyThreshold
	if thr <= 0 {
		thr = s.Defaults.ReadyThreshold
	}
	lim = p.FailureLimit
	if lim <= 0 {
		lim = s.Defaults.FailureLimit
	}
	ticks = p.ProgressTicks
	if ticks <= 0 {
		ticks = s.Defaults.ProgressTicks
	}
	return surge, unavail, thr, lim, ticks
}

func validate(replicas, surge, unavail int) error {
	if replicas <= 0 {
		return fmt.Errorf("%w: replicas must be > 0", ErrBadRequest)
	}
	if surge < 0 || unavail < 0 {
		return fmt.Errorf("%w: max_surge/max_unavailable must be >= 0", ErrBadRequest)
	}
	if surge == 0 && unavail == 0 {
		// 允许配置（用于失败夹具），但更新非首个版本时必然死锁 —— 由控制器按
		// invalid_strategy 终态化并给出明确原因，这里不提前拒绝。
	}
	return nil
}

// CreateApp 创建应用与首个 revision/rollout（op=create）。
func (s *Service) CreateApp(ctx context.Context, req CreateAppRequest, requestID string) (*Created, error) {
	name := strings.TrimSpace(req.Name)
	version := strings.TrimSpace(req.Version)
	if !nameRe.MatchString(name) || !versionRe.MatchString(version) {
		return nil, fmt.Errorf("%w: name/version format invalid", ErrBadRequest)
	}
	replicas := req.Replicas
	if replicas <= 0 {
		replicas = 3
	}
	surge, unavail, thr, lim, ticks := s.fillPolicy(req.Policy)
	if err := validate(replicas, surge, unavail); err != nil {
		return nil, err
	}
	if existing, err := s.St.AppGetByName(ctx, name); err != nil && !errors.Is(err, store.ErrNotFound) {
		return nil, err
	} else if existing != nil {
		return nil, fmt.Errorf("%w: app %q already exists", ErrConflict, name)
	}

	app := &model.App{ID: ids.New("app"), Name: name, Replicas: replicas}
	rev := &model.Revision{ID: ids.New("rev"), AppID: app.ID, Version: version, Source: "deploy"}
	ro := &model.Rollout{
		ID: ids.New("rol"), AppID: app.ID, Op: model.OpCreate,
		RevisionID: rev.ID, Replicas: replicas, MaxSurge: surge, MaxUnavailable: unavail,
		ReadyThreshold: thr, FailureLimit: lim, ProgressTicks: ticks,
		Status: model.StatusPending,
	}
	err := s.St.WithTx(ctx, func(t store.Tx) error {
		if err := t.CreateApp(ctx, app); err != nil {
			return err
		}
		if err := t.CreateRevision(ctx, rev); err != nil {
			return err
		}
		if err := t.CreateRollout(ctx, ro); err != nil {
			return err
		}
		_, err := t.EventInsert(ctx, &model.Event{
			RequestID: requestID, RolloutID: ro.ID, AppID: app.ID, Kind: "rollout_created",
			RevisionID: rev.ID,
			Note:       fmt.Sprintf("create app with version %s replicas=%d surge=%d unavail=%d threshold=%d", version, replicas, surge, unavail, thr),
		})
		return err
	})
	if err != nil {
		return nil, err
	}
	return &Created{AppID: app.ID, RevisionID: rev.ID, RolloutID: ro.ID, Op: model.OpCreate}, nil
}

// Deploy 发起一次滚动更新。replicas 为 0 时沿用应用当前期望副本。
func (s *Service) Deploy(ctx context.Context, appName, version string, p Policy, requestID string) (*Created, error) {
	version = strings.TrimSpace(version)
	if !versionRe.MatchString(version) {
		return nil, fmt.Errorf("%w: version format invalid", ErrBadRequest)
	}
	app, err := s.St.AppGetByName(ctx, appName)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return nil, fmt.Errorf("%w: app %q", ErrNotFound, appName)
		}
		return nil, err
	}
	active, err := s.St.ActiveRollout(ctx, app.ID)
	if err != nil {
		return nil, err
	}
	if active != nil {
		return nil, fmt.Errorf("%w: rollout %s still %s", ErrConflict, active.ID, active.Status)
	}
	replicas := p.Replicas
	if replicas <= 0 {
		replicas = app.Replicas
	}
	surge, unavail, thr, lim, ticks := s.fillPolicy(p)
	if err := validate(replicas, surge, unavail); err != nil {
		return nil, err
	}
	// 上一版本：最近一次发布的目标版本（无论成败，历史都可追溯）。
	prevID := ""
	if rs, err := s.St.ListRollouts(ctx, app.ID); err != nil {
		return nil, err
	} else if n := len(rs); n > 0 {
		prevID = rs[n-1].RevisionID
	}
	rev := &model.Revision{ID: ids.New("rev"), AppID: app.ID, Version: version, Source: "deploy"}
	ro := &model.Rollout{
		ID: ids.New("rol"), AppID: app.ID, Op: model.OpRollout,
		RevisionID: rev.ID, PrevRevisionID: prevID,
		Replicas: replicas, MaxSurge: surge, MaxUnavailable: unavail,
		ReadyThreshold: thr, FailureLimit: lim, ProgressTicks: ticks,
		Status: model.StatusPending,
	}
	err = s.St.WithTx(ctx, func(t store.Tx) error {
		if err := t.CreateRevision(ctx, rev); err != nil {
			return err
		}
		if err := t.CreateRollout(ctx, ro); err != nil {
			return err
		}
		_, err := t.EventInsert(ctx, &model.Event{
			RequestID: requestID, RolloutID: ro.ID, AppID: app.ID, Kind: "rollout_created",
			RevisionID: rev.ID,
			Note: fmt.Sprintf("deploy version %s replicas=%d surge=%d unavail=%d threshold=%d",
				version, replicas, surge, unavail, thr),
		})
		return err
	})
	if err != nil {
		return nil, err
	}
	return &Created{AppID: app.ID, RevisionID: rev.ID, RolloutID: ro.ID, Op: model.OpRollout}, nil
}

// Rollback 以历史成功版本发起一次“新的发布操作”回退。
// 历史 revision/rollout 全部保留：回退生成新的 revision（指向同一版本镜像）与新 rollout。
func (s *Service) Rollback(ctx context.Context, appName string, requestID string) (*Created, error) {
	app, err := s.St.AppGetByName(ctx, appName)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			return nil, fmt.Errorf("%w: app %q", ErrNotFound, appName)
		}
		return nil, err
	}
	active, err := s.St.ActiveRollout(ctx, app.ID)
	if err != nil {
		return nil, err
	}
	if active != nil {
		return nil, fmt.Errorf("%w: rollout %s still %s", ErrConflict, active.ID, active.Status)
	}
	rs, err := s.St.ListRollouts(ctx, app.ID)
	if err != nil {
		return nil, err
	}
	if len(rs) == 0 {
		return nil, fmt.Errorf("%w: no rollout history", ErrNotFound)
	}
	latest := rs[len(rs)-1]
	target, err := s.St.LatestSucceededRolloutBefore(ctx, app.ID, latest.ID)
	if err != nil {
		return nil, err
	}
	if target == nil {
		return nil, fmt.Errorf("%w: no earlier successful revision to roll back to", ErrNotFound)
	}
	targetRev, err := s.St.RevisionGet(ctx, target.RevisionID)
	if err != nil {
		return nil, err
	}
	newRev := &model.Revision{
		ID: ids.New("rev"), AppID: app.ID, Version: targetRev.Version,
		Source: "rollback:" + latest.ID,
	}
	ro := &model.Rollout{
		ID: ids.New("rol"), AppID: app.ID, Op: model.OpRollback,
		RevisionID: newRev.ID, PrevRevisionID: latest.RevisionID,
		Replicas: latest.Replicas, MaxSurge: latest.MaxSurge, MaxUnavailable: latest.MaxUnavailable,
		ReadyThreshold: latest.ReadyThreshold, FailureLimit: latest.FailureLimit,
		ProgressTicks: latest.ProgressTicks, Status: model.StatusPending,
	}
	var adopted int
	err = s.St.WithTx(ctx, func(t store.Tx) error {
		if err := t.CreateRevision(ctx, newRev); err != nil {
			return err
		}
		if err := t.CreateRollout(ctx, ro); err != nil {
			return err
		}
		// 现存的同版本实例直接划归到新回退 revision（相同镜像无需重启）。
		n, err := t.InstanceAdoptByVersion(ctx, app.ID, targetRev.Version, newRev.ID, ro.ID)
		if err != nil {
			return err
		}
		adopted = n
		_, err = t.EventInsert(ctx, &model.Event{
			RequestID: requestID, RolloutID: ro.ID, AppID: app.ID, Kind: "rollout_created",
			RevisionID: newRev.ID,
			Note: fmt.Sprintf("rollback as NEW rollout to version %s (prior rollout %s); adopted %d existing instances",
				targetRev.Version, latest.ID, adopted),
		})
		return err
	})
	if err != nil {
		return nil, err
	}
	return &Created{AppID: app.ID, RevisionID: newRev.ID, RolloutID: ro.ID, Op: model.OpRollback}, nil
}
