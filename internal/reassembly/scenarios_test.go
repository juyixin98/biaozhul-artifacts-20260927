package reassembly_test

import (
	"strings"
	"testing"

	"tcpreplay/internal/netmodel"
	"tcpreplay/internal/reassembly"
	"tcpreplay/internal/testsupport"
)

// Known original streams, hand-authored. Every expected output is a slice of
// these constants; none is produced by the reassembly engine.
var (
	clientOriginal = "HELLO-TCP-REASSEMBLY-WORLD!!" // 28 bytes
	serverOriginal = "ACK-DATA-FROM-SERVER-BYE"     // 24 bytes
)

// run feeds packets in order into a manager and returns emitted events.
func run(mgr *reassembly.Manager, pkts []netmodel.Packet) []reassembly.Event {
	var events []reassembly.Event
	for i, p := range pkts {
		rid := p.RecordID
		if rid == "" {
			rid = "rec"
		}
		res := mgr.Process(p, "t", rid+"-"+itoa(i))
		events = append(events, res.Events...)
	}
	return events
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b [12]byte
	pos := len(b)
	for i > 0 {
		pos--
		b[pos] = byte('0' + i%10)
		i /= 10
	}
	return string(b[pos:])
}

func onlyView(t *testing.T, mgr *reassembly.Manager) reassembly.GenerationView {
	t.Helper()
	views := mgr.AllViews()
	if len(views) != 1 {
		t.Fatalf("expected 1 generation, got %d: %+v", len(views), views)
	}
	return views[0]
}

func viewN(t *testing.T, mgr *reassembly.Manager, n int) []reassembly.GenerationView {
	t.Helper()
	views := mgr.AllViews()
	if len(views) != n {
		t.Fatalf("expected %d generations, got %d", n, len(views))
	}
	return views
}

func checkDir(t *testing.T, label string, v reassembly.DirectionView, want testsupport.ExpectedDirection) {
	t.Helper()
	for _, m := range testsupport.CheckDirection(v, want) {
		t.Errorf("%s: %s", label, m)
	}
}

// TestOutOfOrderWithGaps feeds segments shuffled and asserts the exact stream
// and gap/held state at every intermediate step; missing bytes are never
// fabricated.
func TestOutOfOrderWithGaps(t *testing.T) {
	b := testsupport.NewBuilder(1000, 5000).Handshake()
	mgr := reassembly.NewManager(reassembly.PolicyFirstWins, false)
	run(mgr, b.Packets())

	b.ClientData(12, []byte(clientOriginal[12:20]))
	run(mgr, b.Packets()[len(b.Packets())-1:])
	v := onlyView(t, mgr)
	checkDir(t, "after[12:20] a_to_b", v.AtoB, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte(""),
		Gaps:           []reassembly.Gap{{Start: 0, End: 12}},
		HeldRunsData:   [][]byte{[]byte("ASSEMBLY")},
	})

	b.ClientData(0, []byte(clientOriginal[0:6]))
	run(mgr, b.Packets()[len(b.Packets())-1:])
	v = onlyView(t, mgr)
	checkDir(t, "after[0:6] a_to_b", v.AtoB, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte("HELLO-"),
		Gaps:           []reassembly.Gap{{Start: 6, End: 12}},
		HeldRunsData:   [][]byte{[]byte("ASSEMBLY")},
	})

	b.ClientData(20, []byte(clientOriginal[20:28]))
	run(mgr, b.Packets()[len(b.Packets())-1:])
	v = onlyView(t, mgr)
	// [12,20) and [20,28) are adjacent and merge into one held run.
	checkDir(t, "after[20:28] a_to_b", v.AtoB, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte("HELLO-"),
		Gaps:           []reassembly.Gap{{Start: 6, End: 12}},
		HeldRunsData:   [][]byte{[]byte("ASSEMBLY-WORLD!!")},
	})

	b.ClientData(6, []byte(clientOriginal[6:12]))
	run(mgr, b.Packets()[len(b.Packets())-1:])
	b.ClientFIN(28, nil)
	b.ServerFIN(0, nil)
	run(mgr, b.Packets()[len(b.Packets())-2:])
	v = onlyView(t, mgr)
	checkDir(t, "complete a_to_b", v.AtoB, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte(clientOriginal),
		Gaps:           nil,
		HeldRunsData:   nil,
		FINSeen:        true,
		FINPos:         28,
		LengthProved:   28,
	})
	checkDir(t, "b_to_a", v.BtoA, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte(""),
		FINSeen:        true,
		FINPos:         0,
		LengthProved:   0,
	})
	if !v.Closed {
		t.Errorf("generation should be closed after both FINs")
	}
}

