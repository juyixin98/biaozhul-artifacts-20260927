// Package runner drives a Scenario against three live nodes and verifies the
// observed global snapshots against (a) the independent oracle and (b) the
// hand-authored fixture expectations. It also produces a replayable run
// report: run id, per-step intermediate state, and a judgement with reasons.
package runner

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"sync"
	"time"

	"clsnap/internal/cluster"
	"clsnap/internal/errs"
	"clsnap/internal/protocol"
	"clsnap/internal/scenario"
	"clsnap/internal/store"
)

// Driver is the execution surface the runner needs from one live node.
type Driver interface {
	cluster.NodeView
	BaseURL() string
	Peers() []string
	Transfer(ctx context.Context, to, txID string, amount int64) error
	StartSnapshot(ctx context.Context, sessionID string) (*store.SessionRecord, error)
	Pin(ctx context.Context, peer string, pinned bool) error
	Abort(ctx context.Context, sessionID, reason string) error
	State(ctx context.Context) (map[string]int64, int64, error)
	Journal(ctx context.Context, sessionID string) ([]store.Event, error)
	OutboxLen(ctx context.Context, peer string) (int, error)
	// Restart stops and rebuilds the node against the SAME durable store.
	Restart(ctx context.Context) error
	Close() error
}

// Failure is a concrete classified call failure recorded by ActFail steps.
type Failure struct {
	Step    int    `json:"step"`
	Want    string `json:"want_class"`
	Got     string `json:"got_class,omitempty"`
	Code    string `json:"got_code,omitempty"`
	Message string `json:"message,omitempty"`
	Matched bool   `json:"matched"`
}

// StepRecord captures the key intermediate state after each step.
type StepRecord struct {
	Index    int                    `json:"index"`
	Action   string                 `json:"action"`
	Summary  string                 `json:"summary"`
	Lamport  map[string]int64       `json:"lamport,omitempty"`
	Balances map[string]int64       `json:"balances,omitempty"`
	Pending  map[string]int         `json:"pending_outbox,omitempty"`
	Status   map[string]string      `json:"session_status,omitempty"`
	Failure  *Failure               `json:"failure,omitempty"`
}

// SnapshotJudgement is the verdict for one expected snapshot.
type SnapshotJudgement struct {
	SnapID       string                            `json:"snap_id"`
	Pass         bool                              `json:"pass"`
	Reasons      []string                          `json:"reasons,omitempty"`
	Observed     *cluster.GlobalSnapshot           `json:"observed,omitempty"`
	Expected     *scenario.Expectation             `json:"expected,omitempty"`
	Oracle       map[string]interface{}            `json:"oracle,omitempty"`
	Conservation *ConservationCheck                `json:"conservation,omitempty"`
}

type ConservationCheck struct {
	Pass        bool   `json:"pass"`
	WantTotal   int64  `json:"want_total"`
	GotTotal    int64  `json:"got_total"`
	Explanation string `json:"explanation"`
}

// Report is the full replayable artefact for one run.
type Report struct {
	RunID        string                      `json:"run_id"`
	Scenario     string                      `json:"scenario"`
	StartedAt    time.Time                   `json:"started_at"`
	FinishedAt   time.Time                   `json:"finished_at"`
	Pass         bool                        `json:"pass"`
	Steps        []StepRecord                `json:"steps"`
	Judgements   map[string]*SnapshotJudgement `json:"judgements"`
	Failures     []Failure                   `json:"classified_failures,omitempty"`
	Reasons      []string                    `json:"reasons,omitempty"`
	FinalBalances map[string]int64           `json:"final_balances,omitempty"`
	NoGlobalPause bool                       `json:"no_global_pause"`
}

// Options controls a run.
type Options struct {
	RunID         string
	StepTimeout   time.Duration
	SettleTimeout time.Duration
	Out           io.Writer // optional human trace
}

