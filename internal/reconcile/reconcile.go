// Package reconcile 实现离线协调循环：从存储读取（快照, 策略集合），
// 校验后构造不可变判定引擎，并以原子方式切换“当前生效视图”。
//
// 失败语义（fail-closed for correctness, fail-sticky for availability）：
//   - 数据缺失：返回 ErrNotReady，不替换当前视图（首个版本未装载时无视图）；
//   - 校验失败：返回具体错误，不替换当前视图，避免坏数据影响判定；
//   - 版本不一致【不是】装载错误：引擎合法存在，但每个判定返回 UNKNOWN，
//     因为标签快照与策略版本必须一致才有意义；
//   - 新视图一旦构造成功即原子替换；并发判定始终看到某一个完整版本。
package reconcile

import (
	"context"
	"errors"
	"fmt"
	"sync/atomic"

	"netpolreach/internal/engine"
	"netpolreach/internal/model"
	"netpolreach/internal/policy"
	"netpolreach/internal/store"
)

var (
	// ErrNotReady 表示快照或策略集合尚未装载，当前无法判定。
	ErrNotReady = errors.New("reconcile: 尚未形成可判定视图（缺少快照或策略集合）")
)

// Status 是最近一次协调的可观测状态。
type Status struct {
	Ready         bool   `json:"ready"`
	LabelVersion  string `json:"label_version"`
	PolicyVersion string `json:"policy_version"`
	VersionMatch  bool   `json:"version_match"`
	Endpoints     int    `json:"endpoints"`
	Policies      int    `json:"policies"`
	LastError     string `json:"last_error,omitempty"`
}

// View 是某次协调后的不可变只读视图。
type View struct {
	Engine    *engine.Engine
	Snapshot  model.Snapshot
	PolicySet model.PolicySet
}

// Reconciler 持有当前生效视图的原子指针。
type Reconciler struct {
	st  store.Store
	cur atomic.Pointer[View]

	// 最近错误与状态用单独原子承载，避免与 View 混在一起。
	lastErr atomic.Value // string
}

// New 创建协调器。
func New(st store.Store) *Reconciler {
	r := &Reconciler{st: st}
	r.lastErr.Store("")
	return r
}

// Reconcile 从存储重新装载并切换视图。
func (r *Reconciler) Reconcile(ctx context.Context) (*View, error) {
	snap, err := r.st.LoadSnapshot(ctx)
	if errors.Is(err, store.ErrNotFound) {
		r.recordError("缺少标签快照")
		return nil, fmt.Errorf("%w: 缺少标签快照", ErrNotReady)
	} else if err != nil {
		r.recordError(err.Error())
		return nil, fmt.Errorf("装载快照失败: %w", err)
	}

	ps, err := r.st.LoadPolicySet(ctx)
	if errors.Is(err, store.ErrNotFound) {
		r.recordError("缺少策略集合")
		return nil, fmt.Errorf("%w: 缺少策略集合", ErrNotReady)
	} else if err != nil {
		r.recordError(err.Error())
		return nil, fmt.Errorf("装载策略集合失败: %w", err)
	}

	// 校验失败绝不替换现有视图（保留上一个已知良好版本）。
	if err := policy.ValidateSnapshot(snap); err != nil {
		r.recordError("快照校验失败: " + err.Error())
		return nil, fmt.Errorf("快照校验失败: %w", err)
	}
	if err := policy.ValidatePolicySet(ps); err != nil {
		r.recordError("策略集合校验失败: " + err.Error())
		return nil, fmt.Errorf("策略集合校验失败: %w", err)
	}

	eng, err := engine.NewEngine(snap, ps)
	if err != nil {
		r.recordError(err.Error())
		return nil, err
	}
	v := &View{Engine: eng, Snapshot: snap, PolicySet: ps}
	r.cur.Store(v)
	r.lastErr.Store("")
	return v, nil
}

// View 返回当前生效视图；未就绪时返回 ErrNotReady。
func (r *Reconciler) Current() (*View, error) {
	v := r.cur.Load()
	if v == nil {
		return nil, ErrNotReady
	}
	return v, nil
}

// Status 返回观测状态。
func (r *Reconciler) Status() Status {
	v := r.cur.Load()
	st := Status{LastError: loadString(&r.lastErr)}
	if v != nil {
		st.Ready = true
		st.LabelVersion = v.Snapshot.LabelVersion
		st.PolicyVersion = v.PolicySet.Version
		st.VersionMatch = v.Snapshot.PolicyVersion == v.PolicySet.Version
		st.Endpoints = len(v.Snapshot.Endpoints)
		st.Policies = len(v.PolicySet.Policies)
	}
	return st
}

func (r *Reconciler) recordError(msg string) {
	r.lastErr.Store(msg)
}

func loadString(v *atomic.Value) string {
	if s, ok := v.Load().(string); ok {
		return s
	}
	return ""
}