// TestRetransmissionNoDuplicateOutput: byte-identical retransmissions (full
// and partially overlapping) must never duplicate output.
func TestRetransmissionNoDuplicateOutput(t *testing.T) {
	b := testsupport.NewBuilder(1000, 5000).Handshake()
	b.ClientData(0, []byte(clientOriginal[0:6]))
	b.ClientData(0, []byte(clientOriginal[0:6]))    // exact retransmission
	b.ClientData(6, []byte(clientOriginal[6:12]))   // "TCP-RE"
	b.ClientData(3, []byte(clientOriginal[3:11]))   // identical overlap [3,11)
	b.ClientData(12, []byte(clientOriginal[12:20])) // "ASSEMBLY"
	b.ClientData(20, []byte(clientOriginal[20:28])) // "-WORLD!!"
	b.ClientFIN(28, nil)
	b.ServerFIN(0, nil)

	mgr := reassembly.NewManager(reassembly.PolicyFirstWins, false)
	events := run(mgr, b.Packets())
	v := onlyView(t, mgr)
	checkDir(t, "a_to_b", v.AtoB, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte(clientOriginal), Gaps: nil, FINSeen: true,
		FINPos: 28, LengthProved: 28,
	})
	if confs := mgr.Conflicts(); len(confs) != 0 {
		t.Fatalf("identical retransmission produced %d conflicts: %+v", len(confs), confs)
	}
	if !testsupport.FindEvent(events, reassembly.EvRetransmitIdent, reassembly.LevelInfo) {
		t.Errorf("expected RETRANSMIT_IDENTICAL event")
	}
}

