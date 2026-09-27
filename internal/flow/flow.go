// Package flow defines the network 5-tuple model, its canonical form and the
// hash input used by the routing core.
package flow

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"strings"

	"flexhash/internal/fherr"
)

// FiveTuple identifies a unidirectional flow.
type FiveTuple struct {
	SrcIP   string `json:"src_ip"`
	SrcPort uint16 `json:"src_port"`
	DstIP   string `json:"dst_ip"`
	DstPort uint16 `json:"dst_port"`
	// Protocol is normalized to lowercase canonical names: "tcp", "udp", "icmp".
	Protocol string `json:"protocol"`
}

// CanonicalProtocol normalizes the common aliases. Numeric IP protocol
// numbers ("6"/"17"/"1") are accepted; anything else is an input error.
func CanonicalProtocol(p string) (string, error) {
	switch strings.ToLower(strings.TrimSpace(p)) {
	case "tcp", "6":
		return "tcp", nil
	case "udp", "17":
		return "udp", nil
	case "icmp", "1":
		return "icmp", nil
	default:
		return "", fherr.New(fherr.KindInput, "flow.CanonicalProtocol",
			"unsupported protocol (want tcp/udp/icmp or 6/17/1): "+p)
	}
}

// Validate normalizes the tuple in place and checks both IP literals.
func (f *FiveTuple) Validate() error {
	const op = "flow.Validate"
	if f == nil {
		return fherr.New(fherr.KindInput, op, "nil tuple")
	}
	proto, err := CanonicalProtocol(f.Protocol)
	if err != nil {
		return err
	}
	f.Protocol = proto
	if _, err := netip.ParseAddr(f.SrcIP); err != nil {
		return fherr.Wrap(fherr.KindInput, op, "invalid src_ip: "+f.SrcIP, err)
	}
	if _, err := netip.ParseAddr(f.DstIP); err != nil {
		return fherr.Wrap(fherr.KindInput, op, "invalid dst_ip: "+f.DstIP, err)
	}
	// Port zero is legal for ICMP (unused) but for tcp/udp a zero ephemeral
	// port is still a structurally valid tuple; we do not invent semantics.
	return nil
}

// Canonical renders the tuple as the exact byte sequence hashed.
// The tuple must have been validated first.
func (f *FiveTuple) Canonical() string {
	return fmt.Sprintf("%s|%d|%s|%d|%s",
		f.SrcIP, f.SrcPort, f.DstIP, f.DstPort, f.Protocol)
}

// Key returns the routing key (canonical tuple) after validating.
func (f *FiveTuple) Key() (string, error) {
	if err := f.Validate(); err != nil {
		return "", err
	}
	return f.Canonical(), nil
}

// DecodeRequest parses one lookup request body and validates the tuple.
func DecodeRequest(b []byte) (*FiveTuple, error) {
	var t FiveTuple
	if err := json.Unmarshal(b, &t); err != nil {
		return nil, fherr.Wrap(fherr.KindInput, "flow.DecodeRequest", "invalid JSON: "+err.Error(), err)
	}
	if err := t.Validate(); err != nil {
		return nil, err
	}
	return &t, nil
}
