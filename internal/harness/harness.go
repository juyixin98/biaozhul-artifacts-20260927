// Package harness provides in-process and subprocess test harnesses for the
// three-node teaching system.
package harness

import (
	"context"
	"fmt"
	"sync"
	"time"

	"clsnap/internal/node"
	"clsnap/internal/protocol"
	"clsnap/internal/runner"
	"clsnap/internal/store"
)

// LoopbackTransport delivers envelopes directly to an in-process node.
// Delivery is synchronous and per-peer FIFO (the sender pump only hands over
// one head at a time). Transport failures are injectable for tests.
type LoopbackTransport struct {
	mu      sync.Mutex
	targets map[string]*node.Node
	failTo  map[string]error // peer id -> forced delivery error
}

func NewLoopbackTransport() *LoopbackTransport {
	return &LoopbackTransport{targets: map[string]*node.Node{}, failTo: map[string]error{}}
}

func (t *LoopbackTransport) Register(n *node.Node) {
	t.mu.Lock()
	t.targets[n.NodeID()] = n
	t.mu.Unlock()
}

// FailNextTo forces delivery failures to one peer until cleared.
func (t *LoopbackTransport) FailTo(peer string, err error) {
	t.mu.Lock()
	if err == nil {
		delete(t.failTo, peer)
	} else {
		t.failTo[peer] = err
	}
	t.mu.Unlock()
}

func (t *LoopbackTransport) Deliver(ctx context.Context, baseURL string, env protocol.Envelope) error {
	t.mu.Lock()
	if e := t.failTo[env.To]; e != nil {
		t.mu.Unlock()
		return e
	}
	target := t.targets[env.To]
	t.mu.Unlock()
	if target == nil {
		return fmt.Errorf("loopback: no target %q", env.To)
	}
	return target.HandleIncoming(ctx, env)
}

// LocalNode bundles a node with its store so a Restart rebuild can reuse the
// SAME store (memory stores simulate restart abort through RecoverAborts;
// pg tests use real persistence).
type LocalNode struct {
	ID        string
	Node      *node.Node
	St        store.Store
	cfg       node.Config
	transport *LoopbackTransport
}

// LocalDriver implements runner.Driver against an in-process node.
type LocalDriver struct {
	ln *LocalNode
}

func (d *LocalDriver) ID() string      { return d.ln.Node.NodeID() }
func (d *LocalDriver) BaseURL() string { return "loopback://" + d.ln.Node.NodeID() }
func (d *LocalDriver) Close() error    { return nil }
func (d *LocalDriver) Peers() []string {
	ps := d.ln.Node.Peers()
	out := make([]string, len(ps))
	for i, p := range ps {
		out[i] = p.ID
	}
	return out
}

func (d *LocalDriver) Transfer(ctx context.Context, to, txID string, amount int64) error {
	_, err := d.ln.Node.SubmitTransfer(ctx, to, protocol.Transfer{TxID: txID, Amount: amount})
	return err
}

func (d *LocalDriver) StartSnapshot(ctx context.Context, id string) (*store.SessionRecord, error) {
	return d.ln.Node.InitiateSnapshot(ctx, id)
}

func (d *LocalDriver) Pin(ctx context.Context, peer string, pinned bool) error {
	return d.ln.Node.SetPinned(peer, pinned)
}

func (d *LocalDriver) Abort(ctx context.Context, id, reason string) error {
	return d.ln.Node.AbortSnapshot(ctx, id, reason)
}

func (d *LocalDriver) State(ctx context.Context) (map[string]int64, int64, error) {
	return d.ln.Node.Store().Balances(), d.ln.Node.ClockNow(), nil
}

func (d *LocalDriver) GetSession(ctx context.Context, id string) (*store.SessionRecord, error) {
	return d.ln.Node.Store().GetSession(id)
}

func (d *LocalDriver) Journal(ctx context.Context, id string) ([]store.Event, error) {
	return d.ln.Node.Store().Journal(ctx, id, 0)
}

func (d *LocalDriver) OutboxLen(ctx context.Context, peer string) (int, error) {
	return d.ln.Node.Store().OutboxLen(peer)
}

// Restart rebuilds the node object on the same store and runs recovery,
// exactly like a process restart. With PgStore this genuinely aborts
// in-flight sessions; with MemoryStore the unit tests use MarkRecoverAbort.
func (d *LocalDriver) Restart(ctx context.Context) error {
	return d.ln.Rebuild(ctx)
}

// Cluster is a set of three in-process nodes sharing one loopback transport.
type Cluster struct {
	RunID   string
	Nodes   map[string]*LocalNode
	Trans   *LoopbackTransport
	Drivers map[string]runner.Driver
	stores  func(id string) store.Store
}

// Option tunes cluster construction.
type Option func(*clusterOpts)

type clusterOpts struct {
	pumpInterval time.Duration
	storeFactory func(id string, peers []string, runID string) store.Store
}

func WithPumpInterval(d time.Duration) Option {
	return func(o *clusterOpts) { o.pumpInterval = d }
}

func WithStoreFactory(f func(id string, peers []string, runID string) store.Store) Option {
	return func(o *clusterOpts) { o.storeFactory = f }
}

// NewCluster builds three nodes n1,n2,n3 with the given equal initial balance
// per node, wires loopback delivery, and starts the pumps.
func NewCluster(ctx context.Context, runID string, perNode int64, opts ...Option) (*Cluster, error) {
	o := &clusterOpts{pumpInterval: 5 * time.Millisecond}
	for _, op := range opts {
		op(o)
	}
	ids := []string{"n1", "n2", "n3"}
	tr := NewLoopbackTransport()
	c := &Cluster{RunID: runID, Nodes: map[string]*LocalNode{}, Trans: tr,
		Drivers: map[string]runner.Driver{}}

	for _, id := range ids {
		peers := []node.Peer{}
		for _, other := range ids {
			if other != id {
				peers = append(peers, node.Peer{ID: other, BaseURL: "loopback://" + other})
			}
		}
		var st store.Store
		if o.storeFactory != nil {
			st = o.storeFactory(id, without(ids, id), runID)
		} else {
			st = store.NewMemoryStore(id, runID, without(ids, id), 64)
		}
		cfg := node.Config{
			NodeID:          id,
			RunID:           runID,
			InitialBalances: map[string]int64{id: perNode},
			Peers:           peers,
			OutboxCap:       64,
			Store:           st,
			Transport:       tr,
			PumpInterval:    o.pumpInterval,
		}
		n, err := node.New(ctx, cfg)
		if err != nil {
			return nil, err
		}
		n.Run(ctx)
		tr.Register(n)
		ln := &LocalNode{ID: id, Node: n, St: st, cfg: cfg, transport: tr}
		c.Nodes[id] = ln
		c.Drivers[id] = &LocalDriver{ln: ln}
	}
	return c, nil
}

func (c *Cluster) Shutdown(ctx context.Context) {
	for _, n := range c.Nodes {
		_ = n.Node.Shutdown(ctx)
	}
}

// Rebuild re-constructs the node.Node on the same config/store (restart
// simulation).
func (ln *LocalNode) Rebuild(ctx context.Context) error {
	n, err := node.New(ctx, ln.cfg)
	if err != nil {
		return err
	}
	n.Run(ctx)
	ln.transport.Register(n)
	ln.Node = n
	return nil
}

func without(xs []string, x string) []string {
	out := make([]string, 0, len(xs)-1)
	for _, v := range xs {
		if v != x {
			out = append(out, v)
		}
	}
	return out
}
