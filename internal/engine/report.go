package engine

import (
	"sort"

	"pathvector/internal/ierr"
)

func (e *engine) buildReport(status Status, reason string) *Report {
	r := &Report{
		RunID:          e.runID,
		Status:         status,
		Reason:         reason,
		Steps:          e.step,
		Seeds:          e.totalSeeds,
		ProcessedSeeds: e.processedSeeds,
		Budget:         e.budget,
		QueueCap:       e.queueCap,
		BestRoutes:     map[string]map[string]CandidateSnap{},
		Trace:          append([]TraceEvent(nil), e.trace...),
		Cycle:          e.cycle,
	}
	pfxs := keysOf(e.prefixes)
	sort.Strings(pfxs)
	r.Prefixes = pfxs
	for _, p := range pfxs {
		r.BestRoutes[p] = map[string]CandidateSnap{}
		for _, id := range e.idx.Order() {
			if c := e.best[id][p]; c != nil {
				r.BestRoutes[p][id] = snapshotCandidate(c)
			}
		}
	}
	return r
}

// partialReport is attached when a hard error aborts the run. Status is
// not_converged and reason records the error kind so persistence layers can
// index failures by category.
func (e *engine) partialReport(err error) *Report {
	reason := string(ierr.Of(err))
	r := e.buildReport(StatusNotConverged, reason)
	r.Cycle = nil
	return r
}
