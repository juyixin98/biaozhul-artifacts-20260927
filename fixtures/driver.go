package fixtures

import (
	"context"
	"sync"
	"time"

	"example.com/cgcoord/coordinator"
	"example.com/cgcoord/protocol"
)

// Driver runs synthetic members against a coordinator using a controllable
// clock. It records the request id of every call so failures can be tied to
// a concrete request in journal and logs.
type Driver struct {
	Coord *coordinator.Coordinator
	Clock *Clock

	mu      sync.Mutex
	members map[string]*memberState
	scen    Scenario
}

type memberState struct {
	spec     *MemberSpec
	gen      protocol.Generation
	owned    map[protocol.TP]struct{}
	pending  map[protocol.TP]protocol.Generation
	joinedAt time.Time
}

// NewDriver wires a driver for a scenario.
func NewDriver(coord *coordinator.Coordinator, clock *Clock, scen Scenario) *Driver {
	return &Driver{
		Coord: coord, Clock: clock, scen: scen,
		members: map[string]*memberState{},
	}
}

// CreateGroup creates the scenario group at the clock's current time.
func (d *Driver) CreateGroup(ctx context.Context) error {
	topics := make([]protocol.TopicSpec, 0, len(d.scen.Topics))
	for name, n := range d.scen.Topics {
		topics = append(topics, protocol.TopicSpec{Name: protocol.Topic(name), Partitions: n})
	}
	return d.Coord.CreateGroup(ctx, coordinator.CreateGroupRequest{
		Name: d.scen.Group,
		Topics: topics,
		Config: protocol.GroupConfig{
			RevokeTimeout:         time.Duration(d.scen.RevokeTimeoutMS) * time.Millisecond,
			QuarantineTimeout:     time.Duration(d.scen.QuarantineTimeoutMS) * time.Millisecond,
			DefaultSessionTimeout: time.Duration(d.scen.SessionTimeoutMS) * time.Millisecond,
		},
		RequestID: "fixture:create:" + d.scen.Group,
		At:        d.Clock.Now(),
	})
}

// Join makes one member join and performs its cooperative obligations
// (revoke acks) according to policy. Slow members simply do not ack.
func (d *Driver) Join(ctx context.Context, id string) (*JoinTrace, error) {
	spec := d.findSpec(id)
	timeout := 0
	if spec != nil && spec.SessionTimeoutMS > 0 {
		timeout = spec.SessionTimeoutMS
	}
	topics := []protocol.Topic{}
	if spec != nil {
		for _, t := range spec.Topics {
			topics = append(topics, protocol.Topic(t))
		}
	}
	trace := &JoinTrace{Member: id}
	res, err := d.Coord.Join(ctx, coordinator.JoinRequest{
		Group: d.scen.Group,
		Member: protocol.MemberSpec{
			ID: protocol.MemberID(id),
			Subscription: protocol.Subscription{Topics: topics},
			SessionTimeout: time.Duration(timeout) * time.Millisecond,
		},
		RequestID: "fixture:join:" + id,
		At:        d.Clock.Now(),
	})
	if err != nil {
		return trace, err
	}
	trace.Phase = res.Phase
	trace.Generation = res.Generation
	d.putMember(id, spec, res.Generation)
	if err := d.observe(ctx, id, trace); err != nil {
		return trace, err
	}
	return trace, nil
}

// Heartbeat refreshes a member and processes its obligations.
func (d *Driver) Heartbeat(ctx context.Context, id string) (*coordinator.HeartbeatResult, error) {
	ms := d.member(id)
	var gen protocol.Generation
	if ms != nil {
		gen = ms.gen
	}
	res, err := d.Coord.Heartbeat(ctx, coordinator.HeartbeatRequest{
		Group: d.scen.Group, Member: protocol.MemberID(id),
		Generation: gen, RequestID: "fixture:heartbeat:" + id, At: d.Clock.Now(),
	})
	if err != nil {
		return nil, err
	}
	if ms != nil {
		ms.gen = res.Generation
	}
	return res, nil
}

// Ack makes a member confirm specific revocations regardless of policy;
// tests use it to simulate late confirmations.
func (d *Driver) Ack(ctx context.Context, id string, gen protocol.Generation, tps []protocol.TP) (*coordinator.AckResult, error) {
	return d.Coord.AckRevocations(ctx, coordinator.AckRequest{
		Group: d.scen.Group, Member: protocol.MemberID(id),
		Generation: gen, Partitions: tps,
		RequestID: "fixture:ack:" + id, At: d.Clock.Now(),
	})
}