// TestOverlapPolicies verifies conflict isolation byte-for-byte under all
// three declared policies, in both temporal orderings (original first and
// corrupted segment first).
func TestOverlapPolicies(t *testing.T) {
	orig := []byte(clientOriginal)
	evil := []byte("zzzzzzzz") // replaces offsets 10..17 ("REASSEMB")
	patched := []byte("HELLO-TCP-zzzzzzzzLY-WORLD!!")

	coords := make([]int64, 8)
	for i := range coords {
		coords[i] = int64(10 + i) // coordinate of stream offset 10+i (SYN at 0)
	}

	wantConfs := func(disp string, acceptedFirst bool) []testsupport.ExpectConflict {
		out := make([]testsupport.ExpectConflict, 8)
		for i := range out {
			a, o := orig[10+i], evil[i]
			if !acceptedFirst {
				a, o = evil[i], orig[10+i]
			}
			out[i] = testsupport.ExpectConflict{
				Generation: 0, Direction: "a_to_b", ByteOffset: coords[i],
				Accepted: a, Offered: o, Disposition: disp,
			}
		}
		return out
	}

	t.Run("original_first", func(t *testing.T) {
		cases := []struct {
			policy      reassembly.OverlapPolicy
			disp        string
			wantStream  []byte
			quarantined int
		}{
			{reassembly.PolicyFirstWins, reassembly.DispRejected, orig, 0},
			{reassembly.PolicyLastWins, reassembly.DispReplaced, patched, 0},
			{reassembly.PolicyQuarantine, reassembly.DispQuarantined, orig, 8},
		}
		for _, tc := range cases {
			t.Run(string(tc.policy), func(t *testing.T) {
				b := testsupport.NewBuilder(1000, 5000).Handshake()
				b.ClientData(0, orig[:10])
				b.ClientData(10, orig[10:])
				b.ClientDataID(10, evil, "evil-seg")
				b.ClientFIN(28, nil)
				b.ServerFIN(0, nil)
				mgr := reassembly.NewManager(tc.policy, false)
				events := run(mgr, b.Packets())
				v := onlyView(t, mgr)
				checkDir(t, "a_to_b", v.AtoB, testsupport.ExpectedDirection{
					HandshakeKnown: true,
					Stream:         tc.wantStream, FINSeen: true, FINPos: 28,
					LengthProved: 28, QuarantineBytes: tc.quarantined,
				})
				if ms := testsupport.CheckConflicts(mgr.Conflicts(), wantConfs(tc.disp, true)); len(ms) > 0 {
					for _, m := range ms {
						t.Error(m)
					}
				}
				if !testsupport.FindEvent(events, reassembly.EvOverlapConflict, reassembly.LevelWarn) {
					t.Errorf("expected OVERLAP_CONFLICT warn event")
				}
			})
		}
	})

	t.Run("corrupt_first", func(t *testing.T) {
		cases := []struct {
			policy      reassembly.OverlapPolicy
			disp        string
			wantStream  []byte
			quarantined int
		}{
			{reassembly.PolicyFirstWins, reassembly.DispRejected, patched, 0},
			{reassembly.PolicyLastWins, reassembly.DispReplaced, orig, 0},
			{reassembly.PolicyQuarantine, reassembly.DispQuarantined, patched, 8},
		}
		for _, tc := range cases {
			t.Run(string(tc.policy), func(t *testing.T) {
				b := testsupport.NewBuilder(1000, 5000).Handshake()
				b.ClientDataID(10, evil, "evil-seg") // new bytes: accepted initially
				b.ClientData(0, orig[:10])
				b.ClientData(10, orig[10:]) // conflicts on the 8 shared bytes
				b.ClientFIN(28, nil)
				b.ServerFIN(0, nil)
				mgr := reassembly.NewManager(tc.policy, false)
				run(mgr, b.Packets())
				v := onlyView(t, mgr)
				checkDir(t, "a_to_b", v.AtoB, testsupport.ExpectedDirection{
					HandshakeKnown: true,
					Stream:         tc.wantStream, FINSeen: true, FINPos: 28,
					LengthProved: 28, QuarantineBytes: tc.quarantined,
				})
				if ms := testsupport.CheckConflicts(mgr.Conflicts(), wantConfs(tc.disp, false)); len(ms) > 0 {
					for _, m := range ms {
						t.Error(m)
					}
				}
			})
		}
	})
}

// TestSequenceWrap exercises data straddling the 32-bit boundary. ISN is
// 0xFFFFFFF6: the first data byte is 0xFFFFFFF7 and the stream wraps to 0.
func TestSequenceWrap(t *testing.T) {
	const isn uint32 = 0xFFFFFFF6
	// Arithmetic authored directly against RFC semantics: data at offset o has
	// raw seq isn+1+o (mod 2^32); FIN is at isn+1+28.
	if got := netmodel.SeqAdd(isn, 1+18); got != 0x00000009 {
		t.Fatalf("offset18 raw seq = 0x%08x, want 0x00000009", got)
	}
	if got := netmodel.SeqAdd(isn, 1+28); got != 0x00000013 {
		t.Fatalf("FIN raw seq = 0x%08x, want 0x00000013", got)
	}

	b := testsupport.NewBuilder(isn, 7000).Handshake()
	// Deliberately shuffled so mapper anchors are non-adjacent across the wrap.
	b.ClientData(6, []byte(clientOriginal[6:18]))
	b.ClientData(18, []byte(clientOriginal[18:28]))
	b.ClientData(0, []byte(clientOriginal[0:6]))
	b.ServerData(0, []byte(serverOriginal))
	b.ClientFIN(28, nil)
	b.ServerFIN(24, nil)

	mgr := reassembly.NewManager(reassembly.PolicyFirstWins, false)
	events := run(mgr, b.Packets())
	v := onlyView(t, mgr)
	checkDir(t, "wrap a_to_b", v.AtoB, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte(clientOriginal), FINSeen: true, FINPos: 28, LengthProved: 28,
	})
	checkDir(t, "b_to_a", v.BtoA, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte(serverOriginal), FINSeen: true, FINPos: 24, LengthProved: 24,
	})
	var sawWrappedFIN bool
	for _, e := range events {
		if e.Code == reassembly.EvFINAccepted && e.RawSeq == 0x00000013 && e.Direction == "a_to_b" {
			sawWrappedFIN = true
		}
	}
	if !sawWrappedFIN {
		t.Errorf("did not record client FIN at wrapped raw seq 0x00000013")
	}
}

