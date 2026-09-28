package store

import (
	"context"
	"sync"
	"time"

	"workbroker/internal/clock"
	"workbroker/internal/kernel"
	"workbroker/internal/protocol"
)

// Memory is the in-memory Store. It is the reference implementation of the
// state model: every transition is computed by internal/kernel and applied
// under one mutex, which is the in-process analog of the SQL transaction.
//
// It also serves long polling via a sync.Cond signaled on every state change.
type Memory struct {
	clk clock.Clock

	mu sync.Mutex
	cv *sync.Cond

	msgs     map[string]*protocol.Message
	receipts map[string]*protocol.Receipt
	failures map[string][]protocol.AttemptFailure // messageID -> history
	dead     map[string]*protocol.DeadReason
	// order preserves enqueue FIFO per partition.
	order    map[string][]string
	events   []protocol.Event
	eventSeq int64
}

// NewMemory builds an empty in-memory store.
func NewMemory(clk clock.Clock) *Memory {
	m := &Memory{
		clk:      clk,
		msgs:     map[string]*protocol.Message{},
		receipts: map[string]*protocol.Receipt{},
		failures: map[string][]protocol.AttemptFailure{},
		dead:     map[string]*protocol.DeadReason{},
		order:    map[string][]string{},
	}
	m.cv = sync.NewCond(&m.mu)
	return m
}

// wakeLocked must be called (mu held) after any change that could unblock a
// Receive waiter.
func (m *Memory) wakeLocked() { m.cv.Broadcast() }

func (m *Memory) Enqueue(_ context.Context, in *EnqueueInput) (*protocol.Message, protocol.Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	res := kernel.EnqueueOne(*in, m.clk.Now())
	msg := res.Message
	m.msgs[msg.ID] = msg
	m.order[msg.Partition] = append(m.order[msg.Partition], msg.ID)
	ev := m.appendEventLocked(res.Event)
	m.wakeLocked()
	return msg, ev, nil
}

// appendEventLocked assigns a sequence number and stores the event.
func (m *Memory) appendEventLocked(ev protocol.Event) protocol.Event {
	m.eventSeq++
	ev.Seq = m.eventSeq
	m.events = append(m.events, ev)
	return ev
}

// reapPartitionLocked applies ReapOne to every expired inflight message of the
// partition. This is the half of the timeout race owned by the broker: any
// receive, extend, ack or nack first observes the consequences of elapsed
// deadlines while holding the same atomic section, so a receipt can never be
// accepted after its window elapsed.
func (m *Memory) reapPartitionLocked(partition string, now time.Time) {
	ids := m.order[partition]
	for _, id := range ids {
		msg := m.msgs[id]
		if msg == nil || msg.Status != protocol.StatusInFlight {
			continue
		}
		if now.Before(msg.VisibilityDeadline) {
			continue
		}
		rcp := m.receipts[msg.ReceiptID]
		if rcp == nil || rcp.Consumed {
			// Defensive: an inflight message must always have a live receipt.
			continue
		}
		prior := m.failures[msg.ID]
		res := kernel.ReapOne(msg, rcp, now, prior)
		m.receipts[rcp.ID] = res.Receipt
		m.failures[msg.ID] = append(m.failures[msg.ID], *res.Failure)
		if res.Dead != nil {
			m.dead[msg.ID] = res.Dead
		}
		m.appendEventLocked(res.Event)
	}
}

func (m *Memory) Receive(ctx context.Context, in ReceiveInput) ([]Received, error) {
	max := in.MaxMessages
	if max <= 0 {
		max = 1
	}

	m.mu.Lock()
	defer m.mu.Unlock()

	for {
		now := m.clk.Now()
		m.reapPartitionLocked(in.Partition, now)

		out := m.deliverLocked(in, now, max)
		if len(out) > 0 {
			m.wakeLocked()
			return out, nil
		}
		if ctxErr := ctx.Err(); ctxErr != nil {
			return nil, protocol.NewFailure("receive", protocol.FailTimeout,
				"no message became visible before the wait deadline", ctxErr)
		}
		// Wait for a state change or context cancellation, then re-check.
		// sync.Cond has no deadline; run cancellation in a goroutine that
		// broadcasts the cond.
		stop := m.waitContext(ctx)
		m.cv.Wait()
		stop()
	}
}

// waitContext arranges a Broadcast when ctx fires; returned func cancels it.
// Caller holds mu.
func (m *Memory) waitContext(ctx context.Context) func() {
	if ctx.Done() == nil {
		return func() {}
	}
	done := make(chan struct{})
	go func() {
		select {
		case <-ctx.Done():
			m.mu.Lock()
			m.cv.Broadcast()
			m.mu.Unlock()
		case <-done:
		}
	}()
	return func() { close(done) }
}

func (m *Memory) deliverLocked(in ReceiveInput, now time.Time, max int) []Received {
	var out []Received
	for _, id := range m.order[in.Partition] {
		if len(out) >= max {
			break
		}
		msg := m.msgs[id]
		if msg == nil || msg.Status != protocol.StatusAvailable {
			continue
		}
		if msg.AvailableAt.After(now) {
			continue
		}
		res := kernel.ReceiveOne(msg, in.WorkerID, in.Visibility, now)
		m.msgs[msg.ID] = res.Message
		m.receipts[res.Receipt.ID] = res.Receipt
		m.appendEventLocked(res.Event)
		out = append(out, Received{Message: res.Message, Receipt: res.Receipt})
	}
	return out
}

