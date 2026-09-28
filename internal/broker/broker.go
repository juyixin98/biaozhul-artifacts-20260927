// Package broker is the service/orchestration layer: it validates requests,
// applies configuration limits, drives long polling against the Store and
// exposes the replay read model. It contains no SQL or HTTP.
package broker

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"workbroker/internal/config"
	"workbroker/internal/protocol"
	"workbroker/internal/replay"
	"workbroker/internal/store"
)

// Broker is the service facade over a Store.
type Broker struct {
	st   store.Store
	conf config.Config
}

// New constructs a Broker.
func New(st store.Store, conf config.Config) *Broker {
	return &Broker{st: st, conf: conf}
}

// Store exposes the underlying store (needed by replay endpoints/tests).
func (b *Broker) Store() store.Store { return b.st }

// SendInput is a produce request.
type SendInput struct {
	Partition    string
	Body         []byte
	MaxAttempts  int64
	DelaySeconds int64
}

// Send enqueues one message.
func (b *Broker) Send(ctx context.Context, in SendInput) (*protocol.Message, error) {
	if strings.TrimSpace(in.Partition) == "" {
		return nil, fail("send", protocol.FailValidation, "partition is required", nil)
	}
	if len(in.Body) == 0 {
		return nil, fail("send", protocol.FailValidation, "body is required", nil)
	}
	if len(in.Body) > 256*1024 {
		return nil, fail("send", protocol.FailValidation, "body exceeds 256KiB", nil)
	}
	maxAttempts := in.MaxAttempts
	if maxAttempts == 0 {
		maxAttempts = b.conf.MaxAttempts
	}
	if maxAttempts < 1 || maxAttempts > 1000 {
		return nil, fail("send", protocol.FailValidation,
			fmt.Sprintf("max_attempts=%d outside [1,1000]", maxAttempts), nil)
	}
	if in.DelaySeconds < 0 || in.DelaySeconds > 900 {
		return nil, fail("send", protocol.FailValidation,
			fmt.Sprintf("delay_seconds=%d outside [0,900]", in.DelaySeconds), nil)
	}
	msg, _, err := b.st.Enqueue(ctx, &store.EnqueueInput{
		Partition:   in.Partition,
		Body:        in.Body,
		MaxAttempts: maxAttempts,
		Delay:       time.Duration(in.DelaySeconds) * time.Second,
	})
	if err != nil {
		return nil, err
	}
	return msg, nil
}

// Delivered is one receive result for the API.
type Delivered struct {
	ID              string
	ReceiptID       string
	Body            []byte
	Attempts        int64
	Partition       string
	ReceivedAt      time.Time
	VisibilityUntil time.Time
}

// Receive runs one (possibly long-polling) receive.
func (b *Broker) Receive(ctx context.Context, partition, workerID string, visibilitySeconds, waitSeconds int64) ([]Delivered, error) {
	if strings.TrimSpace(partition) == "" {
		return nil, fail("receive", protocol.FailValidation, "partition is required", nil)
	}
	if strings.TrimSpace(workerID) == "" {
		return nil, fail("receive", protocol.FailValidation, "worker_id is required", nil)
	}
	visibility, err := b.conf.ValidateVisibility(time.Duration(visibilitySeconds) * time.Second)
	if err != nil {
		return nil, fail("receive", protocol.FailValidation, err.Error(), err)
	}
	maxWait := int64(b.conf.LongPollWait.Seconds())
	if waitSeconds < 0 || waitSeconds > maxWait {
		return nil, fail("receive", protocol.FailValidation,
			fmt.Sprintf("wait_seconds outside [0,%d]", maxWait), nil)
	}

	pollCtx := ctx
	var cancel context.CancelFunc
	if waitSeconds > 0 {
		pollCtx, cancel = context.WithTimeout(ctx, time.Duration(waitSeconds)*time.Second)
		defer cancel()
	}

	in := store.ReceiveInput{
		Partition:   partition,
		WorkerID:    workerID,
		Visibility:  visibility,
		MaxMessages: 10,
	}
	if waitSeconds == 0 {
		// One non-blocking attempt; empty result is valid (HTTP 200 []).
		got, err := b.st.Receive(pollCtx, in)
		if err != nil {
			return nil, err
		}
		return toDelivered(got), nil
	}

	for {
		got, err := b.st.Receive(pollCtx, in)
		if err == nil && len(got) > 0 {
			return toDelivered(got), nil
		}
		if err != nil && !isTimeout(err) {
			return nil, err
		}
		if pollCtx.Err() != nil {
			if errors.Is(pollCtx.Err(), context.DeadlineExceeded) {
				// Long-poll timeout with no messages: return empty list.
				return []Delivered{}, nil
			}
			return nil, fail("receive", protocol.FailTimeout, "receive canceled", pollCtx.Err())
		}
	}
}

