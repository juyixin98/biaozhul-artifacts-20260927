// Package broker is the application service: it owns the clock, the run
// identity and the background lease sweeper, and expresses the work-message
// protocol (claim / extend / ack / nack / dead-letter) on top of a Store.
//
// Side-effect de-duplication is deliberately NOT here: the broker guarantees
// at-least-once delivery; the consumer is responsible for making its effects
// idempotent (see the sideeffect simulation and its test).
package broker

import (
	"context"
	"sync"
	"time"

	"localbroker/internal/clock"
	"localbroker/internal/idgen"
	"localbroker/internal/protocol"
	"localbroker/internal/store"
)

// Broker coordinates a store with a clock and a sweeper.
type Broker struct {
	store store.Store
	clock clock.Clock
	runID string

	stopOnce sync.Once
	stopCh   chan struct{}
	wg       sync.WaitGroup
}

// Option customizes a Broker.
type Option func(*Broker)

// WithClock injects a clock (tests use clock.Fake).
func WithClock(c clock.Clock) Option {
	return func(b *Broker) { b.clock = c }
}

// WithRunID sets the run identity stamped on every event.
func WithRunID(id string) Option {
	return func(b *Broker) { b.runID = id }
}

// WithSweeper starts a background loop expiring due leases every interval.
func WithSweeper(interval time.Duration) Option {
	return func(b *Broker) {
		b.wg.Add(1)
		go b.sweepLoop(interval)
	}
}

// New constructs a broker over st.
func New(st store.Store, opts ...Option) *Broker {
	b := &Broker{store: st, clock: clock.System{}, runID: "run-local", stopCh: make(chan struct{})}
	for _, o := range opts {
		o(b)
	}
	return b
}

// RunID returns the run identity.
func (b *Broker) RunID() string { return b.runID }

func (b *Broker) sweepLoop(interval time.Duration) {
	defer b.wg.Done()
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-b.stopCh:
			return
		case <-t.C:
			b.Sweep(context.Background())
		}
	}
}

// Sweep expires all due leases across every queue. Errors are returned to the
// caller; the background loop ignores them but they remain observable via
// direct Sweep calls in tests.
func (b *Broker) Sweep(ctx context.Context) (total int, err error) {
	queues, err := b.store.Queues(ctx)
	if err != nil {
		return 0, err
	}
	now := b.clock.Now()
	for _, q := range queues {
		n, err := b.store.ExpireDue(ctx, q.Name, now, b.runID)
		if err != nil {
			return total, err
		}
		total += n
	}
	return total, nil
}

// Close stops the sweeper. The caller closes the store.
func (b *Broker) Close() {
	b.stopOnce.Do(func() { close(b.stopCh) })
	b.wg.Wait()
}

// CreateQueue validates and registers a queue.
func (b *Broker) CreateQueue(ctx context.Context, cfg protocol.QueueConfig) error {
	if err := cfg.Validate(); err != nil {
		return &InvalidArgument{err.Error()}
	}
	return b.store.CreateQueue(ctx, cfg)
}

// Published is the result of Publish.
type Published struct {
	ID string
}

// Publish stores a message; the id is generated server-side (sortable).
func (b *Broker) Publish(ctx context.Context, queueName string, body []byte) (Published, error) {
	id, err := idgen.MessageID()
	if err != nil {
		return Published{}, err
	}
	if err := b.store.Publish(ctx, queueName, id, body, b.clock.Now(), b.runID); err != nil {
		return Published{}, err
	}
	return Published{ID: id}, nil
}

// Delivery is one granted work item.
type Delivery struct {
	MessageID  string
	Body       []byte
	Attempts   int
	Receipt    string
	ReceiptGen int64
	Deadline   time.Time
}

// Claim generates a fresh unguessable receipt and grants one delivery.
func (b *Broker) Claim(ctx context.Context, queueName string) (Delivery, error) {
	receipt, err := idgen.Receipt()
	if err != nil {
		return Delivery{}, err
	}
	res, err := b.store.Claim(ctx, queueName, b.clock.Now(), receipt, b.runID)
	if err != nil {
		return Delivery{}, err
	}
	return Delivery{
		MessageID:  res.MessageID,
		Body:       res.Body,
		Attempts:   res.Attempts,
		Receipt:    res.Receipt,
		ReceiptGen: res.ReceiptGen,
		Deadline:   res.Deadline,
	}, nil
}

// Extend pushes the deadline to now+extra for a live receipt.
func (b *Broker) Extend(ctx context.Context, queueName, receipt string, extra time.Duration) (time.Time, error) {
	if extra <= 0 {
		return time.Time{}, &InvalidArgument{"extend duration must be > 0"}
	}
	return b.store.Extend(ctx, queueName, receipt, extra, b.clock.Now(), b.runID)
}

// Ack confirms a live receipt.
func (b *Broker) Ack(ctx context.Context, queueName, receipt string) error {
	return b.store.Ack(ctx, queueName, receipt, b.clock.Now(), b.runID)
}

// Nack reports failure for a live receipt.
func (b *Broker) Nack(ctx context.Context, queueName, receipt, reason string) error {
	return b.store.Nack(ctx, queueName, receipt, reason, b.clock.Now(), b.runID)
}

// ExpireDue exposes the atomic expiry transition for deterministic tests.
func (b *Broker) ExpireDue(ctx context.Context, queueName string) (int, error) {
	return b.store.ExpireDue(ctx, queueName, b.clock.Now(), b.runID)
}

// Message returns live state.
func (b *Broker) Message(ctx context.Context, queueName, id string) (protocol.Message, error) {
	return b.store.Message(ctx, queueName, id)
}

// Dead returns dead letters.
func (b *Broker) Dead(ctx context.Context, queueName string) ([]protocol.Message, error) {
	return b.store.ListDead(ctx, queueName)
}

// Store exposes the underlying store (replay/admin endpoints only).
func (b *Broker) Store() store.Store { return b.store }