// byReceiptLocked loads message + receipt for a receipt action and computes
// the newer-receipt count used by the kernel guard.
func (m *Memory) byReceiptLocked(receiptID string) (*protocol.Message, *protocol.Receipt, int) {
	rcp := m.receipts[receiptID]
	if rcp == nil {
		return nil, nil, 0
	}
	msg := m.msgs[rcp.MessageID]
	if msg == nil {
		return nil, rcp, 0
	}
	newer := 0
	for _, other := range m.receipts {
		if other.MessageID == msg.ID && other.IssuedAt.After(rcp.IssuedAt) {
			newer++
		}
	}
	return msg, rcp, newer
}

// reapByReceiptLocked observes timeouts for the partition of the receipt's
// message before an extend/ack/nack decision. Unknown receipt/message is a
// no-op (the kernel guard then classifies the request).
func (m *Memory) reapByReceiptLocked(receiptID string, now time.Time) {
	rcp := m.receipts[receiptID]
	if rcp == nil {
		return
	}
	msg := m.msgs[rcp.MessageID]
	if msg == nil {
		return
	}
	m.reapPartitionLocked(msg.Partition, now)
}

func (m *Memory) Extend(_ context.Context, receiptID string, extend time.Duration) (*protocol.Message, protocol.Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.clk.Now()
	m.reapByReceiptLocked(receiptID, now)
	msg, rcp, newer := m.byReceiptLocked(receiptID)
	res, f := kernel.ExtendOne(msg, rcp, newer, extend, now)
	if f != nil {
		return nil, protocol.Event{}, f
	}
	m.msgs[msg.ID] = res.Message
	m.receipts[rcp.ID] = res.Receipt
	ev := m.appendEventLocked(res.Event)
	m.wakeLocked()
	return res.Message, ev, nil
}

func (m *Memory) Ack(_ context.Context, receiptID string) (*protocol.Message, protocol.Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.clk.Now()
	m.reapByReceiptLocked(receiptID, now)
	msg, rcp, newer := m.byReceiptLocked(receiptID)
	res, f := kernel.AckOne(msg, rcp, newer, now)
	if f != nil {
		return nil, protocol.Event{}, f
	}
	m.msgs[msg.ID] = res.Message
	m.receipts[rcp.ID] = res.Receipt
	ev := m.appendEventLocked(res.Event)
	m.wakeLocked()
	return res.Message, ev, nil
}

func (m *Memory) Nack(_ context.Context, receiptID, reason string) (*protocol.Message, protocol.Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	now := m.clk.Now()
	m.reapByReceiptLocked(receiptID, now)
	msg, rcp, newer := m.byReceiptLocked(receiptID)
	var prior []protocol.AttemptFailure
	if msg != nil {
		prior = m.failures[msg.ID]
	}
	res, f := kernel.NackOne(msg, rcp, newer, reason, now, prior)
	if f != nil {
		return nil, protocol.Event{}, f
	}
	m.msgs[msg.ID] = res.Message
	m.receipts[rcp.ID] = res.Receipt
	m.failures[msg.ID] = append(m.failures[msg.ID], *res.Failure)
	if res.Dead != nil {
		m.dead[msg.ID] = res.Dead
	}
	ev := m.appendEventLocked(res.Event)
	m.wakeLocked()
	return res.Message, ev, nil
}

func (m *Memory) DeadList(_ context.Context, partition string, limit int) ([]DeadRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if limit <= 0 {
		limit = 100
	}
	var out []DeadRecord
	ids := m.order[partition]
	for _, id := range ids {
		msg := m.msgs[id]
		if msg == nil || msg.Status != protocol.StatusDead {
			continue
		}
		out = append(out, DeadRecord{Message: cloneMsg(msg), Reason: cloneDead(m.dead[id])})
		if len(out) >= limit {
			break
		}
	}
	return out, nil
}

func (m *Memory) Events(_ context.Context, afterSeq int64, limit int) ([]protocol.Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if limit <= 0 {
		limit = 1000
	}
	var out []protocol.Event
	for _, ev := range m.events {
		if ev.Seq <= afterSeq {
			continue
		}
		out = append(out, ev)
		if len(out) >= limit {
			break
		}
	}
	return out, nil
}

// Close is a no-op for the memory store.
func (m *Memory) Close() error {
	m.mu.Lock()
	m.cv.Broadcast()
	m.mu.Unlock()
	return nil
}

func cloneMsg(in *protocol.Message) *protocol.Message {
	if in == nil {
		return nil
	}
	cp := *in
	cp.Body = append([]byte(nil), in.Body...)
	return &cp
}

func cloneDead(in *protocol.DeadReason) *protocol.DeadReason {
	if in == nil {
		return nil
	}
	cp := *in
	cp.Failures = append([]protocol.AttemptFailure(nil), in.Failures...)
	return &cp
}
