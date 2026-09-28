// Package netmodel is the synthetic network model: simulated LAN segments
// and hosts. It models the *host side* of IGMPv2 (RFC 2236 §3):
//
//   - hosts keep a per-interface set of joined groups;
//   - on a General Query a member host schedules a report for each joined
//     group with a random delay in [0, MaxRespTime];
//   - if another member's report for the same group/round arrives first,
//     the pending report is cancelled (report suppression);
//   - a group-specific query elicits a delayed report in [0, LMQI].
//
// There is no real socket I/O: "transit", "loss" and "delay" are driven by
// the replay engine from fixture rules.
package netmodel

import (
	"sort"
	"sync"
)

// Host is a synthetic end host on one LAN.
type Host struct {
	Name   string
	Addr   string
	iface  string
	mu     sync.Mutex
	groups map[string]bool
}

// Join records that the host listens on group.
func (h *Host) Join(group string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.groups[group] = true
}

// Leave removes the host's membership on group.
func (h *Host) Leave(group string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	existed := h.groups[group]
	delete(h.groups, group)
	return existed
}

// Groups returns a sorted copy of the host's joined groups.
func (h *Host) Groups() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	out := make([]string, 0, len(h.groups))
	for g := range h.groups {
		out = append(out, g)
	}
	sort.Strings(out)
	return out
}

// Member is an exported view of a host on a LAN.
type Member struct {
	Name string
	Addr string
}

// LAN is one synthetic broadcast segment bound to a router interface.
type LAN struct {
	iface string
	mu    sync.Mutex
	hosts map[string]*Host
}

// Fabric holds all simulated LAN segments.
type Fabric struct {
	mu   sync.Mutex
	lans map[string]*LAN
}

// NewFabric returns an empty fabric.
func NewFabric() *Fabric {
	return &Fabric{lans: map[string]*LAN{}}
}

// AddLAN registers a LAN for iface (idempotent).
func (f *Fabric) AddLAN(iface string) *LAN {
	f.mu.Lock()
	defer f.mu.Unlock()
	lan, ok := f.lans[iface]
	if !ok {
		lan = &LAN{iface: iface, hosts: map[string]*Host{}}
		f.lans[iface] = lan
	}
	return lan
}

// Host looks up a registered host by name.
func (f *Fabric) Host(iface, name string) *Host {
	f.mu.Lock()
	defer f.mu.Unlock()
	if lan, ok := f.lans[iface]; ok {
		return lan.hosts[name]
	}
	return nil
}

// RegisterHost creates a host on iface's LAN.
func (f *Fabric) RegisterHost(iface, name, addr string) *Host {
	lan := f.AddLAN(iface)
	lan.mu.Lock()
	defer lan.mu.Unlock()
	if h, ok := lan.hosts[name]; ok {
		return h
	}
	h := &Host{Name: name, Addr: addr, iface: iface, groups: map[string]bool{}}
	lan.hosts[name] = h
	return h
}

// Members returns the reactive hosts currently joined to group on iface,
// sorted by name for deterministic scheduling.
func (f *Fabric) Members(iface, group string) []Member {
	f.mu.Lock()
	lan, ok := f.lans[iface]
	f.mu.Unlock()
	if !ok {
		return nil
	}
	lan.mu.Lock()
	defer lan.mu.Unlock()
	var out []Member
	names := make([]string, 0, len(lan.hosts))
	for n := range lan.hosts {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		h := lan.hosts[n]
		h.mu.Lock()
		joined := h.groups[group]
		h.mu.Unlock()
		if joined {
			out = append(out, Member{Name: h.Name, Addr: h.Addr})
		}
	}
	return out
}
