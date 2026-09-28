package engine

import (
	"fmt"

	"pvsim/model"
)

func ptrAttrs(a model.Attrs) *model.Attrs {
	c := a.Clone()
	return &c
}

func maybeAttrs(c *Candidate) *model.Attrs {
	if c == nil {
		return nil
	}
	return ptrAttrs(c.Attrs)
}

func prevAttrsOrNil(k ribKey, bucket map[string]*Candidate, prevPeer string) *model.Attrs {
	if prevPeer == "" {
		return nil
	}
	if c := bucket[prevPeer]; c != nil {
		return ptrAttrs(c.Attrs)
	}
	return nil
}

func detail(format string, args ...any) string { return fmt.Sprintf(format, args...) }

func peerName(c *Candidate) string {
	if c == nil {
		return ""
	}
	return c.Peer
}

func peerOrNone(c *Candidate) string {
	if c == nil {
		return "<none>"
	}
	return c.Peer
}

func prevOrNone(p string) string {
	if p == "" {
		return "<none>"
	}
	return p
}

func ruleName(r string) string {
	if r == "" {
		return "<default-permit>"
	}
	return r
}

func ruleSuffix(r string) string {
	if r == "" {
		return ""
	}
	return " [rule=" + r + "]"
}

// attrsEqual compares the attributes relevant to propagation.
func attrsEqual(a, b model.Attrs) bool {
	if a.LocalPrefOr(100) != b.LocalPrefOr(100) {
		return false
	}
	if a.MedOr() != b.MedOr() || a.Origin != b.Origin {
		return false
	}
	if len(a.ASPath) != len(b.ASPath) {
		return false
	}
	for i := range a.ASPath {
		if a.ASPath[i] != b.ASPath[i] {
			return false
		}
	}
	return true
}
