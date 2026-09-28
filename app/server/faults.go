package server

import (
	"sync"

	"replicactl/core/controller"
)

// FaultyDecisionStore wraps a DecisionStore and can force a failure of the
// next/every AppendDecision call, so failure-category tests can assert the
// STORE_FAILED class through the whole HTTP stack.
type FaultyDecisionStore struct {
	Inner controller.DecisionStore
	mu    sync.Mutex
	fail  error
}

// SetFail installs the returned error; nil clears it.
func (f *FaultyDecisionStore) SetFail(err error) {
	f.mu.Lock()
	f.fail = err
	f.mu.Unlock()
}

// AppendDecision implements controller.DecisionStore.
func (f *FaultyDecisionStore) AppendDecision(d controller.Decision) (controller.Decision, error) {
	f.mu.Lock()
	err := f.fail
	f.mu.Unlock()
	if err != nil {
		return d, err
	}
	return f.Inner.AppendDecision(d)
}

// FaultyHistory wraps a RawHistory with independent read/write failures.
type FaultyHistory struct {
	Inner      controller.RawHistory
	mu         sync.Mutex
	failRead   error
	failAppend error
}

// SetFail installs the returned error on both hooks; nil clears both.
func (f *FaultyHistory) SetFail(err error) {
	f.mu.Lock()
	f.failRead = err
	f.failAppend = err
	f.mu.Unlock()
}

// SetReadFail / SetAppendFail target one hook.
func (f *FaultyHistory) SetReadFail(err error) {
	f.mu.Lock()
	f.failRead = err
	f.mu.Unlock()
}

func (f *FaultyHistory) SetAppendFail(err error) {
	f.mu.Lock()
	f.failAppend = err
	f.mu.Unlock()
}

// AppendPoint implements controller.RawHistory.
func (f *FaultyHistory) AppendPoint(p controller.RawPoint) error {
	f.mu.Lock()
	err := f.failAppend
	f.mu.Unlock()
	if err != nil {
		return err
	}
	return f.Inner.AppendPoint(p)
}

// RawPointsSince implements controller.RawHistory.
func (f *FaultyHistory) RawPointsSince(since, now int64) ([]controller.RawPoint, error) {
	f.mu.Lock()
	err := f.failRead
	f.mu.Unlock()
	if err != nil {
		return nil, err
	}
	return f.Inner.RawPointsSince(since, now)
}
