package channel_test

import (
	"context"
	"sync"
	"testing"

	"clsnap/internal/apperr"
	"clsnap/internal/channel"
	"clsnap/internal/protocol"
)

// memPending is a minimal PendingStore for outbox ordering tests.
type memPending struct {
	mu sync.Mutex
	q  map[string][]protocol.Envelope
}

func newMem() *memPending { return &memPending{q: map[string][]protocol.Envelope{}} }
func key(s, d protocol.NodeID) string { return string(s) + "->" + string(d) }

func (m *memPending) AppendOutbox(e protocol.Envelope) (uint64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	k := key(e.Src, e.Dst)
	seq := uint64(len(m.q[k]) + 1)
	e.Seq = seq
	m.q[k] = append(m.q[k], e)
	return seq, nil
}
func (m *memPending) ListOutbox(s, d protocol.NodeID) ([]protocol.Envelope, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := append([]protocol.Envelope(nil), m.q[key(s, d)]...)
	return out, nil
}
func (m *memPending) AckOutbox(s, d protocol.NodeID, seq uint64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	k := key(s, d)
	for i, e := range m.q[k] {
		if e.Seq == seq {
			m.q[k] = append(m.q[k][:i], m.q[k][i+1:]...)
			return nil
		}
	}
	return apperr.Inputf(apperr.CodeMalformed, "no seq")
}
func (m *memPending) PurgeMarkers(s, d protocol.NodeID, snap protocol.SnapshotID) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	k := key(s, d)
	kept := m.q[k][:0]
	removed := 0
	for _, e := range m.q[k] {
		if e.Type == protocol.MsgMarker && e.Marker != nil && e.Marker.Snapshot == snap {
			removed++
			continue
		}
		kept = append(kept, e)
	}
	m.q[k] = kept
	return removed, nil
}

func env(i int) protocol.Envelope {
	return protocol.Envelope{Type: protocol.MsgTransfer, Src: "n1", Dst: "n2",
		Transfer: &protocol.Transfer{Ref: string(rune('a' + i)), From: "a", To: "d", Amount: uint64(i + 1)}}
}

func TestOutboxFIFOOrderAndSeq(t *testing.T) {
	p := newMem()
	ob := channel.NewOutbox("n1", "n2", p)
	ctx := context.Background()
	for i := 0; i < 5; i++ {
		e, err := ob.Enqueue(env(i))
		if err != nil {
			t.Fatal(err)
		}
		if e.Seq != uint64(i+1) {
			t.Fatalf("seq = %d, want %d", e.Seq, i+1)
		}
	}
	got, err := ob.Drain()
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 5 || got[0].Transfer.Ref != "a" || got[4].Transfer.Ref != "e" {
		t.Fatalf("FIFO order broken: %+v", got)
	}
	if err := ob.Ack(got[0].Seq); err != nil {
		t.Fatal(err)
	}
	rest, _ := ob.Drain()
	if len(rest) != 4 || rest[0].Transfer.Ref != "b" {
		t.Fatalf("ack did not remove head")
	}
	_ = ctx
}

func TestOutboxRejectsMisdirectedEnvelope(t *testing.T) {
	ob := channel.NewOutbox("n1", "n2", newMem())
	bad := protocol.Envelope{Src: "n1", Dst: "n3"}
	if _, err := ob.Enqueue(bad); !apperr.IsKind(err, apperr.KindInput) {
		t.Fatalf("misdirected envelope: %v", err)
	}
}

func TestInboxFIFOPushPop(t *testing.T) {
	in := channel.NewInbox("n1", "n2")
	for i := 0; i < 3; i++ {
		if err := in.Push(env(i)); err != nil {
			t.Fatal(err)
		}
	}
	if in.Len() != 3 {
		t.Fatalf("len %d", in.Len())
	}
	for i := 0; i < 3; i++ {
		e, ok := in.Pop()
		if !ok || e.Transfer.Ref != string(rune('a'+i)) {
			t.Fatalf("pop %d: %+v ok=%v", i, e, ok)
		}
	}
	if _, ok := in.Pop(); ok {
		t.Fatal("pop on empty inbox returned true")
	}
}
