package protocol

import "time"

// SubscriberSnapshot is one whole-content update of a subscriber's filter set.
// Updates REPLACE the set (a snapshot), they never patch individual filters.
type SubscriberSnapshot struct {
	SubscriberID string
	Version      int64 // monotonic per-subscriber snapshot version, 1-based
	Filters      []string
	CreatedAt    time.Time
	UpdatedAt    time.Time
	Deleted      bool
}

// RoutingVersion is an immutable snapshot of the whole routing table. A message
// is bound to exactly one routing version for its entire life.
type RoutingVersion struct {
	Version   int64
	CreatedAt time.Time
	// MemberCount records how many subscriber snapshots were included.
	MemberCount int
	// SubVersions maps subscriber_id -> snapshot version frozen here.
	SubVersions map[string]int64
}

// Frame is one subscriber's filter set as frozen inside a routing version.
// Both the indexed kernel and the reference oracle consume this same DTO but
// share no matching code.
type Frame struct {
	SubscriberID string
	Filters      []Filter
}

// Message is a published message bound to a routing version.
type Message struct {
	ID        int64     `json:"id"`
	Topic     string    `json:"topic"`
	Payload   []byte    `json:"payload"`
	RV        int64     `json:"routing_version"`
	CreatedAt time.Time `json:"created_at"`
}

// Route is one delivered subscriber for a message: deduplicated, so a
// subscriber matching through several filters appears exactly once.
type Route struct {
	MessageID    int64  `json:"message_id"`
	SubscriberID string `json:"subscriber_id"`
	RV           int64  `json:"routing_version"`
	// MatchedFilter is the single winner among that subscriber's matching
	// filters (deterministic precedence), retained for diagnostics.
	MatchedFilter string `json:"matched_filter"`
}

// EventKind enumerates the audited decisions.
type EventKind string

const (
	EventUpsert  EventKind = "SUBSCRIPTION_UPSERT"
	EventDelete  EventKind = "SUBSCRIPTION_DELETE"
	EventBuildRV EventKind = "ROUTING_VERSION_BUILD"
	EventPublish EventKind = "MESSAGE_PUBLISH"
	EventReplay  EventKind = "REPLAY_QUERY"
	EventReject  EventKind = "REQUEST_REJECTED"
)

// Outcome is the decision recorded for every request.
type Outcome string

const (
	OutcomeAccepted Outcome = "ACCEPTED"
	OutcomeRejected Outcome = "REJECTED"
	OutcomeUnknown  Outcome = "INCONCLUSIVE" // storage failure before a decision committed
)

// DiagnosticEvent is the structured record attached to every request. It always
// carries the request id, key state, and why the outcome was reached. Sensitive
// fields are redacted: payload only ever appears as a SHA-256 prefix.
type DiagnosticEvent struct {
	ID         int64     `json:"id"`
	RequestID  string    `json:"request_id"`
	Kind       EventKind `json:"kind"`
	Outcome    Outcome   `json:"outcome"`
	ErrorCode  string    `json:"error_code"`
	Reason     string    `json:"reason"`
	Topic      string    `json:"topic"`
	Subscriber string    `json:"subscriber"`
	RV         int64     `json:"routing_version"`
	MessageID  int64     `json:"message_id"`
	SubVersion int64     `json:"sub_version"`
	PayloadSHA string    `json:"payload_sha256_16"`
	KeyState   string    `json:"key_state"`
	CreatedAt  time.Time `json:"created_at"`
}