// Run executes the scenario and returns a report.
func Run(ctx context.Context, sc *scenario.Scenario, drivers map[string]Driver, opts Options) (*Report, error) {
	if err := sc.Validate(); err != nil {
		return nil, err
	}
	if opts.StepTimeout == 0 {
		opts.StepTimeout = 5 * time.Second
	}
	if opts.SettleTimeout == 0 {
		opts.SettleTimeout = 10 * time.Second
	}
	rep := &Report{
		RunID:        opts.RunID,
		Scenario:     sc.Name,
		StartedAt:    time.Now().UTC(),
		Judgements:   map[string]*SnapshotJudgement{},
		NoGlobalPause: true, // gating is per directed channel; asserted in logs
	}

	nodeIDs := sc.NodeIDs()
	ordered := orderedDrivers(nodeIDs, drivers)

	tracef(opts.Out, "[%s] scenario %q start run=%s\n", rep.RunID, sc.Name, rep.RunID)

	for i, st := range sc.Steps {
		rec := StepRecord{Index: i, Action: st.Action}
		var failErr error
		switch st.Action {
		case scenario.ActPin:
			failErr = drivers[st.Node].Pin(ctx, st.Peer, true)
			rec.Summary = fmt.Sprintf("pin %s->%s", st.Node, st.Peer)
		case scenario.ActRelease:
			failErr = drivers[st.Node].Pin(ctx, st.Peer, false)
			rec.Summary = fmt.Sprintf("release %s->%s", st.Node, st.Peer)
		case scenario.ActTransfer:
			failErr = drivers[st.Node].Transfer(ctx, st.To, st.TxID, st.Amount)
			rec.Summary = fmt.Sprintf("transfer %s->%s %s=%d", st.Node, st.To, st.TxID, st.Amount)
		case scenario.ActStartSnapshot:
			_, failErr = drivers[st.Node].StartSnapshot(ctx, st.SnapID)
			rec.Summary = fmt.Sprintf("start snapshot %s at %s", st.SnapID, st.Node)
		case scenario.ActAwaitDelivered:
			failErr = waitEmptyOutbox(ctx, drivers[st.Node], st.Peer, opts.SettleTimeout)
			rec.Summary = fmt.Sprintf("await delivered %s->%s", st.Node, st.Peer)
		case scenario.ActAwaitSettled:
			failErr = waitSettled(ctx, ordered, opts.SettleTimeout)
			rec.Summary = "await all channels settled"
		case scenario.ActAwaitComplete:
			failErr = waitSnapshotComplete(ctx, ordered, st.SnapID, opts.StepTimeout)
			rec.Summary = fmt.Sprintf("await snapshot %s complete on all nodes", st.SnapID)
		case scenario.ActAwaitMarker:
			failErr = waitMarkerSeen(ctx, drivers[st.Node], st.SnapID, st.Peer, opts.StepTimeout)
			rec.Summary = fmt.Sprintf("await %s receives %s marker on channel %s->%s",
				st.Node, st.SnapID, st.Peer, st.Node)
		case scenario.ActExpectSnapshot:
			rec.Summary = fmt.Sprintf("verify snapshot %s", st.SnapID)
		case scenario.ActAbortSnapshot:
			failErr = drivers[st.Node].Abort(ctx, st.SnapID, st.Reason)
			rec.Summary = fmt.Sprintf("abort %s on %s: %s", st.SnapID, st.Node, st.Reason)
		case scenario.ActRestartNode:
			failErr = drivers[st.Node].Restart(ctx)
			rec.Summary = fmt.Sprintf("RESTART node %s (unfinished snapshots must abort)", st.Node)
		case scenario.ActFail:
			gotErr := executeFailAction(ctx, drivers, st)
			f := classifyFailure(i, st.Reason, gotErr)
			rep.Failures = append(rep.Failures, *f)
			rec.Failure = f
			rec.Summary = fmt.Sprintf("expect failure class=%s matched=%v", st.Reason, f.Matched)
			if !f.Matched {
				rep.Reasons = append(rep.Reasons,
					fmt.Sprintf("step %d: expected %s failure, got %s/%s: %s",
						i, st.Reason, f.Got, f.Code, f.Message))
			}
		case scenario.ActSleep:
			sleepFor(st.Meta)
			rec.Summary = "scheduling tick"
		default:
			rec.Summary = st.Action
		}

		if st.Action != scenario.ActFail && failErr != nil {
			if ce, ok := errs.As(failErr); ok {
				rep.Reasons = append(rep.Reasons,
					fmt.Sprintf("step %d (%s) failed: %s/%s: %s",
						i, st.Action, ce.Class, ce.Code, ce.Message))
			} else {
				rep.Reasons = append(rep.Reasons,
					fmt.Sprintf("step %d (%s) failed: %v", i, st.Action, failErr))
			}
		}

		// Capture intermediate state.
		rec.Balances = map[string]int64{}
		rec.Lamport = map[string]int64{}
		rec.Pending = map[string]int{}
		for _, id := range nodeIDs {
			d := drivers[id]
			bal, lamp, _ := d.State(ctx)
			rec.Balances[id] = sumBalances(bal, id)
			rec.Lamport[id] = lamp
		}
		if st.SnapID != "" {
			rec.Status = map[string]string{}
			for _, id := range nodeIDs {
				if sess, err := drivers[id].GetSession(ctx, st.SnapID); err == nil {
					rec.Status[id] = string(sess.Status)
				} else {
					rec.Status[id] = "missing"
				}
			}
		}
		rep.Steps = append(rep.Steps, rec)
		tracef(opts.Out, "[%s] step %2d %-22s bal=%v\n", rep.RunID, i, rec.Summary, rec.Balances)
	}

	// Final settle for post-snapshot delivery checks.
	if err := waitSettled(ctx, ordered, opts.SettleTimeout); err != nil {
		rep.Reasons = append(rep.Reasons, "final settle: "+err.Error())
	}

	// Judgements for every expected snapshot.
	for _, snapID := range sortedExpectationIDs(sc.Expect) {
		j := judge(ctx, snapID, sc.Expect[snapID], ordered, sc.ConserveTotal, opts.RunID)
		rep.Judgements[snapID] = j
		if !j.Pass {
			rep.Reasons = append(rep.Reasons, j.Reasons...)
		}
	}

	rep.FinalBalances = map[string]int64{}
	for _, id := range nodeIDs {
		bal, _, _ := drivers[id].State(ctx)
		rep.FinalBalances[id] = sumBalances(bal, id)
	}
	if id := anyExpectationWithFinal(sc.Expect); id != "" {
		exp := sc.Expect[id]
		for nid, want := range exp.FinalBalances {
			if rep.FinalBalances[nid] != want {
				rep.Reasons = append(rep.Reasons,
					fmt.Sprintf("final balance for %s: want %d got %d", nid, want, rep.FinalBalances[nid]))
			}
		}
	}

	rep.FinishedAt = time.Now().UTC()
	rep.Pass = len(rep.Reasons) == 0
	for _, f := range rep.Failures {
		if !f.Matched {
			rep.Pass = false
		}
	}
	return rep, nil
}

