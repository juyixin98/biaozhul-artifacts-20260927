// Package reasm implements IPv4 fragment reassembly for offline PCAP
// replay. It sends no network traffic: fragments arrive from parsed PCAP
// records (or explicit JSON injection) and completed datagrams are
// returned to the caller.
//
// Algorithm contract (see README for the full statement):
//   - Group key: (src, dst, protocol, identification), plus an explicit
//     idle timeout after which the group is expired and its key may be
//     reused by a new, independent datagram.
//   - Fragment offsets are 8-byte units on the wire; the engine works in
//     byte offsets internally.
//   - Non-final fragments must carry a payload length that is a multiple
//     of 8 (RFC 791); offset*8+len must not exceed MaxDatagramSize.
//   - Overlap policy: any byte-range conflict between non-identical
//     fragments rejects the WHOLE group (the group is poisoned until it
//     times out). Byte-identical retransmissions are recognised
//     separately as duplicates and ignored.
//   - A datagram is emitted only when the final fragment is known AND
//     coverage of [0, total) is contiguous. Last-fragment-first or gaps
//     never produce early output.
package reasm

import (
	"bytes"
	"fmt"
	"net/netip"
	"sort"
	"time"
)

// Key identifies one reassembly group.
type Key struct {
	Src, Dst netip.Addr
	Proto    uint8
	ID       uint16
}

func (k Key) String() string {
	return fmt.Sprintf("%s>%s p=%d id=0x%04x", k.Src, k.Dst, k.Proto, k.ID)
}

// Fragment is one IPv4 fragment presented to the reassembler.
type Fragment struct {
	Key
	OffsetBytes int // wire offset field already multiplied by 8
	More        bool
	Data        []byte
}

// Outcome classifies the result of one Insert.
type Outcome int

const (
	OutcomeStored    Outcome = iota // fragment buffered, group incomplete
	OutcomeDuplicate                // byte-identical retransmission, ignored
	OutcomeCompleted                // datagram assembled and emitted
	OutcomeRejected                 // group rejected; Reason says why
)

func (o Outcome) String() string {
	switch o {
	case OutcomeStored:
		return "stored"
	case OutcomeDuplicate:
		return "duplicate"
	case OutcomeCompleted:
		return "completed"
	case OutcomeRejected:
		return "rejected"
	}
	return "unknown"
}

// RejectReason enumerates the failure categories. Tests assert on these
// exact values; do not rename without updating the contract docs.
type RejectReason string

const (
	ReasonNone            RejectReason = ""
	ReasonOverlap         RejectReason = "overlap"          // byte-range conflict between distinct fragments
	ReasonConflictingLast RejectReason = "conflicting-last" // disagreeing total lengths / MF inconsistency
	ReasonBadLength       RejectReason = "bad-length"       // non-final fragment not a multiple of 8, or empty non-final
	ReasonOversize        RejectReason = "oversize"         // offset*8+len exceeds MaxDatagramSize
	ReasonCapacity        RejectReason = "capacity"         // group/byte budget exhausted
	ReasonPoisoned        RejectReason = "poisoned-group"   // group already rejected; late fragment dropped
)

// Datagram is a successfully reassembled IPv4 payload.
type Datagram struct {
	Key
	Data      []byte
	FragCount int
	Completed time.Time
}

// Result is the outcome of one Insert call.
type Result struct {
	Outcome  Outcome
	Reason   RejectReason // set iff Outcome == OutcomeRejected
	Datagram *Datagram    // set iff Outcome == OutcomeCompleted
}

// Event categories written to the audit sink.
const (
	EvStored    = "stored"
	EvDuplicate = "duplicate"
	EvCompleted = "completed"
	EvRejected  = "rejected"
	EvExpired   = "expired"
)

// Event is one audit-log entry, correlated by RunID and a sequence number.
type Event struct {
	RunID    string
	Seq      int64
	TS       time.Time
	Category string
	Key      Key
	Detail   string
}