// Leave makes a member leave in order.
func (d *Driver) Leave(ctx context.Context, id string) (*coordinator.LeaveResult, error) {
	res, err := d.Coord.Leave(ctx, coordinator.LeaveRequest{
		Group: d.scen.Group, Member: protocol.MemberID(id),
		RequestID: "fixture:leave:" + id, At: d.Clock.Now(),
	})
	if err == nil {
		d.mu.Lock()
		delete(d.members, id)
		d.mu.Unlock()
	}
	return res, err
}

// Sweep runs the coordinator sweep at the clock's current time.
func (d *Driver) Sweep(ctx context.Context) (*coordinator.SweepResult, error) {
	return d.Coord.Sweep(ctx, d.scen.Group, d.Clock.Now())
}

// Commit submits one offset for a member in a generation.
func (d *Driver) Commit(ctx context.Context, id string, gen protocol.Generation, tp protocol.TP, offset int64) (*coordinator.CommitResult, error) {
	return d.Coord.Commit(ctx, coordinator.CommitRequest{
		Group: d.scen.Group, Member: protocol.MemberID(id),
		Generation: gen,
		Items:      []coordinator.CommitItem{{TP: tp, Offset: offset, LeaderEpoch: -1}},
		RequestID:  "fixture:commit:" + id,
		At:         d.Clock.Now(),
	})
}

// Recover forces the out-of-band recovery path for partitions.
func (d *Driver) Recover(ctx context.Context, gen protocol.Generation, reason string, tps ...protocol.TP) (*coordinator.AckResult, error) {
	return d.Coord.RecoverAck(ctx, coordinator.RecoverAckRequest{
		Group: d.scen.Group, Generation: gen, Partitions: tps,
		Reason: reason, RequestID: "fixture:recover", At: d.Clock.Now(),
	})
}

// observe handles the response to a join/sync: cooperative members ack the
// revocations they were asked to perform and then record their assignment.
func (d *Driver) observe(ctx context.Context, id string, trace *JoinTrace) error {
	ms := d.member(id)
	// The joining member usually has nothing to revoke; the members that
	// must release partitions are the existing owners. Drive each of them.
	for _, otherID := range d.memberIDs() {
		other := d.member(otherID)
		if other == nil {
			continue
		}
		hb, err := d.Coord.Heartbeat(ctx, coordinator.HeartbeatRequest{
			Group: d.scen.Group, Member: protocol.MemberID(otherID),
			Generation: other.gen, RequestID: "fixture:observe-hb:" + otherID, At: d.Clock.Now(),
		})
		if err != nil {
			return err
		}
		other.gen = hb.Generation
		if len(hb.Revoke) > 0 && other.spec != nil && other.spec.Policy == PolicyCooperative {
			ack, err := d.Coord.AckRevocations(ctx, coordinator.AckRequest{
				Group: d.scen.Group, Member: protocol.MemberID(otherID),
				Generation: hb.Generation, Partitions: hb.Revoke,
				RequestID: "fixture:observe-ack:" + otherID, At: d.Clock.Now(),
			})
			if err != nil {
				return err
			}
			trace.AckedByOthers = append(trace.AckedByOthers, otherID)
			_ = ack
		}
	}
	// Sync the joiner to learn its stable assignment.
	syncRes, err := d.Coord.Sync(ctx, coordinator.SyncRequest{
		Group: d.scen.Group, Member: protocol.MemberID(id),
		Generation: ms.gen, RequestID: "fixture:sync:" + id,
	})
	if err != nil {
		return err
	}
	trace.Stable = syncRes.Stable
	trace.Assignment = syncRes.Assignment
	trace.Phase = syncRes.Phase
	trace.Generation = syncRes.Generation
	ms.gen = syncRes.Generation
	return nil
}

// JoinTrace records what one join caused.
type JoinTrace struct {
	Member        string
	Phase         protocol.Phase
	Generation    protocol.Generation
	Stable        bool
	Assignment    []protocol.TP
	AckedByOthers []string
}

func (d *Driver) findSpec(id string) *MemberSpec {
	for i := range d.scen.Members {
		if d.scen.Members[i].ID == id {
			return &d.scen.Members[i]
		}
	}
	return nil
}

func (d *Driver) putMember(id string, spec *MemberSpec, gen protocol.Generation) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.members[id] = &memberState{
		spec: spec, gen: gen,
		owned:   map[protocol.TP]struct{}{},
		pending: map[protocol.TP]protocol.Generation{},
	}
}

func (d *Driver) member(id string) *memberState {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.members[id]
}

func (d *Driver) memberIDs() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]string, 0, len(d.members))
	for id := range d.members {
		out = append(out, id)
	}
	return out
}
