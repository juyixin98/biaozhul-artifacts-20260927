package reassembly

import "tcpreplay/internal/netmodel"

// DirectionView is the replayable answer for one direction: only continuously
// evidenced bytes are output; everything uncertain is reported separately.
//
// All coordinates in the view are relative to the first application-data byte
// of the direction, which is 0. When the SYN was observed the SYN occupies the
// internal sequence position immediately before 0 (SYN consumes one sequence
// number); in the anchorless case 0 is simply the earliest byte observed. Raw
// 32-bit sequence numbers are carried alongside coordinates in conflicts and
// events so the mapping can be audited.
type DirectionView struct {
	Direction      string     `json:"direction"`
	Role           string     `json:"role"` // "client"/"server" when handshake known
	HandshakeKnown bool       `json:"handshake_known"`
	Stream         []byte     `json:"stream"`            // contiguous evidenced prefix
	ContiguousEnd  int64      `json:"contiguous_end"`    // first missing byte offset
	HeldRuns       []ByteRun  `json:"held_out_of_order"` // evidence beyond a gap
	Gaps           []Gap      `json:"gaps"`
	FINSeen        bool       `json:"fin_seen"`
	FINPos         int64      `json:"fin_position"` // equals proved byte count when FIN seen
	Reset          bool       `json:"reset"`
	HeldQuarantine []HeldByte `json:"quarantined_bytes"`
	LengthProved   int64      `json:"length_proved"` // FIN-based total length when known
}

// GenerationView is one incarnation of a connection.
type GenerationView struct {
	Flow        string        `json:"flow"`
	Generation  int           `json:"generation"`
	Established bool          `json:"established"`
	Closed      bool          `json:"closed"`
	Reset       bool          `json:"reset"`
	AtoB        DirectionView `json:"a_to_b"`
	BtoA        DirectionView `json:"b_to_a"`
}

// FlowSummary describes a flow and its generations for listing.
type FlowSummary struct {
	Flow        string `json:"flow"`
	Generations int    `json:"generations"`
}

func (m *Manager) viewDirection(g *Generation, d *DirectionState, aToB bool) DirectionView {
	v := DirectionView{
		Direction:      dirName(aToB),
		HandshakeKnown: d.handshakeKnown,
		FINSeen:        d.finSeen,
		Reset:          d.reset,
		FINPos:         -1,
	}
	if g.rolesSet {
		isClientDir := (aToB && g.clientIsA) || (!aToB && !g.clientIsA)
		if isClientDir {
			v.Role = "client"
		} else {
			v.Role = "server"
		}
	}
	for _, h := range d.held {
		v.HeldQuarantine = append(v.HeldQuarantine, HeldByte{
			Offset: h.Offset - d.base, RawSeq: h.RawSeq, Value: h.Value, RecordID: h.RecordID,
		})
	}

	end := d.nextContiguous()
	v.ContiguousEnd = end - d.base
	var stream []byte
	for p := d.base; p < end; p++ {
		stream = append(stream, d.buf.cells[p])
	}
	v.Stream = stream

	for _, r := range d.buf.heldRuns(end) {
		v.HeldRuns = append(v.HeldRuns, ByteRun{Start: r.Start - d.base, End: r.End - d.base, Data: r.Data})
	}
	hi := d.extentEnd()
	for _, gp := range d.buf.gaps(end, hi, d.finSeen) {
		v.Gaps = append(v.Gaps, Gap{Start: gp.Start - d.base, End: gp.End - d.base})
	}
	if d.finSeen {
		v.FINPos = d.finPos - d.base
		v.LengthProved = d.finPos - d.base
	}
	return v
}

// ViewGeneration returns the replay view of one generation. Unknown
// directions (never observed) are returned zero-valued.
func (m *Manager) ViewGeneration(k netmodel.FlowKey, idx int) (GenerationView, bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	gens := m.flows[k]
	if idx < 0 || idx >= len(gens) {
		return GenerationView{}, false
	}
	g := gens[idx]
	out := GenerationView{
		Flow: k.String(), Generation: g.Index, Established: g.established,
		Closed: g.closed, Reset: g.reset,
	}
	if g.AtoB != nil {
		out.AtoB = m.viewDirection(g, g.AtoB, true)
	} else {
		out.AtoB = m.viewDirection(g, newDirectionState(), true)
	}
	if g.BtoA != nil {
		out.BtoA = m.viewDirection(g, g.BtoA, false)
	} else {
		out.BtoA = m.viewDirection(g, newDirectionState(), false)
	}
	return out, true
}

// Flows lists known flows in canonical (string) order.
func (m *Manager) Flows() []FlowSummary {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]FlowSummary, 0, len(m.flows))
	for k, gens := range m.flows {
		out = append(out, FlowSummary{Flow: k.String(), Generations: len(gens)})
	}
	// Deterministic order.
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j-1].Flow > out[j].Flow; j-- {
			out[j-1], out[j] = out[j], out[j-1]
		}
	}
	return out
}

// Conflicts returns every byte-level conflict recorded since the manager was
// created, in emission order, with data-byte-relative offsets (first
// application byte of the direction is 0).
func (m *Manager) Conflicts() []Conflict {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]Conflict, 0, len(m.conflicts))
	for _, c := range m.conflicts {
		cc := c
		if d := m.findDirection(c.Flow, c.Generation, c.Direction); d != nil {
			cc.ByteOffset = c.ByteOffset - d.base
		}
		out = append(out, cc)
	}
	return out
}

func (m *Manager) findDirection(flow string, gen int, direction string) *DirectionState {
	for k, gs := range m.flows {
		if k.String() != flow || gen < 0 || gen >= len(gs) {
			continue
		}
		g := gs[gen]
		if direction == "a_to_b" {
			return g.AtoB
		}
		return g.BtoA
	}
	return nil
}

// AllViews materializes every generation of every flow, ordered by flow and
// generation index. It is used once at the end of an ingest for persistence.
func (m *Manager) AllViews() []GenerationView {
	m.mu.Lock()
	defer m.mu.Unlock()
	keys := make([]netmodel.FlowKey, 0, len(m.flows))
	for k := range m.flows {
		keys = append(keys, k)
	}
	// Order by the canonical string to keep output stable.
	for i := 1; i < len(keys); i++ {
		for j := i; j > 0; j-- {
			a, b := keys[j-1], keys[j]
			if !(a.String() > b.String()) {
				break
			}
			keys[j-1], keys[j] = b, a
		}
	}
	var out []GenerationView
	for _, k := range keys {
		for i := range m.flows[k] {
			g := m.flows[k][i]
			v := GenerationView{
				Flow: k.String(), Generation: g.Index, Established: g.established,
				Closed: g.closed, Reset: g.reset,
			}
			if g.AtoB != nil {
				v.AtoB = m.viewDirection(g, g.AtoB, true)
			} else {
				v.AtoB = m.viewDirection(g, newDirectionState(), true)
			}
			if g.BtoA != nil {
				v.BtoA = m.viewDirection(g, g.BtoA, false)
			} else {
				v.BtoA = m.viewDirection(g, newDirectionState(), false)
			}
			out = append(out, v)
		}
	}
	return out
}

// flowGenerationCount is used by the service when storing results.
func (m *Manager) flowGenerationCount(k netmodel.FlowKey) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.flows[k])
}
