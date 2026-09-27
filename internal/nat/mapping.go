package nat

import (
	"fmt"
	"time"

	"natlab/internal/model"
)

// TCP state names used by the simplified state machine.
const (
	StateSynSent     = "SYN_SENT"
	StateEstablished = "ESTABLISHED"
	StateFinWait1    = "FIN_WAIT_1" // internal sent FIN first
	StateFinWait2    = "FIN_WAIT_2" // external FIN observed first
	StateTimeWait    = "TIME_WAIT"
	StateClosed      = "CLOSED"

	// UDP pseudo-state shown in snapshots and logs.
	StateUDPOpen = "UDP_OPEN"
)

// mapping is one live NAPT translation. The model is symmetric: one internal
// flow (full internal 5-tuple) owns one external port, and traffic returning on
// that port must come from the exact remote endpoint the flow was opened to.
type mapping struct {
	id         string
	proto      model.Protocol
	intSrcIP   string
	intSrcPort uint16
	remIP      string
	remPort    uint16
	extPort    uint16

	state string

	// TCP FIN bookkeeping for the deterministic two-FIN close.
	finInternal bool
	finExternal bool

	createdAt time.Time
	lastSeen  time.Time
	expiresAt time.Time
}

// flowKey identifies a mapping from the internal side.
type flowKey struct {
	proto   model.Protocol
	intIP   string
	intPort uint16
	remIP   string
	remPort uint16
}

// portKey finds the mapping currently holding an external port. The remote
// endpoint is checked separately so a wrong peer yields
// remote_endpoint_mismatch rather than no_matching_mapping.
type portKey struct {
	proto model.Protocol
	port  uint16
}

func keyOf(p model.Packet) flowKey {
	t := p.Tuple
	return flowKey{proto: t.Proto, intIP: t.SrcIP, intPort: t.SrcPort,
		remIP: t.DstIP, remPort: t.DstPort}
}

func inboundPortKey(p model.Packet) portKey {
	return portKey{proto: p.Tuple.Proto, port: p.Tuple.DstPort}
}

func mappingID(proto model.Protocol, extPort uint16) string {
	return fmt.Sprintf("%s-%d", proto, extPort)
}

func (m *mapping) snapshot() model.MappingView {
	internal := model.FiveTuple{SrcIP: m.intSrcIP, SrcPort: m.intSrcPort,
		DstIP: m.remIP, DstPort: m.remPort, Proto: m.proto}
	external := model.FiveTuple{SrcIP: "", SrcPort: m.extPort,
		DstIP: m.remIP, DstPort: m.remPort, Proto: m.proto}
	return model.MappingView{
		ID: m.id, Proto: m.proto, Internal: internal, External: external,
		ExternalPort: m.extPort, State: m.state,
		CreatedAt: m.createdAt, LastSeen: m.lastSeen, ExpiresAt: m.expiresAt,
	}
}
