// Package model defines the synthetic network events that drive a replay:
// membership reports, leaves, general/group-specific queries, and explicit
// report-suppression declarations.
//
// This is a *synthetic event model*, not a wire-format implementation of
// IGMP. Events describe what a querier would observe on a LAN.
package model

// Event types.
const (
	// EventReport: a host sent a membership report for Group.
	// Gen, when present, is the query round the report answers; when
	// absent the report is treated as belonging to the current round
	// (e.g. an unsolicited join).
	EventReport = "report"
	// EventLeave: a host sent an IGMPv2 leave for Group.
	EventLeave = "leave"
	// EventGeneralQuery: the querier issued a general query on Iface.
	// This bumps the interface query round.
	EventGeneralQuery = "general_query"
	// EventGroupQuery: the querier issued a group-specific query.
	// This bumps the interface query round and the group's round.
	EventGroupQuery = "group_query"
	// EventSuppressedReport: a synthetic declaration that Member heard a
	// peer's report for Group in the current round and therefore
	// suppressed its own (RFC 2236 §3 host behaviour). The querier
	// observes nothing; the engine records it for audit only and never
	// changes timers or membership because of it.
	EventSuppressedReport = "suppressed_report"
)

// Event is one synthetic network observation. TimeMS is milliseconds since
// the replay epoch and must be non-decreasing across a scenario.
type Event struct {
	TimeMS int64  `json:"time_ms"`
	Type   string `json:"type"`
	Iface  string `json:"iface"`
	Group  string `json:"group,omitempty"`
	Member string `json:"member,omitempty"`
	// Gen is the query round this event answers. Only meaningful for
	// EventReport; nil means "current round".
	Gen *uint64 `json:"gen,omitempty"`
}

// Types lists all valid event type strings.
var Types = map[string]bool{
	EventReport:           true,
	EventLeave:            true,
	EventGeneralQuery:     true,
	EventGroupQuery:       true,
	EventSuppressedReport: true,
}
