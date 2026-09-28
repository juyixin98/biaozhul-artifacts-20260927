// Package model defines the shared wire/domain types of the offline
// IGMPv2 membership-timer service.
//
// Scope note: this is an offline simulation of the *host-facing membership
// timers* described in RFC 2236 (IGMPv2) — report suppression, group
// membership interval and last-member query handling. It is NOT a multicast
// routing protocol: there is no querier election, no PIM interface and no
// packet I/O. Everything here is driven from synthetic fixtures.
package model

import (
	"fmt"
	"net/netip"
	"strings"
	"time"
)

// PacketType identifies an IGMPv2 message observed on an interface.
type PacketType string

const (
	PktQueryGeneral PacketType = "QUERY_GENERAL" // type 0x11, G-A 0.0.0.0
	PktQueryGroup   PacketType = "QUERY_GROUP"   // type 0x11, G-A = group
	PktReportV2     PacketType = "REPORT_V2"     // type 0x16 Membership Report
	PktLeave        PacketType = "LEAVE"         // type 0x17 Leave Group
)

// EventKind is the kind of input action a replay script can carry.
type EventKind string

const (
	EvReport     EventKind = "report"      // a host Membership Report arrives
	EvLeave      EventKind = "leave"       // a host Leave Group arrives
	EvForceQuery EventKind = "force_query" // script injects a query (G/GSQ)
	EvAdvance    EventKind = "advance"     // advance the clock without a packet
	EvCheckpoint EventKind = "checkpoint"  // no state effect, assertion anchor
)

// Verdict is the per-event accept/reject/other classification produced by
// the state core. Independent tests assert on these exact categories.
type Verdict string

const (
	// VAccepted: report refreshed/created membership, or leave started LMQ.
	VAccepted Verdict = "ACCEPTED"
	// VSuppressed: a member's scheduled report was cancelled by an earlier
	// report (IGMPv2 §3 report suppression, host-side behavior emulated
	// by the replay harness).
	VSuppressed Verdict = "SUPPRESSED"
	// VStale: event refers to an older/superseded query round; it must not
	// overwrite state of the newer generation.
	VStale Verdict = "STALE"
	// VRejected: malformed or illegal packet (bad group, bad source, leave
	// for a group with no members, ...).
	VRejected Verdict = "REJECTED"
	// VTimeout: no report was seen within the Group Membership Interval;
	// the forwarding entry for the group is removed.
	VTimeout Verdict = "TIMEOUT"
	// VUndecidable: the event references state that does not exist in this
	// replay (e.g. a future query generation), so no decision is possible.
	VUndecidable Verdict = "UNDECIDABLE"
	// VDropped: the router emitted a query but the synthetic transit layer
	// deliberately dropped it (fixture-controlled "query loss").
	VDropped Verdict = "DROPPED"
)

// Reason codes attached to diagnostics; they are part of the contract the
// independent tests assert on.
const (
	ReasonMembershipRefreshed = "membership_refreshed"
	ReasonMembershipCreated   = "membership_created"
	ReasonReportDuringLMQ     = "report_during_lmq"
	ReasonReportSuppressed    = "report_suppressed"
	ReasonStaleRound          = "stale_query_round"
	ReasonGenerationUnknown   = "response_to_unknown_generation"
	ReasonBadGroupAddr        = "bad_group_address"
	ReasonBadSourceAddr       = "bad_source_address"
	ReasonLeaveNoGroup        = "leave_unknown_group"
	ReasonLeaveUnknownMember  = "leave_unknown_member"
	ReasonQueryDropped        = "query_lost_in_transit"
	ReasonMembershipTimeout   = "membership_interval_expired"
	ReasonLastMemberConfirmed = "last_member_query_confirmed"
	ReasonLastMemberStarted   = "last_member_query_started"
	ReasonLastMemberCancelled = "last_member_query_cancelled"
	ReasonGeneralQueryEmitted = "general_query_emitted"
	ReasonGroupQueryEmitted   = "group_specific_query_emitted"
)

// Millis is simulation time in milliseconds since the replay epoch (0).
type Millis int64

// Duration returns the value as a time.Duration (millisecond resolution is
// all the simulator uses).
func (m Millis) Duration() time.Duration { return time.Duration(m) * time.Millisecond }

// Event is a single input consumed by the state core.
type Event struct {
	Seq        int64     `json:"seq"`
	At         Millis    `json:"at_ms"`
	Kind       EventKind `json:"kind"`
	Iface      string    `json:"iface,omitempty"`
	Group      string    `json:"group,omitempty"`
	Member     string    `json:"member,omitempty"`      // human-readable member name (scripts)
	SourceAddr string    `json:"source_addr,omitempty"` // actual IP used on the wire
	// ResponseTo, for a report, is the query generation the host is
	// answering. Empty means an unsolicited report.
	ResponseTo string `json:"response_to,omitempty"`
	RequestID  string `json:"request_id,omitempty"` // injected correlation id
}

// QueryType reports which kind of query a force_query event represents.
func (e Event) QueryType() PacketType {
	if strings.TrimSpace(e.Group) == "" {
		return PktQueryGeneral
	}
	return PktQueryGroup
}