// Sink receives state transitions so they can be persisted (SQLite) and
// audited. Implementations must be safe for single-threaded use by the
// reassembler. A nil Sink is valid and discards everything.
type Sink interface {
	// OnFragmentStored persists one accepted fragment.
	OnFragmentStored(ev Event, f Fragment)
	// OnGroupDropped drops all persisted fragment state of a group
	// (completion, rejection, or expiry). This is the resource-reclaim hook.
	OnGroupDropped(ev Event, k Key)
	// OnDatagram persists one completed datagram.
	OnDatagram(ev Event, d Datagram)
	// OnEvent records any remaining event (duplicates, expiries).
	OnEvent(ev Event)
}

// Config carries the engine tunables (mirrors the config package without
// importing it, keeping the dependency direction one-way).
type Config struct {
	Timeout          time.Duration
	MaxDatagramSize  int
	MaxDatagrams     int
	MaxBufferedBytes int64
}

// Stats are cumulative counters for one Reassembler (one run).
type Stats struct {
	FragmentsReceived int64
	Duplicates        int64
	Completed         int64
	Rejected          int64
	Expired           int64
	ActiveGroups      int
	BufferedBytes     int64
}

type slot struct {
	data []byte
	more bool
}

type group struct {
	frags     map[int]slot // byte offset -> fragment
	totalLen  int          // -1 while the final fragment is unknown
	buffered  int
	firstSeen time.Time
	lastSeen  time.Time
	rejected  RejectReason // tombstone: non-empty once the group is poisoned
}

// Reassembler holds the in-flight reassembly state of one replay run.
// It is not safe for concurrent use; the replay engine is single-threaded.
type Reassembler struct {
	cfg      Config
	runID    string
	sink     Sink
	groups   map[Key]*group
	buffered int64
	seq      int64
	stats    Stats
}

// New creates a reassembler. runID correlates every emitted event.
func New(cfg Config, runID string, sink Sink) *Reassembler {
	return &Reassembler{cfg: cfg, runID: runID, sink: sink, groups: make(map[Key]*group)}
}

// Stats returns a snapshot of the counters.
func (r *Reassembler) Stats() Stats {
	s := r.stats
	s.ActiveGroups = len(r.groups)
	s.BufferedBytes = r.buffered
	return s
}

func (r *Reassembler) emit(ts time.Time, cat string, k Key, detail string) Event {
	r.seq++
	return Event{RunID: r.runID, Seq: r.seq, TS: ts, Category: cat, Key: k, Detail: detail}
}

