package protocol

import (
	"bytes"
	"encoding/json"
	"fmt"
)

var eventDetailRegistry = map[EventType]func() any{
	EvGroupCreated:        func() any { return &GroupCreatedDetail{} },
	EvMemberJoined:        func() any { return &MemberJoinedDetail{} },
	EvMemberLeft:          func() any { return &MemberLeftDetail{} },
	EvMemberExpired:       func() any { return &MemberExpiredDetail{} },
	EvHeartbeat:           func() any { return &HeartbeatDetail{} },
	EvPrepareRebalance:    func() any { return &PrepareRebalanceDetail{} },
	EvRevokeIssued:        func() any { return &RevokeIssuedDetail{} },
	EvRevokeAcked:         func() any { return &RevokeAckedDetail{} },
	EvRevokeQuarantined:   func() any { return &RevokeQuarantinedDetail{} },
	EvPartitionForceFreed: func() any { return &PartitionForceFreedDetail{} },
	EvGenerationActivated: func() any { return &GenerationActivatedDetail{} },
	EvOffsetCommitted:     func() any { return &OffsetCommittedDetail{} },
}

type wireEvent struct {
	Seq       Seq             `json:"seq"`
	Group     string          `json:"group"`
	Type      EventType       `json:"type"`
	At        json.RawMessage `json:"at"`
	RequestID string          `json:"request_id"`
	Detail    json.RawMessage `json:"detail,omitempty"`
}

// MarshalEvent serializes an event with its concrete detail payload.
func MarshalEvent(e Event) ([]byte, error) {
	var w wireEvent
	var b bytes.Buffer
	enc := json.NewEncoder(&b)
	enc.SetEscapeHTML(false)
	raw, err := json.Marshal(e.At)
	if err != nil {
		return nil, err
	}
	w.At = raw
	if e.Detail != nil {
		raw, err := json.Marshal(e.Detail)
		if err != nil {
			return nil, err
		}
		w.Detail = raw
	}
	w.Seq, w.Group, w.Type, w.RequestID = e.Seq, e.Group, e.Type, e.RequestID
	if err := enc.Encode(w); err != nil {
		return nil, err
	}
	return bytes.TrimRight(b.Bytes(), "\n"), nil
}

// UnmarshalEvent decodes an event, resolving Detail to the registered struct
// for its type.
func UnmarshalEvent(data []byte) (Event, error) {
	var w wireEvent
	if err := json.Unmarshal(data, &w); err != nil {
		return Event{}, err
	}
	e := Event{Seq: w.Seq, Group: w.Group, Type: w.Type, RequestID: w.RequestID}
	if len(w.At) > 0 {
		if err := json.Unmarshal(w.At, &e.At); err != nil {
			return Event{}, fmt.Errorf("protocol: event %d: bad time: %w", e.Seq, err)
		}
	}
	newDetail, ok := eventDetailRegistry[e.Type]
	if !ok {
		return Event{}, fmt.Errorf("protocol: event %d: unknown event type %q", e.Seq, e.Type)
	}
	detail := newDetail()
	if len(w.Detail) > 0 {
		if err := json.Unmarshal(w.Detail, detail); err != nil {
			return Event{}, fmt.Errorf("protocol: event %d: bad detail: %w", e.Seq, err)
		}
	}
	e.Detail = detail
	return e, nil
}
