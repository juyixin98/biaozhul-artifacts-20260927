// Package replay drives the reassembler from offline inputs (PCAP files
// or explicit JSON fragment lists) and produces a deterministic,
// machine-checkable report. It never sends network traffic.
package replay

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"net/netip"
	"runtime"
	"time"

	"ipreasm/internal/ipv4"
	"ipreasm/internal/pcap"
	"ipreasm/internal/reasm"
)

// InsertRecord logs the decision taken for one fragment.
type InsertRecord struct {
	TS          time.Time `json:"ts"`
	Key         string    `json:"key"`
	OffsetBytes int       `json:"offset_bytes"`
	Len         int       `json:"len"`
	More        bool      `json:"more"`
	Outcome     string    `json:"outcome"`
	Reason      string    `json:"reason,omitempty"`
}

// CompletedRecord describes one reassembled datagram.
type CompletedRecord struct {
	Key        string `json:"key"`
	SHA256     string `json:"sha256"`
	Length     int    `json:"length"`
	FragCount  int    `json:"frag_count"`
	Duplicates int    `json:"duplicate_fragments"`
	PayloadB64 string `json:"payload_b64"`
}

// Report is the full, deterministic outcome of one replay run.
type Report struct {
	RunID        string            `json:"run_id"`
	GoVersion    string            `json:"go_version"`
	StartedAt    time.Time         `json:"started_at"`
	Timeout      string            `json:"timeout"`
	Packets      int               `json:"packets"`
	NonIPv4      int               `json:"non_ipv4"`
	Unfragmented int               `json:"unfragmented"`
	Fragments    int               `json:"fragments"`
	Inserts      []InsertRecord    `json:"inserts"`
	Completed    []CompletedRecord `json:"completed"`
	Expired      []string          `json:"expired"`
	dupCount     map[string]int
	Stats        reasm.Stats `json:"stats"`
}

// Engine replays inputs through a reassembler.
type Engine struct {
	Cfg   reasm.Config
	RunID string
	Sink  reasm.Sink // may be nil
}

// RunPCAP replays every record of a pcap stream. The record timestamps
// drive the reassembly clock, so timeouts are fully deterministic.
func (e *Engine) RunPCAP(r io.Reader) (*Report, error) {
	pr, err := pcap.NewReader(r)
	if err != nil {
		return nil, fmt.Errorf("pcap open: %w", err)
	}
	rep := &Report{
		RunID:     e.RunID,
		GoVersion: runtime.Version(),
		StartedAt: time.Now().UTC(),
		Timeout:   e.Cfg.Timeout.String(),
		dupCount:  make(map[string]int),
	}
	rs := reasm.New(e.Cfg, e.RunID, e.Sink)
	for {
		rec, err := pr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return rep, fmt.Errorf("pcap record %d: %w", rep.Packets, err)
		}
		rep.Packets++
		pkt, err := pcap.ExtractIPv4(pr.LinkType, rec.Data)
		if err != nil {
			rep.NonIPv4++
			continue
		}
		if !pkt.Fragmented() {
			// Whole datagrams are passed through, never reassembled; IP
			// reassembly is not conflated with any transport-layer stream.
			rep.Unfragmented++
			continue
		}
		// Timeout is evaluated against the capture clock BEFORE accepting
		// the new fragment, so an identification field reused after the
		// timeout starts a fresh group even when the first new fragment
		// shares the old group's offset.
		for _, k := range rs.Sweep(rec.TS) {
			rep.Expired = append(rep.Expired, k.String())
		}
		rep.Fragments++
		e.insert(rs, rep, pkt, rec.TS)
	}
	// Final sweep at +inf is deliberately NOT done: groups that never
	// complete and never time out within the capture are reported as
	// still-active in Stats, not silently expired.
	rep.Stats = rs.Stats()
	return rep, nil
}

