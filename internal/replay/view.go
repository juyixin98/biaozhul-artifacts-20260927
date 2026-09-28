// Package replay maps kernel State and the persisted event log to the public
// protocol views. It is the read/introspection side: nothing here mutates state.
package replay

import (
	"encoding/json"

	"opp291/coordinator/internal/kernel"
	"opp291/coordinator/internal/protocol"
	"opp291/coordinator/internal/store"
)

// GroupView renders the full materialised state.
func GroupView(s *kernel.State) protocol.GroupStateResponse {
	out := protocol.GroupStateResponse{
		GroupID:        s.GroupID,
		Generation:     s.Generation,
		Phase:          string(s.Phase),
		Version:        s.Version,
		PartitionCount: s.PartitionCount,
	}
	for _, id := range s.ActiveMembers() {
		m := s.Members[id]
		out.Members = append(out.Members, protocol.MemberView{
			MemberID: m.ID, Generation: m.Generation,
			JoinedAt: m.JoinedAt, LastSeen: m.LastSeen, Active: m.Active,
		})
	}
	for _, p := range s.Partitions {
		view := protocol.PartitionStateView{
			Partition: p.ID, Owner: p.Owner, Generation: p.Generation, Phase: string(p.Phase),
			PrevOwner: p.PrevOwner, PrevGeneration: p.PrevGeneration,
			PendingOwner: p.PendingOwner, Offset: p.Offset,
		}
		out.Partitions = append(out.Partitions, view)
		if p.Uncertain {
			out.Uncertain = append(out.Uncertain, p.ID)
		}
	}
	return out
}

// AssignmentView renders one member's current valid assignment.
func AssignmentView(s *kernel.State, memberID string) protocol.AssignmentResponse {
	m, ok := s.Members[memberID]
	gen := int64(0)
	if ok {
		gen = m.Generation
	}
	out := protocol.AssignmentResponse{
		MemberID: memberID, Generation: gen, GroupPhase: string(s.Phase),
	}
	for _, p := range s.Partitions {
		if p.Phase == kernel.PStable && p.Owner == memberID {
			out.Partitions = append(out.Partitions, protocol.PartitionAssignment{
				Partition: p.ID, Owner: p.Owner, Generation: p.Generation, Phase: string(p.Phase),
			})
		}
	}
	return out
}

// EventView renders persisted kernel events for the replay endpoint.
func EventView(groupID string, evs []kernel.Event) protocol.ReplayResponse {
	out := protocol.ReplayResponse{GroupID: groupID}
	for _, e := range evs {
		detail, _ := json.Marshal(eventDetail(e))
		out.Events = append(out.Events, protocol.Event{
			Seq:       e.Version,
			GroupID:   groupID,
			Type:      string(e.Type),
			Version:   e.Version,
			Timestamp: e.At,
			RequestID: e.RequestID,
			Detail:    detail,
		})
	}
	return out
}

func eventDetail(e kernel.Event) map[string]any {
	d := map[string]any{"generation": e.Generation}
	if e.Force {
		d["force"] = true
	}
	switch e.Type {
	case kernel.EvGroupCreated:
		d["partition_count"] = e.GroupCount
		d["session_timeout_ms"] = e.SessionTimeout.Milliseconds()
	case kernel.EvMemberJoined, kernel.EvMemberLeft, kernel.EvMemberExpired:
		d["member_id"] = e.MemberID
	case kernel.EvHeartbeat:
		d["member_id"] = e.MemberID
	case kernel.EvRebalanceStarted:
		d["plan"] = e.Plan
	case kernel.EvRevocationDemanded:
		d["demanded"] = e.Demanded
	case kernel.EvPartitionRevoked:
		d["revoke"] = e.Revoke
	case kernel.EvPartitionGranted:
		d["grant"] = e.Grant
	case kernel.EvOffsetCommitted:
		d["partition"] = e.CommitPartition
		d["offset"] = e.CommitOffset
		d["owner"] = e.CommitOwner
	}
	return d
}

// TraceView converts stored trace rows to protocol trace events.
func TraceView(rows []store.TraceRow) []protocol.TraceEvent {
	out := make([]protocol.TraceEvent, 0, len(rows))
	for _, r := range rows {
		t := protocol.TraceEvent{
			Time: r.At, RequestID: r.RequestID, Method: r.Method, Path: r.Path,
			MemberID: r.MemberID, Generation: r.Generation, Step: r.Step,
			Version: r.Version, Location: r.Location, OK: r.OK,
			FailCode: protocol.FailureCode(r.FailCode), FailDetail: r.FailDetail,
			Uncertain: r.Uncertain,
		}
		if r.ExtraJSON != "" {
			var ex map[string]any
			if json.Unmarshal([]byte(r.ExtraJSON), &ex) == nil {
				t.Extra = ex
			}
		}
		out = append(out, t)
	}
	return out
}
