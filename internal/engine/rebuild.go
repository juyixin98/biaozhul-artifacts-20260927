package engine

import (
	"fmt"

	"igmpv2timer/internal/clock"
	"igmpv2timer/internal/config"
	"igmpv2timer/internal/core"
	"igmpv2timer/internal/model"
	"igmpv2timer/internal/store"
)

// RebuildResult compares the live replay against a fresh core reconstructed
// solely from the SQLite event journal.
type RebuildResult struct {
	OK               bool                `json:"ok"`
	GroupsMatch      bool                `json:"groups_match"`
	IntervalsMatch   bool                `json:"intervals_match"`
	RebuiltSnapshot  model.StateSnapshot `json:"rebuilt_snapshot"`
	LiveSnapshot     model.StateSnapshot `json:"live_snapshot"`
	RebuiltIntervals []model.Interval    `json:"rebuilt_intervals"`
	Detail           string              `json:"detail,omitempty"`
}

// RebuildFromJournal reconstructs state from journaled input events only
// (no fixture scheduling) and compares it with the live final state.
// Report/leave events carry their virtual timestamp; the clock is advanced
// to each, so every membership/last-member timer is reproduced. `until`
// (the scenario horizon) drives the final timer sweep.
func RebuildFromJournal(cfg config.Config, st *store.Store, until model.Millis,
	live *Report) (*RebuildResult, error) {
	events, err := st.Events()
	if err != nil {
		return nil, err
	}
	clk := clock.New()
	c, err := core.New(cfg, clk)
	if err != nil {
		return nil, err
	}
	for _, ev := range events {
		if err := clk.Advance(ev.At); err != nil {
			return nil, fmt.Errorf("journal event seq=%d: %w", ev.Seq, err)
		}
		// advance timers up to this event; emitted packets are irrelevant
		// to membership state (queries never mutate membership), but
		// timeouts/LMQ deletions must fire.
		if _, _, err := c.Tick(ev.At); err != nil {
			return nil, err
		}
		c.Apply(ev)
	}
	// final timer sweep at the scenario horizon
	if _, _, err := c.Tick(until); err != nil {
		return nil, err
	}

	res := &RebuildResult{
		RebuiltSnapshot:  c.Snapshot(),
		LiveSnapshot:     live.FinalSnapshot,
		RebuiltIntervals: c.Intervals(),
	}
	res.GroupsMatch = snapshotsEqual(res.RebuiltSnapshot, res.LiveSnapshot)
	res.IntervalsMatch = intervalsEqual(res.RebuiltIntervals, live.Intervals)
	res.OK = res.GroupsMatch && res.IntervalsMatch
	if !res.OK {
		res.Detail = "journal rebuild diverged from live replay"
	}
	return res, nil
}

func snapshotsEqual(a, b model.StateSnapshot) bool {
	if len(a.Groups) != len(b.Groups) {
		return false
	}
	ga := map[string]model.GroupSnapshot{}
	for _, g := range a.Groups {
		ga[g.Iface+"\x00"+g.Group] = g
	}
	for _, gb := range b.Groups {
		x, ok := ga[gb.Iface+"\x00"+gb.Group]
		if !ok {
			return false
		}
		if x.MembershipDeadline != gb.MembershipDeadline ||
			x.LMQActive != gb.LMQActive || x.LMQSent != gb.LMQSent {
			return false
		}
		if len(x.Members) != len(gb.Members) {
			return false
		}
		for i := range x.Members {
			if x.Members[i] != gb.Members[i] {
				return false
			}
		}
	}
	return true
}

func intervalsEqual(a, b []model.Interval) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