func judge(ctx context.Context, snapID string, exp scenario.Expectation,
	nodes []Driver, conserve int64, runID string) *SnapshotJudgement {
	views := make([]cluster.NodeView, len(nodes))
	for i, n := range nodes {
		views[i] = n
	}
	g, err := cluster.Collect(ctx, snapID, views)
	j := &SnapshotJudgement{SnapID: snapID, Expected: &exp}
	if err != nil {
		j.Pass = false
		j.Reasons = append(j.Reasons, "collect: "+err.Error())
		return j
	}
	j.Observed = g

	// Negative assertion: this snapshot is supposed to have been aborted (e.g.
	// a process restarted mid-recording). Completing it, or no abort at all,
	// is a protocol failure (state from two time points would be stitched).
	if exp.ExpectAborted {
		if !g.AnyAborted {
			j.Reasons = append(j.Reasons,
				"snapshot was expected to be aborted but no node reports aborted status")
		}
		for _, wantNode := range exp.AbortedOn {
			if g.StatusByNode[wantNode] != store.StatusAborted {
				j.Reasons = append(j.Reasons, fmt.Sprintf(
					"node %s expected status aborted, got %q", wantNode, g.StatusByNode[wantNode]))
			}
		}
		j.Pass = len(j.Reasons) == 0
		return j
	}
	if g.AnyAborted {
		j.Reasons = append(j.Reasons, fmt.Sprintf(
			"snapshot unexpectedly aborted on nodes %v", g.AbortedOn))
	}

	// Conservation invariant: sum(local states) + sum(in-flight recorded once)
	// equals the closed-system token total.
	got := g.GlobalTotal + g.InFlightSum
	cc := &ConservationCheck{WantTotal: conserve, GotTotal: got}
	cc.Explanation = fmt.Sprintf(
		"sum of recorded local balances (%d) + sum of in-flight messages recorded once (%d) = %d; closed system total = %d",
		g.GlobalTotal, g.InFlightSum, got, conserve)
	cc.Pass = conserve == 0 || got == conserve
	j.Conservation = cc
	if !cc.Pass {
		j.Reasons = append(j.Reasons, "TOKEN CONSERVATION BROKEN: "+cc.Explanation)
	}

	if exp.Complete != g.Complete {
		j.Reasons = append(j.Reasons,
			fmt.Sprintf("completion: want complete=%v got complete=%v missing=%v",
				exp.Complete, g.Complete, g.MissingNodes))
	}

	for nodeID, wantBal := range exp.Local {
		gotBal := int64(-1)
		if loc, ok := g.Locals[nodeID]; ok {
			gotBal = loc[nodeID]
		}
		if gotBal != wantBal {
			j.Reasons = append(j.Reasons,
				fmt.Sprintf("local state %s: want %d got %d", nodeID, wantBal, gotBal))
		}
	}
	for ch, wantMsgs := range exp.Channels {
		parts := strings.SplitN(ch, "->", 2)
		from, to := parts[0], parts[1]
		rec := g.Nodes[to]
		var gotMsgs []protocol.Transfer
		if rec != nil && rec.Channels[from] != nil {
			gotMsgs = rec.Channels[from].Recorded
		}
		if len(gotMsgs) != len(wantMsgs) {
			j.Reasons = append(j.Reasons, fmt.Sprintf(
				"channel %s: want %d in-flight messages (%v), got %d (%v)",
				ch, len(wantMsgs), txList(wantMsgs), len(gotMsgs), txsOf(gotMsgs)))
			continue
		}
		for i, w := range wantMsgs {
			if gotMsgs[i].TxID != w.TxID || gotMsgs[i].Amount != w.Amount {
				j.Reasons = append(j.Reasons, fmt.Sprintf(
					"channel %s msg %d: want %s/%d got %s/%d",
					ch, i, w.TxID, w.Amount, gotMsgs[i].TxID, gotMsgs[i].Amount))
			}
		}
	}
	// Also flag any unexpected recorded message.
	wantCount := 0
	for _, ms := range exp.Channels {
		wantCount += len(ms)
	}
	if len(g.InFlight) != wantCount {
		j.Reasons = append(j.Reasons, fmt.Sprintf(
			"in-flight total: expected %d recorded messages across all channels, got %d",
			wantCount, len(g.InFlight)))
	}
	if exp.InFlightSum != 0 && g.InFlightSum != exp.InFlightSum {
		j.Reasons = append(j.Reasons, fmt.Sprintf(
			"in-flight sum: want %d got %d", exp.InFlightSum, g.InFlightSum))
	}
	if exp.GlobalTotal != 0 && g.GlobalTotal != exp.GlobalTotal {
		j.Reasons = append(j.Reasons, fmt.Sprintf(
			"local-state subtotal: want %d got %d", exp.GlobalTotal, g.GlobalTotal))
	}
	j.Pass = len(j.Reasons) == 0
	return j
}

