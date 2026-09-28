package coordinator

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"evictor/internal/domain"
	"evictor/internal/logx"
	"evictor/internal/store"
)

// RejectError carries a concrete failure category (domain.Reason*) plus the
// evaluated budget snapshot back to the HTTP layer. It is the typed form of
// "why": the server maps its Reason to a status code and logs it with the
// request id.
type RejectError struct {
	Reason string
	Detail string
	Budget domain.Budget
}

func (e *RejectError) Error() string { return e.Reason + ": " + e.Detail }

// AsReject unwraps a *RejectError from any error in the chain.
func AsReject(err error) (*RejectError, bool) {
	var r *RejectError
	if errors.As(err, &r) {
		return r, true
	}
	return nil, false
}

// Config tunes the loop. Zero values are replaced with sane defaults.
type Config struct {
	ObservationMaxAge time.Duration // freshness contract of one observation
	Interval          time.Duration // reconcile tick
	EvictionTTL       time.Duration // default approval TTL when policy omits one
}

// Coordinator ties the store, the readiness adapter and a clock together.
type Coordinator struct {
	st  *store.Store
	obs Observer
	cl  Clock
	log *logx.Logger
	cfg Config

	mu       sync.Mutex
	drainers map[string]Drainer // group -> voluntary-move actuator
}

// Drainer is the actuator half of the local adapter: when the coordinator
// approves an eviction it calls Drain to ask the backend to start moving the
// replica. Returning an error means the move could not START; the approval is
// immediately failed (no budget charge). The synthetic cluster implements
// this. Failures AFTER start are observed as domain.StateFailed.
type Drainer interface {
	MarkDraining(id string) bool
}

// Options wires a coordinator.
type Options struct {
	Store   *store.Store
	Observe Observer
	Clock   func() time.Time
	Logger  *logx.Logger
	Config  Config
}

func New(o Options) *Coordinator {
	cfg := o.Config
	if cfg.ObservationMaxAge == 0 {
		cfg.ObservationMaxAge = 5 * time.Second
	}
	if cfg.Interval == 0 {
		cfg.Interval = time.Second
	}
	if cfg.EvictionTTL == 0 {
		cfg.EvictionTTL = 30 * time.Second
	}
	cl := Clock(funcClock{f: time.Now})
	if o.Clock != nil {
		cl = funcClock{f: o.Clock}
	}
	if o.Logger == nil {
		o.Logger = logx.New(nil)
	}
	return &Coordinator{
		st:       o.Store,
		obs:      o.Observe,
		cl:       cl,
		log:      o.Logger,
		cfg:      cfg,
		drainers: map[string]Drainer{},
	}
}

// RegisterDrainer binds a group's actuator.
func (c *Coordinator) RegisterDrainer(group string, d Drainer) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.drainers[group] = d
}

func (c *Coordinator) drainer(group string) Drainer {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.drainers[group]
}

// ---------------------------------------------------------------------------
// Observation ingestion + settlement (one reconcile pass per group)
// ---------------------------------------------------------------------------

// Reconcile performs one pass for a group: pull a REAL observation, ingest
// it atomically, then settle open evictions according to the states actually
// observed, and reap TTL-expired approvals. It is safe to call concurrently
// with Approve; every mutating step is an independent serialized transaction.
func (c *Coordinator) Reconcile(ctx context.Context, group string) error {
	now := c.cl.Now()
	ob := c.obs.Observe(group, c.cfg.ObservationMaxAge)
	if len(ob.Instances) == 0 {
		// An empty observation of a configured group is suspicious, not an
		// authoritative "everything is gone": skip ingestion but still run
		// the TTL reaper so stalled approvals cannot live forever.
		c.log.Event("warn", "observation-empty", "", logx.F("group", group))
		return c.reapExpired(ctx, group, now)
	}
	rows := make([]store.ObservedInstance, 0, len(ob.Instances))
	for _, in := range ob.Instances {
		rows = append(rows, store.ObservedInstance{
			ID: in.ID, State: string(in.State),
			Labels: in.Labels, SelVersion: in.SelVersion,
		})
	}
	ing, err := c.st.IngestObservation(ctx, group, ob.At, rows)
	if err != nil {
		return fmt.Errorf("coordinator: ingest: %w", err)
	}
	state := map[string]domain.InstanceState{}
	for _, in := range ob.Instances {
		state[in.ID] = in.State
	}
	if err := c.settle(ctx, group, ob, state, ing.ObservationID, now); err != nil {
		return err
	}
	return c.reapExpired(ctx, group, now)
}

