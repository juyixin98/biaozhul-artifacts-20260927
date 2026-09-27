package reassembly

import (
	"fmt"

	"tcpreasm/internal/config"
	"tcpreasm/internal/tcpmodel"
)

// TCP state labels tracked per direction/generation. We only model the
// subset that changes evidence semantics.
const (
	stClosed      = "CLOSED"
	stSynSent     = "SYN_SENT"
	stSynReceived = "SYN_RCVD"
	stEstablished = "ESTABLISHED"
	stFinWait     = "FIN_WAIT"
	stReset       = "RESET"
)

// generation binds two directional assemblers for one handshake instance of
// a (possibly reused) 4-tuple.
type generation struct {
	index     int
	inferred  bool
	connState string // connection-level label
	birth     int64  // ingest sequence at creation

	c2s *dirAssembler
	s2c *dirAssembler
}

func newGeneration(flowKey string, index int, inferred bool, cfg config.ReassemblyConfig) *generation {
	mk := func(dir string) *dirAssembler {
		return &dirAssembler{
			flowKey: flowKey, genIndex: index, direction: dir,
			policy: cfg.OverlapPolicy, maxBuffer: cfg.MaxBufferedBytesPerDir,
			evTail: cfg.DeliveredEvidenceBytes, inferred: inferred,
		}
	}
	g := &generation{
		index: index, inferred: inferred, connState: stSynSent,
		c2s: mk(string(tcpmodel.DirC2S)),
		s2c: mk(string(tcpmodel.DirS2C)),
	}
	g.c2s.inferred = inferred
	g.s2c.inferred = inferred
	return g
}

// birthSeq returns the ingest sequence at which the generation was created.
func (g *generation) birthSeq() int64 { return g.birth }

func (g *generation) dir(d tcpmodel.Direction) *dirAssembler {
	if d == tcpmodel.DirS2C {
		return g.s2c
	}
	return g.c2s
}

// stateLabels returns per-direction state labels for persistence.
func (g *generation) stateLabels() (c2s, s2c string) {
	return g.dirState(g.c2s), g.dirState(g.s2c)
}

func (g *generation) dirState(d *dirAssembler) string {
	switch {
	case d.reset:
		return stReset
	case d.finDone:
		return stClosed
	case d.finSeen:
		return stFinWait
	case d.hasISN && g.connState == stEstablished:
		return stEstablished
	case d.hasISN:
		// A direction that has consumed its SYN but the three-way handshake
		// is not fully modeled: still distinguish initiator/responder.
		if d.direction == string(tcpmodel.DirC2S) && g.connState == stSynSent {
			return stSynSent
		}
		return stSynReceived
	default:
		return stClosed
	}
}

// isClosedOrReset reports whether this generation accepts no more data.
func (g *generation) isClosedOrReset() bool {
	if g.c2s.reset || g.s2c.reset {
		return true
	}
	// A real generation is done only after both directions closed. An
	// inferred generation is never assumed closed by handshake state.
	if !g.inferred && g.c2s.finDone && g.s2c.finDone {
		return true
	}
	return false
}

// connSummary returns a connection-level state for the connections table.
func (g *generation) connSummary() string {
	switch {
	case g.c2s.reset || g.s2c.reset:
		return stReset
	case g.c2s.finDone && g.s2c.finDone:
		return stClosed
	case g.c2s.finSeen || g.s2c.finSeen:
		return stFinWait
	default:
		return g.connState
	}
}

// handshakeProgress 0=no syn, 1=SYN seen, 2=SYN+ACK seen.
func (g *generation) handshakeProgress() int {
	switch g.connState {
	case stSynSent:
		if g.s2c.hasISN {
			return 2
		}
		if g.c2s.hasISN {
			return 1
		}
	case stEstablished:
		return 2
	}
	return 0
}

func (g *generation) String() string {
	return fmt.Sprintf("gen#%d inferred=%v state=%s", g.index, g.inferred, g.connSummary())
}