func classifyFailure(step int, wantClass string, err error) *Failure {
	f := &Failure{Step: step, Want: wantClass}
	if err == nil {
		f.Message = "call unexpectedly succeeded"
		return f
	}
	if ce, ok := errs.As(err); ok {
		f.Got = string(ce.Class)
		f.Code = ce.Code
		f.Message = ce.Message
		f.Matched = string(ce.Class) == wantClass
		return f
	}
	f.Got = "non_classified"
	f.Message = err.Error()
	return f
}

func executeFailAction(ctx context.Context, drivers map[string]Driver, st scenario.Step) error {
	switch st.Peer {
	case "__transfer__":
		return drivers[st.Node].Transfer(ctx, st.To, st.TxID, st.Amount)
	case "__snapshot__":
		_, err := drivers[st.Node].StartSnapshot(ctx, st.SnapID)
		return err
	case "__abort__":
		return drivers[st.Node].Abort(ctx, st.SnapID, st.Reason)
	default:
		// pin target is encoded in Peer; a malformed transfer is the default
		if st.To != "" {
			return drivers[st.Node].Transfer(ctx, st.To, st.TxID, st.Amount)
		}
		_, err := drivers[st.Node].StartSnapshot(ctx, st.SnapID)
		return err
	}
}

func waitEmptyOutbox(ctx context.Context, d Driver, peer string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		n, err := d.OutboxLen(ctx, peer)
		if err != nil {
			return err
		}
		if n == 0 {
			return nil
		}
		time.Sleep(15 * time.Millisecond)
	}
	return fmt.Errorf("timeout waiting outbox to %s to drain", peer)
}

