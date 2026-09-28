// Package coordinator implements the consumer-group coordination rules on
// top of the storage journal: join/leave/rebalance orchestration, the
// revoke-before-grant state machine, generation-fenced offset commits and
// the time-based sweep. All state changes go through the replay package by
// appending events inside SERIALIZABLE transactions.
package coordinator

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"time"

	"example.com/cgcoord/protocol"
	"example.com/cgcoord/replay"
	"example.com/cgcoord/storage"
)

// Logger is the minimal logging surface used for explainable, correlated
// diagnostics.
type Logger interface {
	// Log emits one structured record. Keys are free-form but always include
	// request_id (when present), group, member, event/phase and detail.
	Log(level, msg string, kv ...any)
}

// Coordinator owns multiple groups over one Store.
type Coordinator struct {
	store  storage.Store
	logger Logger
	now    func() time.Time

	// sweepMu serializes background sweeps against each other; request
	// serialization itself is provided by storage transactions.
	sweepMu sync.Mutex
}

// New builds a Coordinator.
func New(store storage.Store, logger Logger, now func() time.Time) *Coordinator {
	if now == nil {
		now = time.Now
	}
	if logger == nil {
		logger = nopLogger{}
	}
	return &Coordinator{store: store, logger: logger, now: now}
}

// Recover verifies every existing group by rebuilding it from the journal
// and re-saves the rebuilt blob. It returns the list of verified groups. A
// mismatch fails closed: nothing else should serve that group.
func (c *Coordinator) Recover(ctx context.Context) ([]string, error) {
	names, err := c.store.ListGroups(ctx)
	if err != nil {
		return nil, err
	}
	verified := make([]string, 0, len(names))
	reader := &storeReader{store: c.store, ctx: ctx}
	for _, name := range names {
		built, err := replay.Verify(reader, name)
		if err != nil {
			return nil, err
		}
		if built != nil {
			err := storage.WithTx(ctx, c.store, func(tx storage.Tx) error {
				return tx.SaveGroupState(built)
			})
			if err != nil {
				return nil, err
			}
		}
		verified = append(verified, name)
		c.logger.Log("info", "recovery verified group", "group", name, "generation", built.Generation, "phase", built.Phase)
	}
	return verified, nil
}

// CreateGroup creates a group.
func (c *Coordinator) CreateGroup(ctx context.Context, req CreateGroupRequest) error {
	if req.Name == "" {
		return protocol.NewError(protocol.ErrBadRequest, "group name is required")
	}
	if err := validateTopics(req.Topics); err != nil {
		return err
	}
	cfg := req.Config
	if cfg.RevokeTimeout <= 0 {
		cfg.RevokeTimeout = protocol.DefaultGroupConfig().RevokeTimeout
	}
	if cfg.QuarantineTimeout <= 0 {
		cfg.QuarantineTimeout = protocol.DefaultGroupConfig().QuarantineTimeout
	}
	if cfg.DefaultSessionTimeout <= 0 {
		cfg.DefaultSessionTimeout = protocol.DefaultGroupConfig().DefaultSessionTimeout
	}
	at := req.At
	if at.IsZero() {
		at = c.now()
	}
	return storage.WithTx(ctx, c.store, func(tx storage.Tx) error {
		err := tx.CreateGroup(req.Name)
		if err == storage.ErrGroupExists {
			return protocol.NewError(protocol.ErrBadRequest, "group %q already exists", req.Name)
		}
		if err != nil {
			return err
		}
		s := replay.NewEmptyState(req.Name)
		if err := emit(tx, s, protocol.Event{
			Group: req.Name, Type: protocol.EvGroupCreated, At: at, RequestID: req.RequestID,
			Detail: &protocol.GroupCreatedDetail{Config: cfg, Topics: req.Topics},
		}); err != nil {
			return err
		}
		if err := tx.SaveGroupState(s); err != nil {
			return err
		}
		c.logger.Log("info", "group created", "request_id", req.RequestID, "group", req.Name,
			"topics", len(req.Topics), "revoke_timeout", cfg.RevokeTimeout.String())
		return nil
	})
}

// emit appends an event and applies it through the replay engine, so the
// in-transaction state matches exactly what a restart will rebuild.
func emit(tx storage.Tx, s *protocol.GroupState, e protocol.Event) error {
	seq, err := tx.AppendEvent(e)
	if err != nil {
		return err
	}
	e.Seq = seq
	return replay.Apply(s, e)
}

func validateTopics(topics []protocol.TopicSpec) error {
	if len(topics) == 0 {
		return protocol.NewError(protocol.ErrBadRequest, "at least one topic is required")
	}
	seen := map[protocol.Topic]bool{}
	for _, t := range topics {
		if t.Name == "" {
			return protocol.NewError(protocol.ErrBadRequest, "topic name is required")
		}
		if t.Partitions <= 0 {
			return protocol.NewError(protocol.ErrBadRequest, "topic %q must have at least one partition", t.Name)
		}
		if seen[t.Name] {
			return protocol.NewError(protocol.ErrBadRequest, "duplicate topic %q", t.Name)
		}
		seen[t.Name] = true
	}
	return nil
}

// mutate loads a group, runs fn against its state, then persists. Group
// existence errors are mapped to typed errors.
func (c *Coordinator) mutate(ctx context.Context, group string, fn func(tx storage.Tx, s *protocol.GroupState) error) error {
	return storage.WithTx(ctx, c.store, func(tx storage.Tx) error {
		s, err := tx.LoadGroup(group)
		if err == storage.ErrNotFound {
			return protocol.NewError(protocol.ErrUnknownGroup, "group %q does not exist", group)
		}
		if err != nil {
			return err
		}
		if s == nil {
			return protocol.NewError(protocol.ErrUnknownGroup, "group %q is not initialized", group)
		}
		if err := fn(tx, s); err != nil {
			return err
		}
		return tx.SaveGroupState(s)
	})
}

// ownerMembers projects effective ownership to the member-only map the
// kernel assignment uses.
func ownerMembers(owners map[protocol.TP]protocol.Owner) map[protocol.TP]protocol.MemberID {
	out := make(map[protocol.TP]protocol.MemberID, len(owners))
	for tp, o := range owners {
		out[tp] = o.Member
	}
	return out
}

// sortedTPs returns map keys in canonical order.
func sortedTPs(in map[protocol.TP]struct{}) []protocol.TP {	out := make([]protocol.TP, 0, len(in))
	for tp := range in {
		out = append(out, tp)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Topic != out[j].Topic {
			return out[i].Topic < out[j].Topic
		}
		return out[i].Partition < out[j].Partition
	})
	return out
}

func errf(code protocol.ErrorCode, format string, args ...any) error {
	return protocol.NewError(code, fmt.Sprintf(format, args...))
}

type nopLogger struct{}

func (nopLogger) Log(level, msg string, kv ...any) {}