// FragInput is one JSON-injected fragment.
type FragInput struct {
	Src         string    `json:"src"`
	Dst         string    `json:"dst"`
	Proto       uint8     `json:"proto"`
	ID          uint16    `json:"id"`
	OffsetUnits uint16    `json:"offset_units"` // 8-byte units, as on the wire
	More        bool      `json:"more"`
	PayloadB64  string    `json:"payload_b64"`
	TS          time.Time `json:"ts"`
}

// RunFragments replays an explicit fragment list (HTTP JSON mode).
func (e *Engine) RunFragments(inputs []FragInput) (*Report, error) {
	rep := &Report{
		RunID:     e.RunID,
		GoVersion: runtime.Version(),
		StartedAt: time.Now().UTC(),
		Timeout:   e.Cfg.Timeout.String(),
		dupCount:  make(map[string]int),
	}
	rs := reasm.New(e.Cfg, e.RunID, e.Sink)
	for i, in := range inputs {
		src, err := parseAddr(in.Src)
		if err != nil {
			return rep, fmt.Errorf("fragment %d: src: %w", i, err)
		}
		dst, err := parseAddr(in.Dst)
		if err != nil {
			return rep, fmt.Errorf("fragment %d: dst: %w", i, err)
		}
		payload, err := base64.StdEncoding.DecodeString(in.PayloadB64)
		if err != nil {
			return rep, fmt.Errorf("fragment %d: payload_b64: %w", i, err)
		}
		if in.OffsetUnits > 0x1fff {
			return rep, fmt.Errorf("fragment %d: offset_units %d exceeds 13-bit field", i, in.OffsetUnits)
		}
		rep.Packets++
		rep.Fragments++
		pkt := &ipv4.Packet{
			Src: src, Dst: dst, Protocol: in.Proto, ID: in.ID,
			FragOffsetUnits: in.OffsetUnits, MoreFragments: in.More, Payload: payload,
		}
		ts := in.TS
		if ts.IsZero() {
			ts = time.Now().UTC()
		}
		for _, k := range rs.Sweep(ts) {
			rep.Expired = append(rep.Expired, k.String())
		}
		e.insert(rs, rep, pkt, ts)
	}
	rep.Stats = rs.Stats()
	return rep, nil
}

func (e *Engine) insert(rs *reasm.Reassembler, rep *Report, pkt *ipv4.Packet, ts time.Time) {
	frag := reasm.Fragment{
		Key:         reasm.Key{Src: pkt.Src, Dst: pkt.Dst, Proto: pkt.Protocol, ID: pkt.ID},
		OffsetBytes: pkt.OffsetBytes(),
		More:        pkt.MoreFragments,
		Data:        pkt.Payload,
	}
	res := rs.Insert(frag, ts)
	rec := InsertRecord{
		TS: ts, Key: frag.Key.String(), OffsetBytes: frag.OffsetBytes,
		Len: len(frag.Data), More: frag.More, Outcome: res.Outcome.String(),
	}
	if res.Outcome == reasm.OutcomeRejected {
		rec.Reason = string(res.Reason)
	}
	rep.Inserts = append(rep.Inserts, rec)
	if res.Outcome == reasm.OutcomeDuplicate {
		rep.dupCount[frag.Key.String()]++
	}
	if res.Outcome == reasm.OutcomeCompleted && res.Datagram != nil {
		sum := sha256.Sum256(res.Datagram.Data)
		rep.Completed = append(rep.Completed, CompletedRecord{
			Key:        res.Datagram.Key.String(),
			SHA256:     hex.EncodeToString(sum[:]),
			Length:     len(res.Datagram.Data),
			FragCount:  res.Datagram.FragCount,
			Duplicates: rep.dupCount[frag.Key.String()],
			PayloadB64: base64.StdEncoding.EncodeToString(res.Datagram.Data),
		})
	}
}

func parseAddr(s string) (netip.Addr, error) {
	a, err := netip.ParseAddr(s)
	if err != nil {
		return netip.Addr{}, err
	}
	if !a.Is4() {
		return netip.Addr{}, fmt.Errorf("%s is not an IPv4 address", s)
	}
	return a, nil
}