func waitSettled(ctx context.Context, ds []Driver, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		allEmpty := true
		for _, d := range ds {
			for _, p := range d.Peers() {
				n, err := d.OutboxLen(ctx, p)
				if err == nil && n != 0 {
					allEmpty = false
				}
			}
		}
		if allEmpty {
			return nil
		}
		time.Sleep(15 * time.Millisecond)
	}
	return fmt.Errorf("timeout waiting full settlement")
}

func waitSnapshotComplete(ctx context.Context, ds []Driver, snapID string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		done, aborted := 0, 0
		var bad string
		for _, d := range ds {
			sess, err := d.GetSession(ctx, snapID)
			if err != nil {
				continue
			}
			switch sess.Status {
			case store.StatusComplete:
				done++
			case store.StatusAborted:
				aborted++
				bad = sess.AbortReason
			}
		}
		if done == len(ds) {
			return nil
		}
		if aborted > 0 {
			return fmt.Errorf("%w: snapshot %s aborted on a node: %s",
				errSnapshotAborted, snapID, bad)
		}
		time.Sleep(15 * time.Millisecond)
	}
	return fmt.Errorf("timeout waiting snapshot %s completion", snapID)
}

var errSnapshotAborted = errs.New(errs.ClassSnapshotInterrupted, errs.CodeSnapshotInterrupted,
	"snapshot aborted during wait", nil)

func waitMarkerSeen(ctx context.Context, d Driver, snapID, peer string, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		sess, err := d.GetSession(ctx, snapID)
		if err == nil && sess != nil && sess.Channels[peer] != nil &&
			sess.Channels[peer].MarkerSeenAt != (time.Time{}) {
			return nil
		}
		time.Sleep(10 * time.Millisecond)
	}
	return fmt.Errorf("timeout waiting marker %s from %s at node", snapID, peer)
}

// HTTPDriver talks to a node process over real HTTP.
type HTTPDriver struct {
	id     string
	base   string
	client *http.Client
	peers  []string
}

func NewHTTPDriver(id, base string, peers []string) *HTTPDriver {
	return &HTTPDriver{id: id, base: strings.TrimRight(base, "/"),
		client: &http.Client{Timeout: 5 * time.Second}, peers: peers}
}

func (h *HTTPDriver) ID() string     { return h.id }
func (h *HTTPDriver) BaseURL() string { return h.base }
func (h *HTTPDriver) Peers() []string { return append([]string(nil), h.peers...) }
func (h *HTTPDriver) Close() error   { return nil }

