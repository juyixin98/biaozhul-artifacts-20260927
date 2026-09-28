// Package model defines the resource model of the rolling-release controller:
// workloads, revisions, instances, releases and the auditable release-event log.
//
// These types are plain data carriers. All state-transition rules live in the
// controller package; persistence lives in the store package.
package model

import "time"

// InstanceState is the controller-observed lifecycle state of one instance.
type InstanceState string

const (
	// StateStarting means the instance process exists but has not yet been
	// continuously ready for the release's readiness threshold.
	StateStarting InstanceState = "starting"
	// StateReady means readiness has been observed for ReadyThresholdTicks
	// consecutive observations. A later not-ready observation moves it back
	// to StateStarting (readiness jitter).
	StateReady InstanceState = "ready"
	// StateFailed means the process manager rejected the start or the process
	// exited. Failed instances never count as live or available.
	StateFailed InstanceState = "failed"
	// StateTerminated is the terminal tombstone state after an explicit scale
	// down. Rows are kept for history but excluded from every live count.
	StateTerminated InstanceState = "terminated"
)

// ReleaseState is the lifecycle state of a release.
type ReleaseState string

const (
	RelPending   ReleaseState = "pending"   // created, reconcile loop has not picked it up yet
	RelActive    ReleaseState = "active"    // rollout in progress
	RelSucceeded ReleaseState = "succeeded" // all replicas on the target revision
	RelFailed    ReleaseState = "failed"    // rollout aborted; previous revision keeps serving
)

// ReleaseKind explains why a release row exists. Rollback is a brand-new
// release operation, never a mutation of an older row.
type ReleaseKind string

const (
	KindBootstrap ReleaseKind = "bootstrap" // initial revision recorded at workload creation
	KindRollout   ReleaseKind = "rollout"
	KindRollback  ReleaseKind = "rollback"
)

// FailureCategory is the closed set of failure conclusions a release can end
// with. Empty for non-failed releases.
type FailureCategory string

const (
	// FailStartFailed: a new instance could not start or exited during startup
	// and the release start-failure budget was exhausted.
	FailStartFailed FailureCategory = "start_failed"
	// FailReadinessFlapping: a new instance oscillated ready/not-ready and never
	// held readiness for the threshold before the deadline.
	FailReadinessFlapping FailureCategory = "readiness_flapping"
	// FailInsufficientCapacity: the simulated process manager refused starts
	// because its capacity was full and no old instance could be removed while
	// respecting maxUnavailable.
	FailInsufficientCapacity FailureCategory = "insufficient_capacity"
	// FailRolloutStalled: the deadline elapsed without any structural progress
	// for any other reason.
	FailRolloutStalled FailureCategory = "rollout_stalled"
	// FailRuntimeExited: a running process disappeared outside a start failure
	// (used for instance-level failures; steady state replaces it).
	FailRuntimeExited FailureCategory = "runtime_exited"
)

// Workload is the desired resource: a named group of identical replicas.
type Workload struct {
	Name             string    `json:"name"`
	Replicas         int       `json:"replicas"`
	CurrentRevision  string    `json:"currentRevision"`
	CurrentReleaseID string    `json:"currentReleaseId"`
	CreatedAt        time.Time `json:"createdAt"`
}

// Instance is one concrete worker process owned by a workload.
type Instance struct {
	ID             string          `json:"id"`
	Workload       string          `json:"workload"`
	Revision       string          `json:"revision"`
	ReleaseID      string          `json:"releaseId"`
	ProcID         string          `json:"procId"`
	State          InstanceState   `json:"state"`
	ReadyStreak    int             `json:"readyStreak"`
	CreatedTick    int64           `json:"createdTick"`
	ReadyTick      int64           `json:"readyTick,omitempty"`
	FailedTick     int64           `json:"failedTick,omitempty"`
	TerminatedTick int64           `json:"terminatedTick,omitempty"`
	FailCategory   FailureCategory `json:"failCategory,omitempty"`
	FailMessage    string          `json:"failMessage,omitempty"`
	CreatedAt      time.Time       `json:"createdAt"`
	UpdatedAt      time.Time       `json:"updatedAt"`
}