// Insert feeds one fragment observed at time now. The caller supplies the
// clock (PCAP record timestamps during replay) so timeouts are
// deterministic and reproducible offline.
func (r *Reassembler) Insert(f Fragment, now time.Time) Result {
	r.stats.FragmentsReceived++
	k := f.Key

	// --- stateless validation -------------------------------------------
	end := f.OffsetBytes + len(f.Data)
	if end > r.cfg.MaxDatagramSize {
		return r.reject(k, now, ReasonOversize,
			fmt.Sprintf("offset=%d len=%d end=%d > max=%d", f.OffsetBytes, len(f.Data), end, r.cfg.MaxDatagramSize))
	}
	if f.More && len(f.Data)%8 != 0 {
		return r.reject(k, now, ReasonBadLength,
			fmt.Sprintf("non-final fragment len=%d not a multiple of 8", len(f.Data)))
	}
	if f.More && len(f.Data) == 0 {
		return r.reject(k, now, ReasonBadLength, "empty non-final fragment")
	}

	g, ok := r.groups[k]
	if ok && g.rejected != ReasonNone {
		g.lastSeen = now
		r.stats.Rejected++
		ev := r.emit(now, EvRejected, k, fmt.Sprintf("reason=%s (late fragment dropped)", ReasonPoisoned))
		r.sinkEvent(ev)
		return Result{Outcome: OutcomeRejected, Reason: ReasonPoisoned}
	}
	if !ok {
		if len(r.groups) >= r.cfg.MaxDatagrams {
			ev := r.emit(now, EvRejected, k, fmt.Sprintf("reason=%s groups=%d", ReasonCapacity, len(r.groups)))
			r.sinkEvent(ev)
			r.stats.Rejected++
			return Result{Outcome: OutcomeRejected, Reason: ReasonCapacity}
		}
		g = &group{frags: make(map[int]slot), totalLen: -1, firstSeen: now, lastSeen: now}
		r.groups[k] = g
	}

	// --- final-fragment bookkeeping -------------------------------------
	if !f.More {
		if g.totalLen >= 0 && g.totalLen != end {
			return r.rejectGroup(g, k, now, ReasonConflictingLast,
				fmt.Sprintf("total %d conflicts with known total %d", end, g.totalLen))
		}
		for off, s := range g.frags {
			if off+len(s.data) > end {
				return r.rejectGroup(g, k, now, ReasonConflictingLast,
					fmt.Sprintf("existing fragment [%d,%d) extends beyond announced total %d", off, off+len(s.data), end))
			}
		}
		g.totalLen = end
	} else if g.totalLen >= 0 && end > g.totalLen {
		return r.rejectGroup(g, k, now, ReasonConflictingLast,
			fmt.Sprintf("non-final fragment end=%d beyond known total %d", end, g.totalLen))
	}

	// --- overlap / exact-duplicate classification -----------------------
	for off, s := range g.frags {
		oEnd := off + len(s.data)
		if f.OffsetBytes >= oEnd || off >= end {
			continue // disjoint
		}
		if off == f.OffsetBytes && len(s.data) == len(f.Data) && bytes.Equal(s.data, f.Data) {
			if s.more == f.More {
				g.lastSeen = now
				r.stats.Duplicates++
				r.sinkEvent(r.emit(now, EvDuplicate, k,
					fmt.Sprintf("offset=%d len=%d identical retransmission", f.OffsetBytes, len(f.Data))))
				return Result{Outcome: OutcomeDuplicate}
			}
			return r.rejectGroup(g, k, now, ReasonConflictingLast,
				fmt.Sprintf("identical bytes at offset=%d but MF flag differs", off))
		}
		return r.rejectGroup(g, k, now, ReasonOverlap,
			fmt.Sprintf("new [%d,%d) conflicts with buffered [%d,%d)", f.OffsetBytes, end, off, oEnd))
	}

	// --- capacity guard ---------------------------------------------------
	if r.buffered+int64(len(f.Data)) > r.cfg.MaxBufferedBytes {
		return r.rejectGroup(g, k, now, ReasonCapacity,
			fmt.Sprintf("buffered=%d + len=%d > max=%d", r.buffered, len(f.Data), r.cfg.MaxBufferedBytes))
	}

	// --- accept -----------------------------------------------------------
	data := make([]byte, len(f.Data))
	copy(data, f.Data)
	g.frags[f.OffsetBytes] = slot{data: data, more: f.More}
	g.buffered += len(data)
	g.lastSeen = now
	r.buffered += int64(len(data))
	storedEv := r.emit(now, EvStored, k, fmt.Sprintf("offset=%d len=%d more=%v total=%d", f.OffsetBytes, len(data), f.More, g.totalLen))
	if r.sink != nil {
		r.sink.OnFragmentStored(storedEv, Fragment{Key: k, OffsetBytes: f.OffsetBytes, More: f.More, Data: data})
	}

	// --- completion check: contiguous [0, totalLen) ----------------------
	if g.totalLen >= 0 && covered(g) {
		return r.complete(g, k, now)
	}
	return Result{Outcome: OutcomeStored}
}

// covered reports whether the buffered fragments tile [0, totalLen)
// without gaps. Overlaps cannot exist here (they reject the group), so a
// simple ordered walk suffices.
func covered(g *group) bool {
	offs := make([]int, 0, len(g.frags))
	for off := range g.frags {
		offs = append(offs, off)
	}
	sort.Ints(offs)
	pos := 0
	for _, off := range offs {
		if off > pos {
			return false
		}
		if e := off + len(g.frags[off].data); e > pos {
			pos = e
		}
	}
	return pos == g.totalLen
}