// TestHalfCloseAndDataAfterFIN: once FIN consumed the final sequence number,
// later data at/after it is rejected; the peer direction keeps accepting.
func TestHalfCloseAndDataAfterFIN(t *testing.T) {
	b := testsupport.NewBuilder(1000, 5000).Handshake()
	b.ClientData(0, []byte(clientOriginal[0:14]))
	b.ClientFIN(14, []byte(clientOriginal[14:28])) // data + FIN in one segment
	mgr := reassembly.NewManager(reassembly.PolicyFirstWins, false)
	run(mgr, b.Packets())
	v := onlyView(t, mgr)
	if v.Closed {
		t.Fatalf("generation must not be closed during half-close")
	}
	if !v.AtoB.FINSeen || v.BtoA.FINSeen {
		t.Fatalf("half-close flags wrong: client fin=%v server fin=%v", v.AtoB.FINSeen, v.BtoA.FINSeen)
	}
	if got := string(v.AtoB.Stream); got != clientOriginal {
		t.Fatalf("client stream after FIN-with-data: got %q", got)
	}

	// Server still speaks after the client half-closed.
	b.ServerData(0, []byte(serverOriginal))
	run(mgr, b.Packets()[len(b.Packets())-1:])
	v = onlyView(t, mgr)
	if v.Closed {
		t.Errorf("still half-close after server data")
	}
	if got := string(v.BtoA.Stream); got != serverOriginal {
		t.Errorf("server stream after half-close: got %q want %q", got, serverOriginal)
	}

	// Stray client byte beyond FIN is rejected; duplicate FIN is accepted as
	// retransmission; contradictory FIN is undecided.
	b.RawSegment(testsupport.FromClient, uint32(1000+1+28), []string{"ACK", "PSH"}, []byte("X"), "stray-after-fin")
	b.ClientFIN(28, nil) // duplicate FIN at the same position
	b.RawSegment(testsupport.FromClient, uint32(1000+1+27), []string{"ACK", "FIN"}, nil, "contradictory-fin")
	b.ServerFIN(24, nil)
	events := run(mgr, b.Packets()[len(b.Packets())-4:])
	v = onlyView(t, mgr)
	if got := string(v.AtoB.Stream); got != clientOriginal {
		t.Errorf("stream changed after FIN: got %q", got)
	}
	if !v.Closed {
		t.Errorf("generation should be closed after both FINs")
	}
	for _, want := range []struct {
		code  reassembly.EventCode
		level reassembly.EventLevel
	}{
		{reassembly.EvDataAfterFIN, reassembly.LevelReject},
		{reassembly.EvFINDuplicate, reassembly.LevelInfo},
		{reassembly.EvFINConflict, reassembly.LevelUndecided},
	} {
		if !testsupport.FindEvent(events, want.code, want.level) {
			t.Errorf("missing event %s/%s", want.code, want.level)
		}
	}
}

