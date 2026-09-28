package engine

import (
	"sort"

	"netpolicy/internal/domain"
)

// MatrixCell is one ordered (source -> destination) entry of the exhaustive
// connectivity matrix for one concrete traffic descriptor.
type MatrixCell struct {
	SourceUID string    `json:"sourceUid"`
	DestUID   string    `json:"destUid"`
	Decision  *Decision `json:"decision"`
}

// MatrixResult is the full matrix over all ordered endpoint pairs, including
// self pairs.
type MatrixResult struct {
	Revision int64        `json:"revision"`
	Protocol string       `json:"protocol"`
	Port     int          `json:"port"`
	Cells    []MatrixCell `json:"cells"`
}

// MatrixRequest describes one matrix sweep.
type MatrixRequest struct {
	Protocol domain.Protocol
	Port     int
	// PinRevision rejects a stale requested version.
	PinRevision int64
}

// Matrix evaluates every ordered endpoint pair. The sweep is deterministic:
// endpoints are sorted by namespace/name/uid and each cell is an independent
// Check, so named ports are always resolved against that cell's destination.
func (e *Engine) Matrix(req MatrixRequest) (*MatrixResult, error) {
	eps := make([]domain.Endpoint, len(e.snap.Endpoints))
	copy(eps, e.snap.Endpoints)
	sort.Slice(eps, func(i, j int) bool {
		if eps[i].Namespace != eps[j].Namespace {
			return eps[i].Namespace < eps[j].Namespace
		}
		if eps[i].Name != eps[j].Name {
			return eps[i].Name < eps[j].Name
		}
		return eps[i].UID < eps[j].UID
	})
	res := &MatrixResult{
		Revision: e.snap.Revision,
		Protocol: string(req.Protocol),
		Port:     req.Port,
	}
	for _, src := range eps {
		for _, dst := range eps {
			d, err := e.Check(Input{
				SourceUID:   src.UID,
				DestUID:     dst.UID,
				Protocol:    req.Protocol,
				Port:        req.Port,
				PinRevision: req.PinRevision,
			})
			if err != nil {
				return nil, err
			}
			res.Cells = append(res.Cells, MatrixCell{SourceUID: src.UID, DestUID: dst.UID, Decision: d})
		}
	}
	return res, nil
}

// EndpointPorts returns the de-duplicated numeric (protocol,port) pairs
// served by any endpoint in the snapshot, in stable order. It lets callers
// build a matrix sweep that covers named-port targets without ever treating a
// name as a global number: every enumerated value comes from some concrete
// endpoint's declaration.
func (e *Engine) EndpointPorts() []PortRef {
	seen := map[PortRef]bool{}
	var out []PortRef
	for _, ep := range e.snap.Endpoints {
		for _, p := range ep.Ports {
			proto := p.Protocol
			if proto == "" {
				proto = domain.ProtocolTCP
			}
			ref := PortRef{Protocol: proto, Number: p.Number}
			if !seen[ref] {
				seen[ref] = true
				out = append(out, ref)
			}
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Protocol != out[j].Protocol {
			return out[i].Protocol < out[j].Protocol
		}
		return out[i].Number < out[j].Number
	})
	return out
}

// PortRef is one protocol/number pair.
type PortRef struct {
	Protocol domain.Protocol `json:"protocol"`
	Number   int             `json:"number"`
}

// FullMatrix sweeps every ordered pair against every port declared by any
// endpoint (plus an explicit extra probe when requested), returning a
// per-port matrix list. A single extraPort < 0 disables the extra probe.
type FullMatrixResult struct {
	Revision int64           `json:"revision"`
	Matrices []*MatrixResult `json:"matrices"`
}

// FullMatrix builds one matrix per declared endpoint port. Undeclared probe
// ports can be passed to verify that policies never allow traffic to ports no
// workload serves.
func (e *Engine) FullMatrix(extraPorts ...PortRef) (*FullMatrixResult, error) {
	refs := e.EndpointPorts()
	for _, x := range extraPorts {
		if x.Number > 0 && !containsRef(refs, x) {
			refs = append(refs, x)
		}
	}
	out := &FullMatrixResult{Revision: e.snap.Revision}
	for _, ref := range refs {
		m, err := e.Matrix(MatrixRequest{Protocol: ref.Protocol, Port: ref.Number})
		if err != nil {
			return nil, err
		}
		out.Matrices = append(out.Matrices, m)
	}
	return out, nil
}

func containsRef(xs []PortRef, x PortRef) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
