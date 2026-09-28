package scenario

import (
	"fmt"
	"sort"
	"strings"
)

// This file is the INDEPENDENT REFERENCE ORACLE.
//
// It is a from-scratch, single-goroutine simulation of exactly what the
// Chandy-Lamport protocol records for a scripted scenario. It deliberately
// shares nothing with the production kernel:
//
//   - no imports of internal/node, internal/snapshot or internal/store;
//   - no HTTP, no goroutines, no clocks — the scenario script already fixes
//     the happens-before order, which is all a FIFO snapshot depends on;
//   - balances are plain int64 arithmetic.
//
// Production snapshots are judged against the oracle's prediction and, for the
// checked-in fixtures, against hand-authored expectations.

type orChannel struct {
	// messages currently in flight, in channel (FIFO) order
	queue []orMsg
}

type orMsg struct {
	txID   string
	amount int64
	kind   string // "transfer" or "marker"
	snap   string
}

type orLocal struct {
	balance int64
}

type orSession struct {
	local     map[string]int64   // recorded local balance per node
	channels  map[string][]orMsg // recorded in-flight, key "from->to"
	closed    map[string]bool    // "from->to" channel closed
	started   map[string]bool    // node has recorded local state
	complete  map[string]bool    // node complete (all incoming closed)
	abortedOn map[string]string  // node -> reason
}

type oracle struct {
	order   []string
	bal     map[string]int64
	chans   map[string]*orChannel // key "from->to"
	sess    map[string]*orSession
	// pin state per directed channel: when pinned, messages stay queued
	pinned  map[string]bool
}

func newOracle(order []string, initial map[string]int64) *oracle {
	o := &oracle{
		order:  append([]string(nil), order...),
		bal:    map[string]int64{},
		chans:  map[string]*orChannel{},
		sess:   map[string]*orSession{},
		pinned: map[string]bool{},
	}
	for _, id := range order {
		o.bal[id] = initial[id]
	}
	for _, a := range order {
		for _, b := range order {
			if a != b {
				o.chans[a+"->"+b] = &orChannel{}
			}
		}
	}
	return o
}

func chKey(from, to string) string { return from + "->" + to }

// transferSubmit mirrors the production send semantics: debit immediately,
// append a transfer message to the directed channel.
func (o *oracle) transferSubmit(from, to, txID string, amount int64) error {
	if o.bal[from] < amount {
		return fmt.Errorf("oracle: insufficient funds: %s has %d < %d", from, o.bal[from], amount)
	}
	o.bal[from] -= amount
	o.chans[chKey(from, to)].queue = append(o.chans[chKey(from, to)].queue,
		orMsg{txID: txID, amount: amount, kind: "transfer"})
	return nil
}

func (o *oracle) setPinned(from, to string, p bool) {
	o.pinned[chKey(from, to)] = p
}

// deliverDue walks unpinned channels in a fixed order and delivers their
// queues until each hits a pin boundary. FIFO per channel is preserved.
func (o *oracle) deliverDue() {
	for progress := true; progress; {
		progress = false
		keys := make([]string, 0, len(o.chans))
		for k := range o.chans {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			if o.pinned[k] {
				continue
			}
			ch := o.chans[k]
			if len(ch.queue) == 0 {
				continue
			}
			m := ch.queue[0]
			ch.queue = ch.queue[1:]
			parts := strings.SplitN(k, "->", 2)
			o.deliver(parts[0], parts[1], m)
			progress = true
		}
	}
}

func (o *oracle) deliver(from, to string, m orMsg) {
	if m.kind == "transfer" {
		// Credit first (live state), then snapshots record per rule 2.
		o.bal[to] += m.amount
		for _, sess := range o.sess {
			key := chKey(from, to)
			if sess.started[to] && !sess.closed[key] {
				sess.channels[key] = append(sess.channels[key], m)
			}
		}
		return
	}
	// marker
	sess := o.sess[m.snap]
	if sess == nil {
		return
	}
	if sess.abortedOn[to] != "" {
		// Dead round: the marker is refused by the restarted node.
		sess.closed[chKey(from, to)] = true
		return
	}
	if !sess.started[to] {
		// First marker at this node: record local state, then relay markers on
		// every outgoing channel (rule 1).
		sess.started[to] = true
		sess.local[to] = o.bal[to] // frozen balance at this node's cut
		for _, other := range o.order {
			if other != to {
				o.chans[chKey(to, other)].queue = append(
					o.chans[chKey(to, other)].queue,
					orMsg{kind: "marker", snap: m.snap})
			}
		}
	}
	sess.closed[chKey(from, to)] = true
	sess.checkComplete(to, o.order)
}

