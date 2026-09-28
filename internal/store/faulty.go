package store

import (
	"context"
	"sync"

	"netpolreach/internal/model"
)

// FaultKind 指定向底层 Store 注入的故障类别。
type FaultKind string

const (
	FaultNone          FaultKind = ""
	FaultUnavailable   FaultKind = "unavailable"
	FaultCorrupt       FaultKind = "corrupt"
	FaultVersionConflict FaultKind = "version_conflict"
)

// FaultyStore 包装真实 Store，按方法注入确定性故障，用于故障测试。
// 每个方法可独立开关，便于穷举“哪一侧的存储调用失败导致无法判定/5xx”。
type FaultyStore struct {
	Inner Store

	mu                   sync.Mutex
	faultOnSaveSnapshot  FaultKind
	faultOnLoadSnapshot  FaultKind
	faultOnSavePolicies  FaultKind
	faultOnLoadPolicies  FaultKind
	faultOnPing          FaultKind
	faultOnInsert        FaultKind
}

// SetFault 配置某类操作的注入故障。
func (f *FaultyStore) SetFault(op string, k FaultKind) {
	f.mu.Lock()
	defer f.mu.Unlock()
	switch op {
	case "save_snapshot":
		f.faultOnSaveSnapshot = k
	case "load_snapshot":
		f.faultOnLoadSnapshot = k
	case "save_policies":
		f.faultOnSavePolicies = k
	case "load_policies":
		f.faultOnLoadPolicies = k
	case "ping":
		f.faultOnPing = k
	case "insert_decision":
		f.faultOnInsert = k
	}
}

func (f *FaultyStore) errFor(k FaultKind) error {
	switch k {
	case FaultUnavailable:
		return ErrUnavailable
	case FaultCorrupt:
		return ErrCorrupt
	case FaultVersionConflict:
		return ErrVersionConflict
	default:
		return nil
	}
}

func (f *FaultyStore) SaveSnapshot(ctx context.Context, snap model.Snapshot, overwrite bool) error {
	f.mu.Lock()
	k := f.faultOnSaveSnapshot
	f.mu.Unlock()
	if e := f.errFor(k); e != nil {
		return e
	}
	return f.Inner.SaveSnapshot(ctx, snap, overwrite)
}

func (f *FaultyStore) LoadSnapshot(ctx context.Context) (model.Snapshot, error) {
	f.mu.Lock()
	k := f.faultOnLoadSnapshot
	f.mu.Unlock()
	if e := f.errFor(k); e != nil {
		return model.Snapshot{}, e
	}
	return f.Inner.LoadSnapshot(ctx)
}

func (f *FaultyStore) HasSnapshot(ctx context.Context) (bool, error) {
	return f.Inner.HasSnapshot(ctx)
}

func (f *FaultyStore) SavePolicySet(ctx context.Context, ps model.PolicySet, overwrite bool) error {
	f.mu.Lock()
	k := f.faultOnSavePolicies
	f.mu.Unlock()
	if e := f.errFor(k); e != nil {
		return e
	}
	return f.Inner.SavePolicySet(ctx, ps, overwrite)
}

func (f *FaultyStore) LoadPolicySet(ctx context.Context) (model.PolicySet, error) {
	f.mu.Lock()
	k := f.faultOnLoadPolicies
	f.mu.Unlock()
	if e := f.errFor(k); e != nil {
		return model.PolicySet{}, e
	}
	return f.Inner.LoadPolicySet(ctx)
}

func (f *FaultyStore) HasPolicySet(ctx context.Context) (bool, error) {
	return f.Inner.HasPolicySet(ctx)
}

func (f *FaultyStore) InsertDecision(ctx context.Context, rec DecisionRecord) error {
	f.mu.Lock()
	k := f.faultOnInsert
	f.mu.Unlock()
	if e := f.errFor(k); e != nil {
		return e
	}
	return f.Inner.InsertDecision(ctx, rec)
}

func (f *FaultyStore) ListDecisions(ctx context.Context, limit int) ([]DecisionRecord, error) {
	return f.Inner.ListDecisions(ctx, limit)
}

func (f *FaultyStore) Ping(ctx context.Context) error {
	f.mu.Lock()
	k := f.faultOnPing
	f.mu.Unlock()
	if e := f.errFor(k); e != nil {
		return e
	}
	return f.Inner.Ping(ctx)
}

func (f *FaultyStore) Close() error { return f.Inner.Close() }