func toDelivered(in []store.Received) []Delivered {
	out := make([]Delivered, 0, len(in))
	for _, r := range in {
		out = append(out, Delivered{
			ID:              r.Message.ID,
			ReceiptID:       r.Receipt.ID,
			Body:            r.Message.Body,
			Attempts:        r.Message.Attempts,
			Partition:       r.Message.Partition,
			ReceivedAt:      r.Receipt.IssuedAt,
			VisibilityUntil: r.Receipt.ExpiresAt,
		})
	}
	return out
}

func isTimeout(err error) bool {
	f := protocol.AsFailure(err)
	return f != nil && f.Class == protocol.FailTimeout
}

// Extend extends visibility for one receipt.
func (b *Broker) Extend(ctx context.Context, receiptID string, extendSeconds int64) (time.Time, error) {
	if strings.TrimSpace(receiptID) == "" {
		return time.Time{}, fail("extend", protocol.FailValidation, "receipt_id is required", nil)
	}
	if extendSeconds <= 0 {
		return time.Time{}, fail("extend", protocol.FailValidation, "extend_seconds must be > 0", nil)
	}
	d := time.Duration(extendSeconds) * time.Second
	if d < b.conf.VisibilityMin || d > b.conf.VisibilityMax {
		return time.Time{}, fail("extend", protocol.FailValidation,
			fmt.Sprintf("extend_seconds outside [%d,%d]",
				int64(b.conf.VisibilityMin.Seconds()), int64(b.conf.VisibilityMax.Seconds())), nil)
	}
	msg, _, err := b.st.Extend(ctx, receiptID, d)
	if err != nil {
		return time.Time{}, err
	}
	return msg.VisibilityDeadline, nil
}

// Ack confirms one receipt.
func (b *Broker) Ack(ctx context.Context, receiptID string) error {
	if strings.TrimSpace(receiptID) == "" {
		return fail("ack", protocol.FailValidation, "receipt_id is required", nil)
	}
	_, _, err := b.st.Ack(ctx, receiptID)
	return err
}

// Nack fails one receipt explicitly.
func (b *Broker) Nack(ctx context.Context, receiptID, reason string) error {
	if strings.TrimSpace(receiptID) == "" {
		return fail("nack", protocol.FailValidation, "receipt_id is required", nil)
	}
	_, _, err := b.st.Nack(ctx, receiptID, reason)
	return err
}

// FailureView is the API shape of an attempt failure.
type FailureView struct {
	ID         string    `json:"id"`
	Attempt    int64     `json:"attempt"`
	Cause      string    `json:"cause"`
	Reason     string    `json:"reason"`
	WorkerID   string    `json:"worker_id"`
	HappenedAt time.Time `json:"happened_at"`
}

// DeadView is one dead-letter record with its complete history.
type DeadView struct {
	ID        string        `json:"id"`
	Partition string        `json:"partition"`
	Body      []byte        `json:"body"`
	Attempts  int64         `json:"attempts"`
	Cause     string        `json:"cause"`
	Reason    string        `json:"reason"`
	DeadAt    time.Time     `json:"dead_at"`
	Failures  []FailureView `json:"failures"`
}

// DeadList returns dead-letter messages.
func (b *Broker) DeadList(ctx context.Context, partition string, limit int) ([]DeadView, error) {
	if strings.TrimSpace(partition) == "" {
		return nil, fail("deadlist", protocol.FailValidation, "partition is required", nil)
	}
	if limit <= 0 || limit > 100 {
		limit = 50
	}
	recs, err := b.st.DeadList(ctx, partition, limit)
	if err != nil {
		return nil, err
	}
	out := make([]DeadView, 0, len(recs))
	for _, r := range recs {
		v := DeadView{
			ID:        r.Message.ID,
			Partition: r.Message.Partition,
			Body:      r.Message.Body,
			Attempts:  r.Message.Attempts,
		}
		if r.Reason != nil {
			v.Cause = string(r.Reason.Class)
			v.Reason = r.Reason.Reason
			v.DeadAt = r.Reason.HappenedAt
			v.Failures = make([]FailureView, 0, len(r.Reason.Failures))
			for _, f := range r.Reason.Failures {
				v.Failures = append(v.Failures, FailureView{
					ID: f.ID, Attempt: f.Attempt, Cause: string(f.Class),
					Reason: f.Reason, WorkerID: f.WorkerID, HappenedAt: f.HappenedAt,
				})
			}
		}
		out = append(out, v)
	}
	return out, nil
}

// ReplaySnapshot returns the state reconstructed from the full event stream.
func (b *Broker) ReplaySnapshot(ctx context.Context) (*replay.Snapshot, error) {
	return replay.Build(func(after int64, limit int) ([]protocol.Event, error) {
		return b.st.Events(ctx, after, limit)
	})
}

func fail(op string, class protocol.FailureClass, detail string, cause error) error {
	return protocol.NewFailure(op, class, detail, cause)
}