// startSnapshot at initiator: record local state, marker on every outgoing.
func (o *oracle) startSnapshot(id, initiator string) {
	sess := &orSession{
		local:     map[string]int64{},
		channels:  map[string][]orMsg{},
		closed:    map[string]bool{},
		started:   map[string]bool{},
		complete:  map[string]bool{},
		abortedOn: map[string]string{},
	}
	o.sess[id] = sess
	sess.started[initiator] = true
	sess.local[initiator] = o.bal[initiator]
	for _, other := range o.order {
		if other != initiator {
			o.chans[chKey(initiator, other)].queue = append(
				o.chans[chKey(initiator, other)].queue,
				orMsg{kind: "marker", snap: id})
		}
	}
	sess.checkComplete(initiator, o.order)
}

func (s *orSession) checkComplete(node string, order []string) {
	for _, other := range order {
		if other == node {
			continue
		}
		if !s.closed[chKey(other, node)] {
			return
		}
	}
	s.complete[node] = true
}

// settle releases all pins and drains every channel.
func (o *oracle) settle() {
	for k := range o.pinned {
		o.pinned[k] = false
	}
	o.deliverDue()
}

// restart marks any session still recording at that node aborted.
func (o *oracle) restart(node string) {
	for _, sess := range o.sess {
		if sess.started[node] && !sess.complete[node] {
			sess.abortedOn[node] = "restarted while recording"
		}
	}
}

// Prediction is the oracle's answer for one snapshot.
type Prediction struct {
	Complete     bool
	Aborted      map[string]string
	Local        map[string]int64
	Channels     map[string][]ExpectedTransfer
	InFlightSum  int64
	GlobalTotal  int64
	FinalBalances map[string]int64
}

func (o *oracle) predict(id string, finalBalances map[string]int64) Prediction {
	sess := o.sess[id]
	p := Prediction{
		Local:         map[string]int64{},
		Channels:      map[string][]ExpectedTransfer{},
		Aborted:       map[string]string{},
		FinalBalances: finalBalances,
	}
	if sess == nil {
		return p
	}
	complete := len(sess.started) == len(o.order)
	for _, n := range o.order {
		if sess.abortedOn[n] != "" {
			p.Aborted[n] = sess.abortedOn[n]
			complete = false
		}
		if !sess.complete[n] {
			complete = false
		}
	}
	p.Complete = complete
	for n, v := range sess.local {
		p.Local[n] = v
		p.GlobalTotal += v
	}
	keys := make([]string, 0, len(sess.channels))
	for k := range sess.channels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		msgs := sess.channels[k]
		exp := make([]ExpectedTransfer, 0, len(msgs))
		for _, m := range msgs {
			exp = append(exp, ExpectedTransfer{TxID: m.txID, Amount: m.amount})
			p.InFlightSum += m.amount
		}
		if len(exp) > 0 {
			p.Channels[k] = exp
		}
	}
	return p
}

// RunOracle executes the whole scenario and returns predictions per snap id,
// plus the final live balances after every scenario step has settled. It
// returns an error only on an internally inconsistent script (which is a
// fixture bug, distinct from any production failure class).
func RunOracle(s *Scenario) (map[string]Prediction, error) {
	if err := s.Validate(); err != nil {
		return nil, err
	}
	order := s.NodeIDs()
	o := newOracle(order, s.InitialBalance)

	predictions := map[string]Prediction{}
	predicted := map[string]bool{}

	for _, st := range s.Steps {
		switch st.Action {
		case ActPin:
			o.setPinned(st.Node, st.Peer, true)
		case ActRelease:
			o.setPinned(st.Node, st.Peer, false)
			o.deliverDue()
		case ActTransfer:
			if err := o.transferSubmit(st.Node, st.To, st.TxID, st.Amount); err != nil {
				return nil, err
			}
		case ActStartSnapshot:
			o.startSnapshot(st.SnapID, st.Node)
			o.deliverDue()
		case ActAwaitDelivered, ActAwaitSettled:
			if st.Peer != "" {
				o.setPinned(st.Node, st.Peer, false)
			}
			o.deliverDue()
		case ActAwaitComplete:
			o.deliverDue()
		case ActExpectSnapshot:
			o.deliverDue()
			predictions[st.SnapID] = o.predict(st.SnapID, nil)
			predicted[st.SnapID] = true
		case ActRestartNode:
			o.restart(st.Node)
		case ActAbortSnapshot:
			if sess := o.sess[st.SnapID]; sess != nil {
				sess.abortedOn[st.Node] = st.Reason
			}
		case ActSleep, ActFail:
			// no semantic content
		}
	}
	// Final settle: release pins, drain, and re-predict snapshots that the
	// fixture expects after full delivery (e.g. final_balances checks).
	o.settle()
	finalBalances := map[string]int64{}
	for _, id := range order {
		finalBalances[id] = o.bal[id]
	}
	for id := range o.sess {
		p := o.predict(id, finalBalances)
		if _, ok := predictions[id]; !ok {
			predictions[id] = p
		} else {
			// keep original cut prediction; only attach final balances
			prev := predictions[id]
			prev.FinalBalances = finalBalances
			predictions[id] = prev
		}
	}
	return predictions, nil
}