// TestMissingHandshake: no SYN observed; offsets are relative to the first
// seen byte and the result is explicitly marked anchorless/undecided.
func TestMissingHandshake(t *testing.T) {
	// Builder ISNs chosen so ClientData(0)/ServerData(0) raw seqs are 5000 and
	// 8000 respectively; the engine never learns the true ISN.
	b := testsupport.NewBuilder(4999, 7999) // deliberately no Handshake()
	b.ClientData(6, []byte(clientOriginal[6:18]))
	b.ServerData(6, []byte(serverOriginal[6:18]))
	b.ClientData(0, []byte(clientOriginal[0:6]))
	b.ServerData(0, []byte(serverOriginal[0:6]))
	b.ClientData(18, []byte(clientOriginal[18:28]))
	b.ServerData(18, []byte(serverOriginal[18:24]))
	b.ClientFIN(28, nil)
	b.ServerFIN(24, nil)

	mgr := reassembly.NewManager(reassembly.PolicyFirstWins, false)
	events := run(mgr, b.Packets())
	v := onlyView(t, mgr)
	// Relative coordinate system: first observed byte is 0, FIN is at 28.
	checkDir(t, "anchorless a_to_b", v.AtoB, testsupport.ExpectedDirection{
		HandshakeKnown: false, Stream: []byte(clientOriginal),
		Gaps: nil, FINSeen: true, FINPos: 28, LengthProved: 28,
	})
	checkDir(t, "anchorless b_to_a", v.BtoA, testsupport.ExpectedDirection{
		HandshakeKnown: false, Stream: []byte(serverOriginal),
		FINSeen: true, FINPos: 24, LengthProved: 24,
	})
	if !testsupport.FindEvent(events, reassembly.EvHandshakeAbsent, reassembly.LevelUndecided) {
		t.Errorf("expected HANDSHAKE_ABSENT undecided event")
	}
}

// TestConnectionReuse: a reused 4-tuple creates a new generation; streams must
// not contaminate each other. A duplicate same-ISN SYN on an open generation
// must not create one.
func TestConnectionReuse(t *testing.T) {
	b := testsupport.NewBuilder(1000, 5000)
	b.ClientSYN()
	b.ServerSYNACK()
	b.ClientHandshakeACK()
	b.ClientData(0, []byte("GEN0-CLIENT")) // 11 bytes
	b.ServerData(0, []byte("GEN0-SERVER"))
	b.ClientFIN(11, nil)
	b.ServerFIN(11, nil)
	// Generation 1: same canonical endpoints, new ISNs after orderly close.
	b.RawSegment(testsupport.FromClient, 9000, []string{"SYN"}, nil, "g1-syn")
	b.RawSegment(testsupport.FromServer, 9500, []string{"SYN", "ACK"}, nil, "g1-synack").
		RawSegment(testsupport.FromClient, 9001, []string{"ACK"}, nil, "g1-ack")
	b.RawSegment(testsupport.FromClient, 9001, []string{"ACK", "PSH"}, []byte("GEN1-CLIENT"), "g1-cdata")
	b.RawSegment(testsupport.FromServer, 9501, []string{"ACK", "PSH"}, []byte("GEN1-SERVER"), "g1-sdata")
	b.RawSegment(testsupport.FromClient, uint32(9001+11), []string{"ACK", "FIN"}, nil, "g1-cfin")
	b.RawSegment(testsupport.FromServer, uint32(9501+11), []string{"ACK", "FIN"}, nil, "g1-sfin")

	mgr := reassembly.NewManager(reassembly.PolicyFirstWins, false)
	events := run(mgr, b.Packets())
	views := viewN(t, mgr, 2)
	if got := string(views[0].AtoB.Stream); got != "GEN0-CLIENT" {
		t.Errorf("gen0 client: %q", got)
	}
	if got := string(views[0].BtoA.Stream); got != "GEN0-SERVER" {
		t.Errorf("gen0 server: %q", got)
	}
	if got := string(views[1].AtoB.Stream); got != "GEN1-CLIENT" {
		t.Errorf("gen1 client: %q", got)
	}
	if got := string(views[1].BtoA.Stream); got != "GEN1-SERVER" {
		t.Errorf("gen1 server: %q", got)
	}
	if !testsupport.FindEvent(events, reassembly.EvNewGeneration, reassembly.LevelInfo) {
		t.Errorf("expected NEW_GENERATION event")
	}

	// Duplicate opening SYN (same ISN) while the generation is open.
	b2 := testsupport.NewBuilder(2000, 6000).Handshake()
	b2.ClientData(0, []byte("data"))
	b2.ClientSYN() // retransmitted SYN, is 2000
	mgr2 := reassembly.NewManager(reassembly.PolicyFirstWins, false)
	ev2 := run(mgr2, b2.Packets())
	if got := len(mgr2.AllViews()); got != 1 {
		t.Errorf("duplicate SYN created %d generations, want 1", got)
	}
	if !testsupport.FindEvent(ev2, reassembly.EvSYNDuplicate, reassembly.LevelInfo) {
		t.Errorf("expected SYN_DUPLICATE event")
	}
}

