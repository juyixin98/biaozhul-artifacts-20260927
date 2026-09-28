// Package adapter contains the external-participant adapters. In this project
// the only participant is a simulated local process manager that provides
// synthetic fault fixtures (start rejection, exit-after-start, readiness
// jitter, capacity exhaustion).
package adapter

import (
	"context"
	"errors"
)

// Failure reasons returned by adapters. They map onto model.FailureCategory in
// the controller and are deliberately stable strings so tests can assert the
// exact failure class.
const (
	ReasonStartRejected = "start_rejected"
	ReasonExited        = "process_exited"
	ReasonCapacityFull  = "capacity_full"
)

// ErrCapacity is returned by Start when the process manager's capacity is full.
var ErrCapacity = errors.New("adapter: process manager capacity exhausted")

// ErrStartRejected is returned by Start when the fixture fails the start
// itself (as opposed to a process that starts and later exits).
var ErrStartRejected = errors.New("adapter: start rejected by fixture")

// ProcGone is returned by Observe/Terminate for an unknown process.
var ProcGone = errors.New("adapter: process not found")

// StartMeta carries everything the manager needs to launch one process.
type StartMeta struct {
	Workload  string
	Revision  string
	ReleaseID string
}

// ProcStatus is one observation of a managed process.
type ProcStatus struct {
	ProcID string
	// Running is false once the process has exited.
	Running bool
	// Ready is the current readiness observation. A ready observation is not
	// readiness: the controller requires ReadyThresholdTicks consecutive ready
	// observations before treating the instance as available.
	Ready bool
	// Reason explains a not-running or not-ready observation.
	Reason string
}

// ProcessManager is the minimal contract a participant must satisfy.
//
// Start launches a process; Observe reads its current state at the logical
// tick the manager has been told about; Terminate releases a process slot
// (used both for live scale-down and for reaping an exited process).
type ProcessManager interface {
	Start(ctx context.Context, meta StartMeta) (procID string, err error)
	Observe(ctx context.Context, procID string) (ProcStatus, error)
	Terminate(ctx context.Context, procID string) error
}

// TickDriven marks an adapter whose time advances through logical ticks. The
// controller calls SetTick before reconciling so observations are
// deterministic.
type TickDriven interface {
	SetTick(tick int64)
}