// settle walks open evictions and closes them based on the freshly ingested
// observation. Crucially the involuntarily failed branch frees the
// reservation with kind "involuntary-failure" — an explicit confirmation,
// never a silent release and never a budget-exhausted decision.
func (c *Coordinator) settle(ctx context.Context, group string, ob domain.Observation,
	state map[string]domain.InstanceState, observationID int64, now time.Time) error {
	open, err := c.st.ApprovedEvictions(ctx, group)
	if err != nil {
		return err
	}
	for _, e := range open {
		st, seen := state[e.InstanceID]
		switch {
		case seen && st.Involuntary():
			detail := fmt.Sprintf("instance %s observed failed before eviction completed; "+
				"reservation released without charging voluntary budget", e.InstanceID)
			if _, err := c.st.Fail(ctx, e.ID, detail, "reconcile:"+group, observationID, now); err != nil {
				return err
			}
			c.audit(ctx, auditInput{
				TS: now, Group: group, InstanceID: e.InstanceID,
				Action: "settle-failure", Outcome: "settled",
				Reason: domain.ReasonInvoluntaryFailure, Detail: detail,
			})
			c.log.Event("error", "eviction-failed-involuntary", "",
				logx.F("group", group), logx.F("eviction", e.ID),
				logx.F("instance", e.InstanceID), logx.F("observed_at", ob.At.Format(time.RFC3339)))
		case seen && st == domain.StateGone:
			detail := fmt.Sprintf("instance %s observed gone; voluntary move completed", e.InstanceID)
			if _, err := c.st.Complete(ctx, e.ID, detail, "reconcile:"+group, observationID, now); err != nil {
				return err
			}
			c.audit(ctx, auditInput{
				TS: now, Group: group, InstanceID: e.InstanceID,
				Action: "settle-completion", Outcome: "settled",
				Reason: domain.ReasonAccepted, Detail: detail,
			})
			c.log.Event("info", "eviction-completed", "",
				logx.F("group", group), logx.F("eviction", e.ID),
				logx.F("instance", e.InstanceID))
		}
	}
	return nil
}

// reapExpired closes approvals whose deadline passed. Reaping is an explicit
// confirmation event (kind ttl-expiry), so released budget is always
// auditable. A "stalled after approval" pause frees its slot this way.
func (c *Coordinator) reapExpired(ctx context.Context, group string, now time.Time) error {
	expired, err := c.st.ExpiredEvictions(ctx, now)
	if err != nil {
		return err
	}
	for _, e := range expired {
		if e.Group != group {
			continue
		}
		detail := fmt.Sprintf("approval %s expired at %s without completion (approved %s)",
			e.ID, e.ExpiresAt.UTC().Format(time.RFC3339), e.ApprovedAt.UTC().Format(time.RFC3339))
		if _, err := c.st.Expire(ctx, e.ID, detail, "reaper:"+group, now); err != nil {
			if errors.Is(err, store.ErrTerminal) {
				continue // another pass/request raced and settled it
			}
			return err
		}
		c.audit(ctx, auditInput{
			TS: now, Group: group, InstanceID: e.InstanceID,
			Action: "reap-ttl", Outcome: "reaped",
			Reason: "rejected:approval-ttl-expired", Detail: detail,
		})
		c.log.Event("warn", "eviction-reaped-ttl", "",
			logx.F("group", group), logx.F("eviction", e.ID),
			logx.F("instance", e.InstanceID))
	}
	return nil
}
