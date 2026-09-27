package model

import (
	"fmt"
	"strconv"

	"pathvector/internal/ierr"
)

// Topology is the fixed small autonomous-system graph used by a replay.
// Nodes are BGP-like speakers; Links are directed import/export policy
// attachment points (a link from A to B models the eBGP/iBGP session over
// which B receives routes advertised by A).
type Topology struct {
	Nodes []Node `json:"nodes"`
	Links []Link `json:"links"`
}

// Node is one router in the topology.
type Node struct {
	// RouterID is the stable identifier ("br1", ...) referenced by events.
	RouterID string `json:"router_id"`
	// ASN is the autonomous system number the router belongs to.
	ASN int `json:"asn"`
	// Address is the router's loopback/peering address. It is the default
	// next_hop of eBGP routes originated by this router and lets iBGP
	// peers resolve the preserved eBGP next_hop back to the egress node
	// for IGP-cost comparison. Optional for non-border routers.
	Address string `json:"address,omitempty"`
	// IGPCost is the cost of reaching this node from the replay's
	// observation point; used as an early-exit metric for next-hop tie
	// breaking (higher IGP cost = worse).
	IGPCost int `json:"igp_cost"`
}

// Link is one directed peering: src advertises to dst. The pair is
// de-duplicated symmetrically by Index so each session is stored once.
type Link struct {
	Src string `json:"src"`
	Dst string `json:"dst"`
}

// Index is the validated, queryable form of Topology.
type Index struct {
	Nodes     map[string]*Node
	ByID      map[string]int // router id -> node ordinal (tie-break)
	addrToID  map[string]string
	neighbors map[string][]string
	pairKey   map[[2]string]bool
	order     []string
}

// Build validates the topology and returns an Index. It rejects duplicate
// router ids, unknown ASN ranges, dangling links, self-links and duplicate
// sessions.
func (t Topology) Build() (*Index, error) {
	const op = "model.BuildTopology"
	idx := &Index{
		Nodes:     map[string]*Node{},
		ByID:      map[string]int{},
		addrToID:  map[string]string{},
		neighbors: map[string][]string{},
		pairKey:   map[[2]string]bool{},
	}
	if len(t.Nodes) == 0 {
		return nil, ierr.New(ierr.KindInvalidInput, op, "topology has no nodes")
	}
	for i := range t.Nodes {
		n := &t.Nodes[i]
		if n.RouterID == "" {
			return nil, ierr.New(ierr.KindInvalidInput, op, fmt.Sprintf("node #%d has empty router_id", i))
		}
		if _, dup := idx.Nodes[n.RouterID]; dup {
			return nil, ierr.New(ierr.KindInvalidInput, op, "duplicate router_id "+n.RouterID)
		}
		if n.ASN <= 0 || n.ASN > 4294967295 {
			return nil, ierr.New(ierr.KindInvalidInput, op,
				fmt.Sprintf("node %s: asn %d out of range [1,4294967295]", n.RouterID, n.ASN))
		}
		if n.IGPCost < 0 {
			return nil, ierr.New(ierr.KindInvalidInput, op,
				fmt.Sprintf("node %s: igp_cost must be >= 0", n.RouterID))
		}
		if n.Address != "" {
			if err := ParseAddress(n.Address); err != nil {
				return nil, ierr.Wrap(ierr.KindInvalidInput, op,
					fmt.Sprintf("node %s: bad address", n.RouterID), err)
			}
			if _, dup := idx.addrToID[n.Address]; dup {
				return nil, ierr.New(ierr.KindInvalidInput, op,
					"duplicate router address "+n.Address)
			}
			idx.addrToID[n.Address] = n.RouterID
		}
		idx.Nodes[n.RouterID] = n
		idx.ByID[n.RouterID] = i
		idx.order = append(idx.order, n.RouterID)
	}
	for _, l := range t.Links {
		if _, ok := idx.Nodes[l.Src]; !ok {
			return nil, ierr.New(ierr.KindInvalidInput, op, "link references unknown node "+l.Src)
		}
		if _, ok := idx.Nodes[l.Dst]; !ok {
			return nil, ierr.New(ierr.KindInvalidInput, op, "link references unknown node "+l.Dst)
		}
		if l.Src == l.Dst {
			return nil, ierr.New(ierr.KindInvalidInput, op, "self-link on "+l.Src)
		}
		a, b := l.Src, l.Dst
		if a > b {
			a, b = b, a
		}
		key := [2]string{a, b}
		if idx.pairKey[key] {
			return nil, ierr.New(ierr.KindInvalidInput, op,
				"duplicate peering session between "+l.Src+" and "+l.Dst)
		}
		idx.pairKey[key] = true
		idx.neighbors[l.Src] = append(idx.neighbors[l.Src], l.Dst)
		idx.neighbors[l.Dst] = append(idx.neighbors[l.Dst], l.Src)
	}
	// Deterministic neighbor ordering: router id ordinal.
	for id := range idx.neighbors {
		ns := idx.neighbors[id]
		for i := 1; i < len(ns); i++ {
			for j := i; j > 0 && idx.ByID[ns[j-1]] > idx.ByID[ns[j]]; j-- {
				ns[j-1], ns[j] = ns[j], ns[j-1]
			}
		}
	}
	return idx, nil
}

// Neighbors returns the routers holding a session with id in ordinal order.
func (x *Index) Neighbors(id string) []string { return x.neighbors[id] }

// Order returns router ids in declaration order (used for deterministic ties).
func (x *Index) Order() []string { return x.order }

// ASN returns the ASN of router id.
func (x *Index) ASN(id string) int { return x.Nodes[id].ASN }

// SameAS reports whether two routers are iBGP peers.
func (x *Index) SameAS(a, b string) bool {
	return x.Nodes[a].ASN == x.Nodes[b].ASN
}

// IGPCost returns the IGP cost of a node (0 for the local origin node when
// unknown callers pass the receiver, which always exists).
func (x *Index) IGPCost(id string) int {
	if n, ok := x.Nodes[id]; ok {
		return n.IGPCost
	}
	return 0
}

// Ordinal returns the declaration ordinal of a router (deterministic tie).
func (x *Index) Ordinal(id string) int {
	if v, ok := x.ByID[id]; ok {
		return v
	}
	return 1 << 30
}

// AddressOf returns the configured address of a router.
func (x *Index) AddressOf(id string) string { return x.Nodes[id].Address }

// AddrMap returns a copy of the address -> router id table.
func (x *Index) AddrMap() map[string]string {
	m := make(map[string]string, len(x.addrToID))
	for k, v := range x.addrToID {
		m[k] = v
	}
	return m
}

// ParseASN parses a decimal 32-bit ASN for fixture authoring.
func ParseASN(s string) (int, error) {
	v, err := strconv.ParseUint(s, 10, 32)
	if err != nil {
		return 0, ierr.Wrap(ierr.KindInvalidInput, "model.ParseASN", "bad asn "+s, err)
	}
	return int(v), nil
}