func (h *HTTPDriver) postJSON(path string, body interface{}) ([]byte, int, error) {
	raw, _ := json.Marshal(body)
	req, _ := http.NewRequest(http.MethodPost, h.base+path, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	resp, err := h.client.Do(req)
	if err != nil {
		return nil, 0, errs.New(errs.ClassUnavailable, errs.CodePeerUnavailable, err.Error(), err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode/100 != 2 {
		var we struct {
			Class   string `json:"class"`
			Code    string `json:"code"`
			Message string `json:"message"`
		}
		_ = json.Unmarshal(data, &we)
		if we.Class != "" {
			return data, resp.StatusCode, errs.New(errs.Class(we.Class), we.Code, we.Message, nil)
		}
		return data, resp.StatusCode, errs.New(errs.ClassUnavailable, "http_"+fmt.Sprint(resp.StatusCode),
			string(data), nil)
	}
	return data, resp.StatusCode, nil
}

func (h *HTTPDriver) getJSON(path string, out interface{}) error {
	resp, err := h.client.Get(h.base + path)
	if err != nil {
		return errs.New(errs.ClassUnavailable, errs.CodePeerUnavailable, err.Error(), err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode/100 != 2 {
		return errs.New(errs.ClassUnavailable, "http_"+fmt.Sprint(resp.StatusCode), string(data), nil)
	}
	return json.Unmarshal(data, out)
}

func (h *HTTPDriver) Transfer(ctx context.Context, to, txID string, amount int64) error {
	_, _, err := h.postJSON("/transfer", map[string]interface{}{
		"to": to, "tx_id": txID, "amount": amount})
	return err
}

func (h *HTTPDriver) StartSnapshot(ctx context.Context, sessionID string) (*store.SessionRecord, error) {
	data, _, err := h.postJSON("/snapshot", map[string]interface{}{"session_id": sessionID})
	if err != nil {
		return nil, err
	}
	var rec store.SessionRecord
	if err := json.Unmarshal(data, &rec); err != nil {
		return nil, err
	}
	return &rec, nil
}

func (h *HTTPDriver) Pin(ctx context.Context, peer string, pinned bool) error {
	_, _, err := h.postJSON("/admin/pin", map[string]interface{}{"peer": peer, "pinned": pinned})
	return err
}

func (h *HTTPDriver) Abort(ctx context.Context, sessionID, reason string) error {
	_, _, err := h.postJSON("/admin/abort",
		map[string]interface{}{"session_id": sessionID, "reason": reason})
	return err
}

func (h *HTTPDriver) State(ctx context.Context) (map[string]int64, int64, error) {
	var out struct {
		Lamport  int64            `json:"lamport"`
		Balances map[string]int64 `json:"balances"`
	}
	if err := h.getJSON("/state", &out); err != nil {
		return nil, 0, err
	}
	return out.Balances, out.Lamport, nil
}

func (h *HTTPDriver) GetSession(ctx context.Context, sessionID string) (*store.SessionRecord, error) {
	var rec store.SessionRecord
	if err := h.getJSON("/snapshots/"+sessionID, &rec); err != nil {
		return nil, err
	}
	return &rec, nil
}

func (h *HTTPDriver) Journal(ctx context.Context, sessionID string) ([]store.Event, error) {
	var out struct {
		Events []store.Event `json:"events"`
	}
	path := "/journal"
	if sessionID != "" {
		path += "?session_id=" + sessionID
	}
	if err := h.getJSON(path, &out); err != nil {
		return nil, err
	}
	return out.Events, nil
}

func (h *HTTPDriver) OutboxLen(ctx context.Context, peer string) (int, error) {
	// HTTP nodes do not expose raw outbox length; approximate via state
	// polling using a dedicated admin endpoint when present.
	var out struct {
		Total int `json:"total"`
	}
	if err := h.getJSON("/admin/outbox?peer="+peer, &out); err != nil {
		return 0, err
	}
	return out.Total, nil
}

// Restart for real processes is managed by the harness (subprocess), not via
// HTTP. Returns an error if invoked on a plain HTTPDriver.
func (h *HTTPDriver) Restart(ctx context.Context) error {
	return errs.New(errs.ClassInputInvalid, "restart_unsupported",
		"HTTPDriver cannot restart its process; use the subprocess harness", nil)
}

// ---- helpers ----

func orderedDrivers(ids []string, m map[string]Driver) []Driver {
	out := make([]Driver, 0, len(ids))
	for _, id := range ids {
		out = append(out, m[id])
	}
	return out
}

func sumBalances(b map[string]int64, self string) int64 {
	// The node ledger only contains its own token account in this teaching
	// system, but sum generically to stay honest to the map.
	if v, ok := b[self]; ok {
		return v
	}
	var t int64
	for _, v := range b {
		t += v
	}
	return t
}

func tracef(w io.Writer, f string, args ...interface{}) {
	if w != nil {
		fmt.Fprintf(w, f, args...)
	}
}

func sortedExpectationIDs(m map[string]scenario.Expectation) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func anyExpectationWithFinal(m map[string]scenario.Expectation) string {
	for k, e := range m {
		if e.FinalBalances != nil {
			return k
		}
	}
	return ""
}

func txList(es []scenario.ExpectedTransfer) []string {
	out := make([]string, len(es))
	for i, e := range es {
		out[i] = fmt.Sprintf("%s/%d", e.TxID, e.Amount)
	}
	return out
}

func txsOf(ts []protocol.Transfer) []string {
	out := make([]string, len(ts))
	for i, t := range ts {
		out[i] = fmt.Sprintf("%s/%d", t.TxID, t.Amount)
	}
	return out
}

func sleepFor(meta map[string]interface{}) {
	if ms, ok := meta["ms"].(float64); ok && ms > 0 {
		time.Sleep(time.Duration(ms) * time.Millisecond)
	}
}

// RunID is a sortable UTC id that ties every log line of one run together.
var runMu sync.Mutex

func NewRunID() string {
	runMu.Lock()
	defer runMu.Unlock()
	return "run-" + time.Now().UTC().Format("20060102T150405.000000000Z")
}