// complete assembles the datagram, reclaims group state and reports it.
func (r *Reassembler) complete(g *group, k Key, now time.Time) Result {
	out := make([]byte, 0, g.totalLen)
	offs := make([]int, 0, len(g.frags))
	for off := range g.frags {
		offs = append(offs, off)
	}
	sort.Ints(offs)
	for _, off := range offs {
		out = append(out, g.frags[off].data...)
	}
	d := Datagram{Key: k, Data: out, FragCount: len(g.frags), Completed: now}
	r.dropGroup(g, k)
	r.stats.Completed++
	ev := r.emit(now, EvCompleted, k, fmt.Sprintf("total=%d frags=%d", len(out), d.FragCount))
	if r.sink != nil {
		r.sink.OnDatagram(ev, d)
		r.sink.OnGroupDropped(ev, k)
	}
	return Result{Outcome: OutcomeCompleted, Datagram: &d}
}

// reject handles a stateless rejection (no group exists yet or the
// violation is visible without group state). It still poisons the group so
// later fragments of the same key cannot resurrect it.
func (r *Reassembler) reject(k Key, now time.Time, reason RejectReason, detail string) Result {
	g, ok := r.groups[k]
	if !ok {
		if len(r.groups) < r.cfg.MaxDatagrams {
			g = &group{frags: make(map[int]slot), totalLen: -1, firstSeen: now, lastSeen: now, rejected: reason}
			r.groups[k] = g
		}
	} else {
		r.dropFragments(g, k)
		g.rejected = reason
		g.lastSeen = now
	}
	r.stats.Rejected++
	ev := r.emit(now, EvRejected, k, fmt.Sprintf("reason=%s %s", reason, detail))
	r.sinkEvent(ev)
	if r.sink != nil {
		r.sink.OnGroupDropped(ev, k)
	}
	return Result{Outcome: OutcomeRejected, Reason: reason}
}

// rejectGroup poisons an existing group and reclaims its buffers.
func (r *Reassembler) rejectGroup(g *group, k Key, now time.Time, reason RejectReason, detail string) Result {
	r.dropFragments(g, k)
	g.rejected = reason
	g.lastSeen = now
	r.stats.Rejected++
	ev := r.emit(now, EvRejected, k, fmt.Sprintf("reason=%s %s", reason, detail))
	r.sinkEvent(ev)
	if r.sink != nil {
		r.sink.OnGroupDropped(ev, k)
	}
	return Result{Outcome: OutcomeRejected, Reason: reason}
}

// dropFragments releases the buffered payload of a group but keeps the
// (possibly tombstoned) group entry.
func (r *Reassembler) dropFragments(g *group, k Key) {
	r.buffered -= int64(g.buffered)
	g.buffered = 0
	g.frags = make(map[int]slot)
}

// dropGroup removes a group entirely (completion path).
func (r *Reassembler) dropGroup(g *group, k Key) {
	r.dropFragments(g, k)
	delete(r.groups, k)
}

func (r *Reassembler) sinkEvent(ev Event) {
	if r.sink != nil {
		r.sink.OnEvent(ev)
	}
}

// Sweep expires groups that have been idle for longer than the configured
// timeout and reclaims their state. It returns the expired keys so the
// replay layer can report them. Expiry is the ONLY way a tombstoned
// (rejected) group is forgotten, which bounds tombstone lifetime and makes
// identification-field reuse safe: a reused key after expiry starts a
// fresh, independent group.
func (r *Reassembler) Sweep(now time.Time) []Key {
	var expired []Key
	for k, g := range r.groups {
		if now.Sub(g.lastSeen) <= r.cfg.Timeout {
			continue
		}
		detail := fmt.Sprintf("idle>%s frags_buffered=%d total_known=%v", r.cfg.Timeout, g.buffered, g.totalLen >= 0)
		if g.rejected != ReasonNone {
			detail = fmt.Sprintf("tombstone reason=%s purged after %s", g.rejected, r.cfg.Timeout)
		}
		r.dropGroup(g, k)
		r.stats.Expired++
		expired = append(expired, k)
		ev := r.emit(now, EvExpired, k, detail)
		r.sinkEvent(ev)
		if r.sink != nil {
			r.sink.OnGroupDropped(ev, k)
		}
	}
	sort.Slice(expired, func(i, j int) bool { return expired[i].String() < expired[j].String() })
	return expired
}
