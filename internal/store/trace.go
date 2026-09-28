package store

import "time"

// Trace is one correlated diagnostic step recorded while processing a request.
type Trace struct {
	RequestID  string
	Method     string
	Path       string
	MemberID   string
	Generation int64
	Step       string
	Version    int64
	Location   string // package/function where the step occurred
	OK         bool
	FailCode   string
	FailDetail string
	Uncertain  bool
	Extra      map[string]any
}

// TraceRow is a persisted trace as returned by the diagnostics endpoint.
type TraceRow struct {
	ID         int64     `json:"id"`
	At         time.Time `json:"at"`
	RequestID  string    `json:"request_id"`
	Method     string    `json:"method"`
	Path       string    `json:"path"`
	MemberID   string    `json:"member_id"`
	Generation int64     `json:"generation"`
	Step       string    `json:"step"`
	Version    int64     `json:"version"`
	Location   string    `json:"location"`
	OK         bool      `json:"ok"`
	FailCode   string    `json:"fail_code"`
	FailDetail string    `json:"fail_detail"`
	Uncertain  bool      `json:"uncertain"`
	ExtraJSON  string    `json:"extra_json"`
}
