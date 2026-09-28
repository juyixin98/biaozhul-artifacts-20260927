package kernel

import (
	"encoding/base64"
	"time"

	"workbroker/internal/protocol"
)

// EnqueueInput constructs a fresh message.
type EnqueueInput struct {
	Partition   string
	Body        []byte
	MaxAttempts int64
	Delay       time.Duration // 0 = immediately visible
}

// EnqueueResult is the persistence contract of enqueue: message + event.
type EnqueueResult struct {
	Message *protocol.Message
	Event   protocol.Event
}

// EnqueueOne constructs a new available message. A delayed message carries an
// AvailableAt in the future and is skipped by receive until then.
func EnqueueOne(in EnqueueInput, now time.Time) EnqueueResult {
	at := now.UTC()
	maxAttempts := in.MaxAttempts
	if maxAttempts <= 0 {
		maxAttempts = DefaultMaxAttempts
	}
	m := &protocol.Message{
		ID:          NewID("msg_"),
		Partition:   in.Partition,
		Body:        append([]byte(nil), in.Body...),
		Status:      protocol.StatusAvailable,
		Attempts:    0,
		MaxAttempts: maxAttempts,
		AvailableAt: at.Add(in.Delay),
		CreatedAt:   at,
		UpdatedAt:   at,
	}
	ev := event(protocol.EventEnqueued, m, at)
	ev.Data = mustJSON(EnqueueData{
		BodyBase64:  base64.StdEncoding.EncodeToString(in.Body),
		MaxAttempts: maxAttempts,
		AvailableAt: rfc3339(m.AvailableAt),
	})
	return EnqueueResult{Message: m, Event: ev}
}
