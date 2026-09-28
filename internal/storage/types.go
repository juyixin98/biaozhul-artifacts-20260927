// Package storage persists leases, OFFER reservations, transaction records and
// decision events in SQLite. All state transitions that allocate or commit an
// address run inside a single SQL transaction so concurrent contenders cannot
// obtain the same address.
package storage

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
)

// Identity is the canonical client identity used throughout the state
// machine. Per RFC 2131 §4.4.1 the client-id option (61) is authoritative;
// when absent the chaddr is the identity key.
type Identity struct {
	// Kind is "client-id" or "chaddr".
	Kind string
	// Key is the lowercase hex of the identifier bytes.
	Key string
	// Label is a short human-readable form (e.g. first bytes).
	Label string
}

// IdentityFromClientID builds an identity from option 61 bytes.
func IdentityFromClientID(raw []byte) Identity {
	sum := sha256.Sum256(raw)
	return Identity{
		Kind:  "client-id",
		Key:   "cid:" + hex.EncodeToString(sum[:16]),
		Label: "cid:" + hex.EncodeToString(raw),
	}
}

// IdentityFromCHAddr builds an identity from the hardware address fallback.
func IdentityFromCHAddr(mac []byte) Identity {
	return Identity{
		Kind:  "chaddr",
		Key:   "mac:" + hex.EncodeToString(mac),
		Label: "mac:" + hex.EncodeToString(mac),
	}
}

func (i Identity) String() string { return i.Kind + ":" + i.Label }

// LeaseState is the state-machine status of one address record.
type LeaseState string

const (
	StateOffered  LeaseState = "OFFERED"  // address held by an OFFER, not leased
	StateBound    LeaseState = "BOUND"    // committed by an ACK
	StateReleased LeaseState = "RELEASED" // client sent RELEASE
	StateExpired  LeaseState = "EXPIRED"  // lifetime elapsed / offer timed out
)

// IsActive reports whether the record occupies the address.
func (s LeaseState) IsActive() bool { return s == StateOffered || s == StateBound }

// Lease is one row of the leases table.
type Lease struct {
	ID            int64      `json:"id"`
	IP            string     `json:"ip"`
	IdentityID    string     `json:"identityId"`
	IdentityLabel string     `json:"identityLabel"`
	State         LeaseState `json:"state"`
	XID           uint32     `json:"xid"`
	// OfferedAt is when the OFFER reservation was (re)made.
	OfferedAt int64 `json:"offeredAt"`
	OfferExp  int64 `json:"offerExp"`
	// BoundAt/Starts/Ends describe the committed lease; zero when OFFERED.
	BoundAt int64 `json:"boundAt"`
	Starts  int64 `json:"starts"`
	Ends    int64 `json:"ends"`
	// RenewCount counts successful renew/rebind extensions.
	RenewCount int   `json:"renewCount"`
	UpdatedAt  int64 `json:"updatedAt"`
}

// Event is one structured decision record (also rendered to the log).
type Event struct {
	ID         int64  `json:"id"`
	RunID      string `json:"runId"`
	TsNanos    int64  `json:"tsNanos"`
	XID        uint32 `json:"xid"`
	IdentityID string `json:"identityId,omitempty"`
	MAC        string `json:"mac,omitempty"`
	RemoteAddr string `json:"remoteAddr,omitempty"`
	InType     string `json:"inType"`
	OutType    string `json:"outType,omitempty"`
	Action     string `json:"action"`
	Result     string `json:"result"`
	Reason     string `json:"reason"`
	AssignedIP string `json:"assignedIp,omitempty"`
	Detail     string `json:"detail,omitempty"`
}

// FailureCategory distinguishes protocol-level outcomes for diagnostics. It
// never collapses abnormal inputs into success.
type FailureCategory string

const (
	// CatOK: normal processing (including a protocol NAK, which is a valid
	// reply but must still be distinguishable in metrics).
	CatOK          FailureCategory = "ok"
	CatNAK         FailureCategory = "nak"
	CatNoReply     FailureCategory = "no_reply"
	CatMalformed   FailureCategory = "malformed_packet"
	CatUnsupported FailureCategory = "unsupported_message"
	CatPoolFull    FailureCategory = "pool_exhausted"
	CatContend     FailureCategory = "address_contention"
	CatInternal    FailureCategory = "internal_error"
)

func (c FailureCategory) String() string { return string(c) }

// ErrLeaseGone is returned by optimistic transactions when a concurrent
// committer won the address; the caller maps it to contention/NAK.
var ErrLeaseGone = fmt.Errorf("lease row changed concurrently")
