// Package store defines the persistence contract of a snapshot process and its
// two implementations: an in-memory store for unit tests and a PostgreSQL
// store for integration / real three-process runs.
//
// Reliability model assumed by the whole system:
//
//   - channels are RELIABLE and FIFO (the Store's per-peer outbox is persisted
//     before HTTP delivery and retried; the receiver validates per-channel
//     sequence numbers);
//   - node local state (ledger + lamport clock) is durable;
//   - an append-only journal records every protocol event so runs can be
//     replayed;
//   - on restart, sessions that were still recording are recovered in the
//     ABORTED state. The protocol forbids stitching them with fresh state.
package store

import (
	"context"
	"time"

	"clsnap/internal/protocol"
)

// Status of a snapshot session on one node.
type Status string

const (
	StatusRecording Status = "recording"
	StatusComplete  Status = "complete"
	StatusAborted   Status = "aborted" // restarted while recording, or abort API
)

// ChannelState is one node's recording of a single incoming channel.
type ChannelState struct {
	From          string             `json:"from"`
	To            string `json:"to"`
	Recorded      []protocol.Transfer `json:"recorded"`
	MarkerSeenAt  time.Time           `json:"marker_seen_at"`
	MarkerLamport int64               `json:"marker_lamport"`
	TotalInFlight int64               `json:"total_in_flight"`
}

// LocalState is a node's frozen self-state at its first marker.
type LocalState struct {
	NodeID       string         `json:"node_id"`
	Lamport      int64          `json:"lamport"`
	RecordedAt   time.Time      `json:"recorded_at"`
	Balances     map[string]int64 `json:"balances"`
	TotalBalance int64          `json:"total_balance"`
}

// SessionRecord is the full per-node record of one snapshot.
type SessionRecord struct {
	SessionID    string                    `json:"session_id"`
	Initiator    string                    `json:"initiator"`
	Status       Status                    `json:"status"`
	StartedAt    time.Time                 `json:"started_at"`
	CompletedAt  *time.Time                `json:"completed_at,omitempty"`
	AbortedAt    *time.Time                `json:"aborted_at,omitempty"`
	AbortReason  string                    `json:"abort_reason,omitempty"`
	Local        *LocalState               `json:"local,omitempty"`
	Channels     map[string]*ChannelState  `json:"channels,omitempty"`
	PeersPending []string                  `json:"peers_pending,omitempty"`
}

// OutboxItem is a persisted message awaiting/under HTTP delivery.
type OutboxItem struct {
	ID       int64
	ToPeer   string
	Envelope protocol.Envelope
}

// Event kinds in the replay journal.
const (
	EvNodeBoot       = "node_boot"
	EvTransferSent   = "transfer_sent"
	EvTransferIn     = "transfer_in"
	EvMarkerSent     = "marker_sent"
	EvMarkerIn       = "marker_in"
	EvStateRecorded  = "local_state_recorded"
	EvChannelRecord  = "channel_recorded"
	EvChannelClosed  = "channel_closed"
	EvSessionStart   = "session_started"
	EvSessionDone    = "session_completed"
	EvSessionAborted = "session_aborted"
	EvDeliveryRetry  = "delivery_retry"
	EvDeliveryFail   = "delivery_failed"
	EvPin            = "channel_pinned"
	EvRelease        = "channel_released"
)

// Event is one immutable journal row. Detail is a kind-specific JSON object.
type Event struct {
	Seq       int64                  `json:"seq"`
	RunID     string                 `json:"run_id"`
	At        time.Time              `json:"at"`
	Kind      string                 `json:"kind"`
	NodeID    string                 `json:"node_id"`
	SessionID string                 `json:"session_id,omitempty"`
	Lamport   int64                  `json:"lamport"`
	Detail    map[string]interface{} `json:"detail,omitempty"`
}

// Store is the full data/error contract between the node kernel and storage.
//
// All mutating methods are expected to be atomic where described; failures are
// returned as *errs.Error so the HTTP layer can report a class.
type Store interface {
	NodeID() string
	RunID() string

	// Boot / recovery.
	// Bootstrap inserts initial balances only for a fresh store.
	Bootstrap(initialBalances map[string]int64) (fresh bool, err error)
	// BootstrapTopology registers known peers (incoming channels and outbox
	// counters). Idempotent.
	BootstrapTopology(peers []string) error
	// RecoverAborts marks every recording session aborted after a restart and
	// returns their ids (empty on a clean boot).
	RecoverAborts(ctx context.Context) (aborted []string, err error)
	Close() error

	// Live ledger + clock.
	Balances() map[string]int64
	AddBalance(account string, delta int64) error
	Clock() (t int64, set func(t int64))

	// Journal. AppendEvent persists before the side effect is exposed.
	AppendEvent(ctx context.Context, ev Event) (int64, error)
	Journal(ctx context.Context, sessionID string, limit int) ([]Event, error)

	// Outbox (reliable FIFO channels).
	// EnqueueOutbox persists an envelope and atomically assigns its per-channel
	// seq, which it returns. Caller leaves Seq zero. Returns
	// resource-exhausted when the per-peer cap of unacked items is reached.
	EnqueueOutbox(ctx context.Context, toPeer string, env protocol.Envelope) (seq int64, err error)
	// PendingOutbox returns due items for one peer in seq order. The node is
	// responsible for not pumping pinned channels; the store itself has no
	// notion of a gate.
	PendingOutbox(peer string) ([]OutboxItem, error)
	AckOutbox(id int64) error
	OutboxLen(peer string) (int, error)

	// Sessions / channels.
	StartSession(ctx context.Context, rec SessionRecord) error
	GetSession(sessionID string) (*SessionRecord, error)
	ListSessions() ([]*SessionRecord, error)
	SaveLocalState(ctx context.Context, sessionID string, st LocalState) error
	RecordChannelMessage(ctx context.Context, sessionID, fromPeer string, t protocol.Transfer, markerLamport int64) error
	CloseChannel(ctx context.Context, sessionID, fromPeer string, markerLamport int64) error
	AbortSession(ctx context.Context, sessionID, reason string) error
	CompleteSession(ctx context.Context, sessionID string) error
	HasSeenMessage(msgID string) (bool, error)
	RememberMessage(msgID string) error
	LastInSeq(fromPeer string) (int64, error)
	SetLastInSeq(fromPeer string, seq int64) error
}