// Live reports whether the instance currently occupies a process slot.
func (i *Instance) Live() bool { return i.State == StateStarting || i.State == StateReady }

// Available reports whether the instance currently counts toward the serving
// capacity of the workload (stably ready).
func (i *Instance) Available() bool { return i.State == StateReady }

// Policy is the rolling-update constraint set attached to a release.
type Policy struct {
	// MaxSurge: live replicas may exceed the desired baseline by at most this
	// number at every step.
	MaxSurge int `json:"maxSurge"`
	// MaxUnavailable: at least baseline-MaxUnavailable replicas must be
	// available (stably ready) at every step.
	MaxUnavailable int `json:"maxUnavailable"`
	// ReadyThresholdTicks: consecutive ready observations required before a
	// starting instance is promoted to ready.
	ReadyThresholdTicks int `json:"readyThresholdTicks"`
	// DeadlineTicks: no-progress ticks after which the release fails.
	DeadlineTicks int64 `json:"deadlineTicks"`
	// MaxStartFailures: tolerated failed new-instance starts (0 = fail release
	// on the first start failure).
	MaxStartFailures int `json:"maxStartFailures"`
}

// Release is one rollout operation, historical rows retained forever.
type Release struct {
	ID                string          `json:"id"`
	Workload          string          `json:"workload"`
	Revision          string          `json:"revision"`
	PreviousReleaseID string          `json:"previousReleaseId,omitempty"`
	State             ReleaseState    `json:"state"`
	Kind              ReleaseKind     `json:"kind"`
	Policy            Policy          `json:"policy"`
	FailureCategory   FailureCategory `json:"failureCategory,omitempty"`
	FailMessage       string          `json:"failMessage,omitempty"`
	// RollbackOf is set when Kind == KindRollback and names the release being
	// undone.
	RollbackOf   string    `json:"rollbackOf,omitempty"`
	RequestID    string    `json:"requestId,omitempty"`
	CreatedAt    time.Time `json:"createdAt"`
	StartedTick  int64     `json:"startedTick,omitempty"`
	FinishedTick int64     `json:"finishedTick,omitempty"`
	// bookkeeping used by the planner
	LastProgressTick int64 `json:"-"`
	SawFlap          bool  `json:"-"`
	SawCapacityBlock bool  `json:"-"`
}

// EventLevel separates hard facts from transient uncertainty.
type EventLevel string

const (
	LevelInfo EventLevel = "info"
	LevelWarn EventLevel = "warn"
	LevelFail EventLevel = "fail"
)

// Event is one auditable step in a release's history.
type Event struct {
	Seq        int64      `json:"seq"`
	Tick       int64      `json:"tick"`
	TS         time.Time  `json:"ts"`
	RequestID  string     `json:"requestId,omitempty"`
	Workload   string     `json:"workload"`
	ReleaseID  string     `json:"releaseId,omitempty"`
	InstanceID string     `json:"instanceId,omitempty"`
	Revision   string     `json:"revision,omitempty"`
	Level      EventLevel `json:"level"`
	Type       string     `json:"type"`
	Category   string     `json:"category,omitempty"`
	// Certain=false marks a transient/uncertain conclusion (a capacity block
	// that may clear); hard failures are Certain=true.
	Certain bool   `json:"certain"`
	Message string `json:"message"`
}

// Event types (closed vocabulary used by planner, API tests and the demo).
const (
	EvReleaseCreated      = "release_created"
	EvReleaseActivated    = "release_activated"
	EvInstanceStart       = "instance_start_requested"
	EvInstanceReady       = "instance_ready"
	EvInstanceNotReady    = "instance_not_ready"
	EvInstanceReadyLost   = "instance_ready_lost"
	EvInstanceStartFailed = "instance_start_failed"
	EvInstanceExited      = "instance_exited"
	EvInstanceTerminated  = "instance_terminated"
	EvBlockedCapacity     = "blocked_capacity"
	EvWaitReady           = "wait_ready"
	EvRolloutSucceeded    = "rollout_succeeded"
	EvRolloutFailed       = "rollout_failed"
	EvSteadyStart         = "steady_replica_started"
)
