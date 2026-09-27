package engine

// Status is the final verdict of a replay.
type Status string

const (
	// StatusConverged: queue drained after the last seed.
	StatusConverged Status = "converged"
	// StatusNotConverged: budget hit or a deterministic state cycle found.
	StatusNotConverged Status = "not_converged"
)

// Reason codes attached to reports and errors. They are part of the stable
// output contract.
const (
	ReasonQuiesced         = "quiesced"
	ReasonBudgetExceeded   = "budget_exceeded"
	ReasonOscillation      = "oscillation_detected"
	ReasonQueueCapExceeded = "queue_cap_exceeded"
	ReasonUnknownWithdraw  = "unknown_withdraw"
)

// CandidateSnap is the recorded view of a RIB-In candidate.
type CandidateSnap struct {
	FromPeer    string `json:"from_peer"`
	NextHop     string `json:"next_hop"`
	LocalPref   uint32 `json:"local_pref"`
	ASPath      []int  `json:"as_path"`
	MED         uint32 `json:"med"`
	Origin      string `json:"origin"`
	LearnedIBGP bool   `json:"learned_ibgp"`
}

// MessageSnap records one queued internal UPDATE.
type MessageSnap struct {
	Kind      string `json:"kind"`
	Prefix    string `json:"prefix"`
	From      string `json:"from"`
	To        string `json:"to"`
	LocalPref uint32 `json:"local_pref,omitempty"`
	MED       uint32 `json:"med,omitempty"`
	ASPath    []int  `json:"as_path,omitempty"`
}

// TraceEvent records one processing step with the evidence needed to
// replay the decision: the input, each filter outcome, the post-decision
// candidate set, the chosen best and the decisive comparison reason.
type TraceEvent struct {
	Step       int             `json:"step"`
	Phase      string          `json:"phase"` // seed | propagate
	Kind       string          `json:"kind"`  // announce | withdraw
	Router     string          `json:"router"`
	From       string          `json:"from,omitempty"`
	Prefix     string          `json:"prefix"`
	Outcome    string          `json:"outcome"`
	Rule       string          `json:"rule,omitempty"`
	Reason     string          `json:"reason,omitempty"`
	Candidates []CandidateSnap `json:"candidates,omitempty"`
	Best       *CandidateSnap  `json:"best,omitempty"`
	Queued     []MessageSnap   `json:"queued,omitempty"`
	// DeniedExports records "neighbor:rule" for neighbors to which the
	// best path was withheld by an export deny rule on this step.
	DeniedExports []string `json:"denied_exports,omitempty"`
}

// StatePoint is one sampled global-state point on a cycle walk.
type StatePoint struct {
	Step      int    `json:"step"`
	Signature string `json:"signature"`
}

// CycleEvidence is the proof of non-convergence returned when the same
// (best-path state, queued UPDATE sequence) recurs.
type CycleEvidence struct {
	FirstStep  int          `json:"first_step"`
	SecondStep int          `json:"second_step"`
	Signature  string       `json:"signature"`
	Walk       []StatePoint `json:"walk"`
}

// Report is the complete replay result.
type Report struct {
	RunID          string                              `json:"run_id"`
	Status         Status                              `json:"status"`
	Reason         string                              `json:"reason"`
	Steps          int                                 `json:"steps"`
	Seeds          int                                 `json:"seeds"`
	ProcessedSeeds int                                 `json:"processed_seeds"`
	Budget         int                                 `json:"budget"`
	QueueCap       int                                 `json:"queue_cap"`
	Prefixes       []string                            `json:"prefixes"`
	BestRoutes     map[string]map[string]CandidateSnap `json:"best_routes"`
	Trace          []TraceEvent                        `json:"trace"`
	Cycle          *CycleEvidence                      `json:"cycle,omitempty"`
}