// TestMissingSegmentProvesGap: the absent middle is an exact gap proved by
// later data; without FIN the length is unknown, with FIN the gap bounds are
// exact. Late arrival heals the gap without duplicating output.
func TestMissingSegmentProvesGap(t *testing.T) {
	b := testsupport.NewBuilder(1000, 5000).Handshake()
	b.ClientData(0, []byte(clientOriginal[0:6]))
	b.ClientData(12, []byte(clientOriginal[12:20]))
	b.ClientData(20, []byte(clientOriginal[20:28]))
	mgr := reassembly.NewManager(reassembly.PolicyFirstWins, false)
	run(mgr, b.Packets())
	v := onlyView(t, mgr)
	checkDir(t, "no-FIN", v.AtoB, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte("HELLO-"),
		Gaps:           []reassembly.Gap{{Start: 6, End: 12}},
		HeldRunsData:   [][]byte{[]byte("ASSEMBLY-WORLD!!")},
	})
	if v.AtoB.LengthProved != 0 {
		t.Errorf("length must not be proved without FIN, got %d", v.AtoB.LengthProved)
	}

	// Late arrival heals the gap; the withheld known bytes are exactly
	// clientOriginal[6:12] = "TCP-RE" (6 bytes).
	b.ClientData(6, []byte(clientOriginal[6:12]))
	b.ClientFIN(28, nil)
	b.ServerFIN(0, nil)
	run(mgr, b.Packets()[len(b.Packets())-3:])
	v = onlyView(t, mgr)
	checkDir(t, "healed", v.AtoB, testsupport.ExpectedDirection{
		HandshakeKnown: true,
		Stream:         []byte(clientOriginal), Gaps: nil, FINSeen: true,
		FINPos: 28, LengthProved: 28,
	})
}

// TestRSTClosesGenerationAndPostRSTUndecided: an RST aborts the generation;
// later data is undecided/not output, and a fresh SYN on the tuple starts a
// new generation.
func TestRSTClosesGenerationAndPostRSTUndecided(t *testing.T) {
	b := testsupport.NewBuilder(1000, 5000).Handshake()
	b.ClientData(0, []byte(clientOriginal[0:6]))
	b.RawSegment(testsupport.FromClient, uint32(1000+1+6), []string{"RST", "ACK"}, nil, "rst")
	b.RawSegment(testsupport.FromClient, uint32(1000+1+6), []string{"ACK", "PSH"}, []byte("after-rst"), "post-rst")
	// Reopen with a new SYN.
	b.RawSegment(testsupport.FromClient, 4000, []string{"SYN"}, nil, "g1-syn")
	b.RawSegment(testsupport.FromServer, 4500, []string{"SYN", "ACK"}, nil, "g1-synack")
	b.RawSegment(testsupport.FromClient, 4001, []string{"ACK", "PSH"}, []byte("NEW"), "g1-data")
	b.RawSegment(testsupport.FromClient, uint32(4001+3), []string{"ACK", "FIN"}, nil, "g1-fin")

	mgr := reassembly.NewManager(reassembly.PolicyFirstWins, false)
	events := run(mgr, b.Packets())
	views := viewN(t, mgr, 2)
	if !views[0].Reset {
		t.Errorf("generation 0 must be marked reset")
	}
	if got := string(views[0].AtoB.Stream); got != "HELLO-" {
		t.Errorf("gen0 stream: %q", got)
	}
	if got := string(views[1].AtoB.Stream); got != "NEW" {
		t.Errorf("gen1 stream: %q", got)
	}
	if !testsupport.FindEvent(events, reassembly.EvRSTClosed, reassembly.LevelInfo) {
		t.Errorf("expected RST_CLOSED event")
	}
	if !testsupport.FindEvent(events, reassembly.EvPacketAfterRST, reassembly.LevelUndecided) {
		t.Errorf("expected PACKET_AFTER_RST undecided event")
	}
	for _, vv := range views {
		if strings.Contains(string(vv.AtoB.Stream), "after-rst") {
			t.Errorf("post-RST data leaked into output: %q", vv.AtoB.Stream)
		}
	}
}
