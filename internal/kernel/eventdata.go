package kernel

import "encoding/json"

// EnqueueData is the payload of EventEnqueued.
type EnqueueData struct {
	BodyBase64  string `json:"body_base64"`
	MaxAttempts int64  `json:"max_attempts"`
	AvailableAt string `json:"available_at"` // RFC3339Nano (UTC)
}

// ReceiveData is the payload of EventReceived.
type ReceiveData struct {
	Attempt    int64  `json:"attempt"`
	WorkerID   string `json:"worker_id"`
	ExpiresAt  string `json:"expires_at"`
	FromStatus string `json:"from_status"` // "available" (new receive) — dead never re-delivers
}

// ExtendData is the payload of EventVisibilityExt.
type ExtendData struct {
	WorkerID      string `json:"worker_id"`
	OldExpiresAt  string `json:"old_expires_at"`
	NewExpiresAt  string `json:"new_expires_at"`
	ExtendSeconds int64  `json:"extend_seconds"`
}

// AckData is the payload of EventAcked.
type AckData struct {
	Attempt  int64  `json:"attempt"`
	WorkerID string `json:"worker_id"`
}

// RequeueData is the payload of EventNackRequeued / EventTimeoutRetry (only
// when the message stays alive).
type RequeueData struct {
	Attempt     int64  `json:"attempt"`
	WorkerID    string `json:"worker_id"`
	Cause       string `json:"cause"` // "worker_nack" | "visibility_timeout"
	FailureID   string `json:"failure_id"`
	AvailableAt string `json:"available_at"`
}

// DeadData is the payload of EventDead. It embeds the *complete* failure
// history so the dead-letter record is self-contained.
type DeadData struct {
	Cause   string         `json:"cause"` // "max_attempts_exhausted[_nack]"
	Reason  string         `json:"reason"`
	History []FailureEntry `json:"history"`
}

// FailureEntry is one attempt in DeadData.History.
type FailureEntry struct {
	FailureID  string `json:"failure_id"`
	Attempt    int64  `json:"attempt"`
	ReceiptID  string `json:"receipt_id"`
	WorkerID   string `json:"worker_id"`
	Cause      string `json:"cause"`
	Reason     string `json:"reason"`
	HappenedAt string `json:"happened_at"`
}

func mustJSON(v any) []byte {
	b, err := json.Marshal(v)
	if err != nil {
		panic("kernel: json marshal of event payload failed: " + err.Error())
	}
	return b
}