// Diag is the diagnostic record explaining one accept/reject decision.
type Diag struct {
	RequestID string     `json:"request_id"`
	Seq       int64      `json:"seq"`
	At        Millis     `json:"at_ms"`
	Iface     string     `json:"iface,omitempty"`
	Group     string     `json:"group,omitempty"`
	Member    string     `json:"member,omitempty"`
	Packet    PacketType `json:"packet,omitempty"`
	Verdict   Verdict    `json:"verdict"`
	Reason    string     `json:"reason"`
	Detail    string     `json:"detail,omitempty"`
	// MembershipDeadline is the group membership deadline *after* applying
	// the event (0 if the group is absent). Key timer state for diagnostics.
	MembershipDeadline Millis `json:"membership_deadline_ms,omitempty"`
	// GenActive/GenApplied are query generations: the round currently open
	// when the event arrived, and the round the event claimed to answer.
	GenActive  int64 `json:"gen_active,omitempty"`
	GenApplied int64 `json:"gen_applied,omitempty"`
	// Members is the member set on (iface, group) after the event.
	Members []string `json:"members,omitempty"`
}

// EmittedPkt records a packet the *router side* emits (periodic/general or
// group-specific queries), with its generation tag.
type EmittedPkt struct {
	At        Millis     `json:"at_ms"`
	Iface     string     `json:"iface"`
	Group     string     `json:"group,omitempty"` // empty = general query
	Packet    PacketType `json:"packet"`
	Gen       int64      `json:"gen"`
	RequestID string     `json:"request_id,omitempty"`
}

// GroupSnapshot is the externally visible state of one (interface, group).
type GroupSnapshot struct {
	Iface              string   `json:"iface"`
	Group              string   `json:"group"`
	Present            bool     `json:"present"`
	Members            []string `json:"members"`
	MembershipDeadline Millis   `json:"membership_deadline_ms"`
	// LMQActive means a last-member-query sequence is running.
	LMQActive bool `json:"lmq_active"`
	// LMQSent is how many group-specific queries the sequence has sent.
	LMQSent int `json:"lmq_sent"`
	// LMQDeadline is when the group is deleted if no report arrives.
	LMQDeadline Millis `json:"lmq_deadline_ms,omitempty"`
	// DeleteGen is the generation guard on the LMQ deletion: a report
	// belonging to a later membership generation invalidates it.
	DeleteGen int64 `json:"delete_gen,omitempty"`
}

// StateSnapshot is the full membership/forwarding table at one instant.
type StateSnapshot struct {
	At     Millis          `json:"at_ms"`
	Groups []GroupSnapshot `json:"groups"`
}

// Interval records how long a forwarding entry for (iface, group) was
// continuously present — the "转发表保留区间" the tests verify.
type Interval struct {
	Iface  string `json:"iface"`
	Group  string `json:"group"`
	Start  Millis `json:"start_ms"`
	End    Millis `json:"end_ms"` // 0 while still present
	Reason string `json:"reason"` // why it ended
}

// ValidateGroupAddr applies RFC 2236 / multicast address sanity rules used
// by the synthetic network model. Returns the parsed address or an error.
//
// Rejected: non-multicast addresses, and 224.0.0.x link-local control
// groups (224.0.0.1 all-hosts is never a membership group).
func ValidateGroupAddr(s string) (netip.Addr, error) {
	a, err := netip.ParseAddr(s)
	if err != nil {
		return netip.Addr{}, fmt.Errorf("group %q: %w", s, err)
	}
	if !a.Is4() {
		return netip.Addr{}, fmt.Errorf("group %q: IPv4 required", s)
	}
	if !a.IsMulticast() {
		return netip.Addr{}, fmt.Errorf("group %s: not a multicast address", s)
	}
	// 224.0.0.0/24 is reserved local network control block.
	p := a.As4()
	if p[0] == 224 && p[1] == 0 && p[2] == 0 {
		return netip.Addr{}, fmt.Errorf("group %s: 224.0.0.0/24 control block is not a membership group", s)
	}
	return a, nil
}

// ValidateSourceAddr validates a host source address: a unicast IPv4 address
// (unspecified/broadcast/multicast are rejected).
func ValidateSourceAddr(s string) (netip.Addr, error) {
	if strings.TrimSpace(s) == "" {
		return netip.Addr{}, fmt.Errorf("empty source address")
	}
	a, err := netip.ParseAddr(s)
	if err != nil {
		return netip.Addr{}, fmt.Errorf("source %q: %w", s, err)
	}
	if !a.Is4() {
		return netip.Addr{}, fmt.Errorf("source %s: IPv4 required", s)
	}
	if a.IsMulticast() || a.IsUnspecified() || !a.IsGlobalUnicast() {
		// IsGlobalUnicast accepts private ranges (fine for synthetic LAN),
		// rejects 0.0.0.0 and 224.0.0.0/4.
		return netip.Addr{}, fmt.Errorf("source %s: not a usable unicast address", s)
	}
	return a, nil
}

// MaskAddr redacts the last octet of an IPv4 address for diagnostic output
// ("192.0.2.11" -> "192.0.2.x"). Multicast group addresses are printed in
// full: they are not host-identifying.
func MaskAddr(s string) string {
	a, err := netip.ParseAddr(s)
	if err != nil || !a.Is4() {
		if s != "" {
			return "***"
		}
		return ""
	}
	p := a.As4()
	return fmt.Sprintf("%d.%d.%d.x", p[0], p[1], p[2])
}
