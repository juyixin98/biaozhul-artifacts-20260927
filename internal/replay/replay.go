// Package replay rebuilds the in-memory routing state purely from the
// append-only SQLite event log, and verifies that reconstructed bucket
// assignments match the assignments persisted at each version. It is the
// mechanism that makes "replay the problem from a run id" possible without
// any additional state.
package replay

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"

	"flexhash/internal/fherr"
	"flexhash/internal/hashring"
	"flexhash/internal/store"
)

// ConfigPayload mirrors store.SaveConfig's event payload.
type ConfigPayload struct {
	Version     int64             `json:"version"`
	BucketCount int               `json:"bucket_count"`
	Members     []hashring.Member `json:"members"`
}

// HealthPayload mirrors store.SaveHealth's event payload.
type HealthPayload struct {
	Revision int64  `json:"revision"`
	Member   string `json:"member"`
	Healthy  bool   `json:"healthy"`
}

// VersionState captures one checkpoint during replay.
type VersionState struct {
	ConfigVersion int64
	HealthRev     int64
	Quota         map[string]int
}

// Report is the outcome of a full log replay.
type Report struct {
	EventsReplayed int
	FinalVersion   int64
	FinalHealthRev int64
	BucketCount    int
	States         []VersionState
	// Mismatches lists versions whose recomputed assignment diverged from
	// the persisted assignment (a computation-failure class integrity
	// problem). Empty means a clean replay.
	Mismatches []string
}

// replay applies every event in log order against a fresh Manager. When
// verify is true, each config checkpoint is compared with the assignment
// rows persisted for that version and divergences are recorded.
func replay(ctx context.Context, st *store.Store, verify bool) (*hashring.Manager, *Report, error) {
	const op = "replay.replay"
	events, err := st.Events(ctx)
	if err != nil {
		return nil, nil, err
	}
	if len(events) == 0 {
		return nil, nil, fherr.New(fherr.KindStateConflict, op, "event log is empty: nothing to replay")
	}

	var mgr *hashring.Manager
	rep := &Report{}

	for _, ev := range events {
		switch ev.Type {
		case store.EvConfig:
			var p ConfigPayload
			if err := json.Unmarshal(ev.PayloadJSON, &p); err != nil {
				return nil, nil, fherr.Wrap(fherr.KindComputationFailed, op,
					fmt.Sprintf("seq %d: bad config payload", ev.Seq), err)
			}
			var ring *hashring.Ring
			if mgr == nil {
				if p.Version != 1 {
					return nil, nil, fherr.New(fherr.KindComputationFailed, op,
						fmt.Sprintf("first event version is %d, want 1", p.Version))
				}
				mgr = hashring.NewManager(p.BucketCount)
				ring, _, err = mgr.Bootstrap(1, p.Members, 0)
				if err != nil {
					return nil, nil, fherr.Wrap(fherr.KindComputationFailed, op,
						fmt.Sprintf("seq %d: bootstrap", ev.Seq), err)
				}
			} else {
				ring, _, err = mgr.ApplyConfig(p.Version, p.Members)
				if err != nil {
					return nil, nil, fherr.Wrap(fherr.KindComputationFailed, op,
						fmt.Sprintf("seq %d: apply version %d", ev.Seq, p.Version), err)
				}
			}
			if verify {
				if err := checkVersion(ctx, op, st, p.Version, ring, rep); err != nil {
					return nil, nil, err
				}
			}
			_, hrev := mgr.Current()
			rep.States = append(rep.States, VersionState{
				ConfigVersion: p.Version,
				HealthRev:     hrev,
				Quota:         ring.Quota(),
			})
			rep.FinalVersion = p.Version
			rep.BucketCount = p.BucketCount
		case store.EvHealth:
			var p HealthPayload
			if err := json.Unmarshal(ev.PayloadJSON, &p); err != nil {
				return nil, nil, fherr.Wrap(fherr.KindComputationFailed, op,
					fmt.Sprintf("seq %d: bad health payload", ev.Seq), err)
			}
			if mgr == nil {
				return nil, nil, fherr.New(fherr.KindComputationFailed, op,
					fmt.Sprintf("seq %d: health event before any config", ev.Seq))
			}
			if err := mgr.RestoreHealth(p.Member, p.Healthy, p.Revision); err != nil {
				return nil, nil, fherr.Wrap(fherr.KindComputationFailed, op,
					fmt.Sprintf("seq %d: restore health rev %d", ev.Seq, p.Revision), err)
			}
			rep.FinalHealthRev = p.Revision
		default:
			return nil, nil, fherr.New(fherr.KindComputationFailed, op,
				fmt.Sprintf("seq %d: unknown event kind %q", ev.Seq, ev.Type))
		}
	}
	rep.EventsReplayed = len(events)
	return mgr, rep, nil
}

func checkVersion(ctx context.Context, op string, st *store.Store, version int64,
	ring *hashring.Ring, rep *Report) error {
	rows, err := st.AssignmentsAt(ctx, version)
	if err != nil {
		return fherr.Wrap(fherr.KindComputationFailed, op,
			fmt.Sprintf("version %d: cannot load persisted assignment", version), err)
	}
	if len(rows) != ring.BucketCount {
		rep.Mismatches = append(rep.Mismatches, fmt.Sprintf(
			"version %d: persisted rows %d != bucket count %d",
			version, len(rows), ring.BucketCount))
		return nil
	}
	diff := 0
	examples := make([]string, 0)
	for _, a := range rows {
		got := ring.Owner(a.Bucket)
		if got != a.Member {
			diff++
			if len(examples) < 5 {
				examples = append(examples, fmt.Sprintf(
					"bucket %d: stored=%s recomputed=%s", a.Bucket, a.Member, got))
			}
		}
	}
	if diff > 0 {
		sort.Strings(examples)
		rep.Mismatches = append(rep.Mismatches, fmt.Sprintf(
			"version %d: %d/%d buckets differ (%s)",
			version, diff, ring.BucketCount, joinExamples(examples)))
	}
	return nil
}

// Rebuild applies the full event log and returns the reconstructed manager
// and checkpoint report (without cross-checking persisted assignment rows).
func Rebuild(ctx context.Context, st *store.Store) (*hashring.Manager, *Report, error) {
	return replay(ctx, st, false)
}

// VerifyAssignments replays the log and compares recomputed owners against
// the persisted assignments table at every config version. Divergences are
// reported, not swallowed: a non-empty Report.Mismatches is an integrity
// failure.
func VerifyAssignments(ctx context.Context, st *store.Store) (*hashring.Manager, *Report, error) {
	return replay(ctx, st, true)
}

func joinExamples(ss []string) string {
	out := ""
	for i, s := range ss {
		if i > 0 {
			out += "; "
		}
		out += s
	}
	return out
}
