package nat_test

import (
	"context"
	"fmt"
	"sync"
	"testing"

	"natlab/internal/model"
	"natlab/internal/storage"
)

// TestBidirectionalFlow exercises the happy path and the exact-peer check:
// outbound first packet allocates, return packets translate the destination,
// and a return from a different remote endpoint is dropped.
func TestBidirectionalFlow(t *testing.T) {
	eng := newEngine(t, "e2e", testCfg(), nil)

	d1 := mustProc(t, eng, out(1, 0, model.TCP, "10.0.0.2", 40001, "203.0.113.10", 80,
		flags(true, false, false, false)))
	if d1.Verdict != model.AcceptTranslate || d1.Post == nil {
		t.Fatalf("first SYN verdict=%v reason=%s", d1.Verdict, d1.Reason)
	}
	if d1.Post.SrcIP != "198.51.100.1" || d1.Post.SrcPort != 20000 {
		t.Fatalf("outbound translation = %s:%d", d1.Post.SrcIP, d1.Post.SrcPort)
	}
	if d1.AllocatedPort != 20000 || d1.MappingID != "TCP-20000" {
		t.Fatalf("allocated=%d id=%s", d1.AllocatedPort, d1.MappingID)
	}

	d2 := mustProc(t, eng, in(2, 1, model.TCP, "203.0.113.10", 80, "198.51.100.1", 20000,
		flags(true, true, false, false)))
	if d2.Verdict != model.AcceptForward || d2.Post == nil ||
		d2.Post.DstIP != "10.0.0.2" || d2.Post.DstPort != 40001 {
		t.Fatalf("SYN+ACK return verdict=%v post=%+v", d2.Verdict, d2.Post)
	}
	if d2.StateAfter != "ESTABLISHED" {
		t.Fatalf("state after synack = %q", d2.StateAfter)
	}

	// Same flow forward after handshake.
	d3 := mustProc(t, eng, out(3, 2, model.TCP, "10.0.0.2", 40001, "203.0.113.10", 80,
		flags(false, true, false, false)))
	if d3.Verdict != model.AcceptForward || d3.StateAfter != "ESTABLISHED" {
		t.Fatalf("post-handshake outbound verdict=%v state=%s", d3.Verdict, d3.StateAfter)
	}

	// UDP over the same time window is independent and gets port 30000.
	d4 := mustProc(t, eng, out(4, 3, model.UDP, "10.0.0.2", 53000, "198.51.100.53", 53))
	if d4.Verdict != model.AcceptTranslate || d4.Post.SrcPort != 30000 {
		t.Fatalf("udp alloc verdict=%v port=%d", d4.Verdict, d4.Post.SrcPort)
	}
	d5 := mustProc(t, eng, in(5, 4, model.UDP, "198.51.100.53", 53, "198.51.100.1", 30000))
	if d5.Verdict != model.AcceptForward || d5.Post.DstIP != "10.0.0.2" || d5.Post.DstPort != 53000 {
		t.Fatalf("udp return verdict=%v post=%+v", d5.Verdict, d5.Post)
	}

	// A different remote peer sending to the mapped port: state conflict,
	// mapping remains usable for the true peer.
	d6 := mustProc(t, eng, in(6, 5, model.TCP, "203.0.113.99", 9000, "198.51.100.1", 20000,
		flags(false, true, false, false)))
	if d6.Verdict != model.Reject || d6.Reason != model.ReasonRemoteMismatch ||
		d6.Class != model.ClassState {
		t.Fatalf("peer spoof verdict=%v reason=%s class=%s", d6.Verdict, d6.Reason, d6.Class)
	}

	st := eng.Stats()
	if st.MappingsCreated != 2 || st.ActiveMappings != 2 {
		t.Fatalf("stats created=%d active=%d, want 2/2", st.MappingsCreated, st.ActiveMappings)
	}
	if st.RejectedState != 1 {
		t.Fatalf("state rejects=%d, want 1", st.RejectedState)
	}
}

// TestPortExhaustion allocates the whole TCP pool and asserts the next first
// packet is rejected with the resource_exhaustion class, while mappings stay
// one-to-one with flows and are not re-used.
func TestPortExhaustion(t *testing.T) {
	eng := newEngine(t, "exh", testCfg(), nil)
	flows := []struct {
		ip   string
		port uint16
	}{
		{"10.0.0.2", 40001}, {"10.0.0.3", 40002}, {"10.0.0.4", 40003}, {"10.0.0.5", 40004},
	}
	seen := map[uint16]bool{}
	for i, f := range flows {
		d := mustProc(t, eng, out(int64(i+1), float64(i), model.TCP, f.ip, f.port,
			"203.0.113.10", 80, flags(true, false, false, false)))
		if d.Verdict != model.AcceptTranslate {
			t.Fatalf("flow %d verdict=%v reason=%s", i, d.Verdict, d.Reason)
		}
		if seen[d.AllocatedPort] {
			t.Fatalf("port %d allocated to two active flows", d.AllocatedPort)
		}
		seen[d.AllocatedPort] = true
	}

	d := mustProc(t, eng, out(5, 4, model.TCP, "10.0.0.6", 40005, "203.0.113.10", 81,
		flags(true, false, false, false)))
	if d.Verdict != model.Reject || d.Reason != model.ReasonPortExhausted {
		t.Fatalf("exhaustion verdict=%v reason=%s", d.Verdict, d.Reason)
	}
	if d.Class != model.ClassExhaustion {
		t.Fatalf("exhaustion class=%s", d.Class)
	}
	if got := eng.Stats().RejectedExhaustion; got != 1 {
		t.Fatalf("exhaustion counter=%d, want 1", got)
	}
}

// TestTimeoutThenReuse verifies: no reuse while active, idle expiry frees the
// port, a late return is classified mapping_expired (not no_mapping), and the
// freed port can serve a new flow.
func TestTimeoutThenReuse(t *testing.T) {
	eng := newEngine(t, "ttr", testCfg(), nil)

	open := mustProc(t, eng, out(1, 0, model.UDP, "10.0.0.2", 50001, "198.51.100.53", 53))
	if open.Post.SrcPort != 30000 {
		t.Fatalf("port=%d", open.Post.SrcPort)
	}
	// Second distinct flow while first is alive may not take 30000.
	second := mustProc(t, eng, out(2, 1, model.UDP, "10.0.0.3", 50002, "198.51.100.54", 53))
	if second.Verdict != model.AcceptTranslate || second.Post.SrcPort == 30000 {
		t.Fatalf("second flow reused active port: %+v", second.Post)
	}
	// Refresh the first flow at t=5 (deadline would be t=10).
	if d := mustProc(t, eng, in(3, 5, model.UDP, "198.51.100.53", 53, "198.51.100.1", 30000)); d.Verdict != model.AcceptForward {
		t.Fatalf("active return verdict=%v", d.Verdict)
	}
	// Keep the second flow alive across the first flow's expiry window.
	mustProc(t, eng, in(9, 8, model.UDP, "198.51.100.54", 53, "198.51.100.1", second.Post.SrcPort))
	mustProc(t, eng, in(10, 15, model.UDP, "198.51.100.54", 53, "198.51.100.1", second.Post.SrcPort))

	// At t=16 the refreshed flow (lastSeen=5, 10s timeout) has expired; the
	// advancing clock triggered by this late packet reaps exactly that one.
	late := mustProc(t, eng, in(4, 16, model.UDP, "198.51.100.53", 53, "198.51.100.1", 30000))
	if late.Verdict != model.Reject || late.Reason != model.ReasonMappingExpired ||
		late.Class != model.ClassState {
		t.Fatalf("late return verdict=%v reason=%s class=%s", late.Verdict, late.Reason, late.Class)
	}
	if len(late.Rationale) == 0 {
		t.Fatal("rejection rationale must be recorded")
	}
	if got := eng.Stats().MappingsExpired; got != 1 {
		t.Fatalf("expired count=%d, want 1 (second flow must stay alive)", got)
	}

	// Port is now reusable by a brand-new flow.
	reuse := mustProc(t, eng, out(5, 17, model.UDP, "10.0.0.9", 50099, "198.51.100.77", 123))
	if reuse.Verdict != model.AcceptTranslate || reuse.Post.SrcPort != 30000 {
		t.Fatalf("reuse verdict=%v port=%+v", reuse.Verdict, reuse.Post)
	}
}

// TestSeparateTCPAndUDPTimeouts confirms the two timers are independent:
// UDP expires well inside an established TCP connection's lifetime.
func TestSeparateTCPAndUDPTimeouts(t *testing.T) {
	eng := newEngine(t, "sep", testCfg(), nil)
	// Establish TCP.
	mustProc(t, eng, out(1, 0, model.TCP, "10.0.0.2", 40001, "203.0.113.10", 80,
		flags(true, false, false, false)))
	mustProc(t, eng, in(2, 0.1, model.TCP, "203.0.113.10", 80, "198.51.100.1", 20000,
		flags(true, true, false, false)))
	mustProc(t, eng, out(3, 0.2, model.TCP, "10.0.0.2", 40001, "203.0.113.10", 80,
		flags(false, true, false, false)))
	// Open UDP.
	mustProc(t, eng, out(4, 0.3, model.UDP, "10.0.0.2", 53000, "198.51.100.53", 53))

	// At t=15: UDP timeout (10s) hit, TCP established (30s) still alive.
	udpLate := mustProc(t, eng, in(5, 15, model.UDP, "198.51.100.53", 53,
		"198.51.100.1", 30000))
	if udpLate.Reason != model.ReasonMappingExpired {
		t.Fatalf("udp at t=15 reason=%s, want mapping_expired", udpLate.Reason)
	}
	tcpFwd := mustProc(t, eng, in(6, 15.1, model.TCP, "203.0.113.10", 80,
		"198.51.100.1", 20000, flags(false, true, false, false)))
	if tcpFwd.Verdict != model.AcceptForward {
		t.Fatalf("tcp at t=15 verdict=%v reason=%s, want forward", tcpFwd.Verdict, tcpFwd.Reason)
	}

	// At t=46: TCP established also expires (last refreshed 15.1 + 30s).
	tcpLate := mustProc(t, eng, in(7, 46, model.TCP, "203.0.113.10", 80,
		"198.51.100.1", 20000, flags(false, true, false, false)))
	if tcpLate.Reason != model.ReasonMappingExpired {
		t.Fatalf("tcp at t=46 reason=%s, want mapping_expired", tcpLate.Reason)
	}
}

// TestClockRollbackDoesNotRevive ensures a packet stamped in the past cannot
// move the clock backwards or resurrect an expired mapping.
func TestClockRollbackDoesNotRevive(t *testing.T) {
	eng := newEngine(t, "rb", testCfg(), nil)
	mustProc(t, eng, out(1, 0, model.UDP, "10.0.0.2", 50001, "198.51.100.53", 53))
	wmBefore := eng.Watermark()

	// Advance to expiry and beyond (t=11).
	exp := mustProc(t, eng, in(2, 11, model.UDP, "198.51.100.53", 53,
		"198.51.100.1", 30000))
	if exp.Reason != model.ReasonMappingExpired {
		t.Fatalf("expiry reason=%s", exp.Reason)
	}
	wmAfter := eng.Watermark()
	if !wmAfter.After(wmBefore) {
		t.Fatalf("watermark did not advance: %v -> %v", wmBefore, wmAfter)
	}

	// A packet stamped at t=2 (before expiry) arrives out of order: it must be
	// evaluated at the monotonic clock, so the mapping stays dead.
	past := mustProc(t, eng, in(3, 2, model.UDP, "198.51.100.53", 53,
		"198.51.100.1", 30000))
	if !past.Watermark.Equal(wmAfter) {
		t.Fatalf("past packet moved watermark %v -> %v", wmAfter, past.Watermark)
	}
	if past.Reason != model.ReasonMappingExpired {
		t.Fatalf("past-dated packet revived mapping: reason=%s", past.Reason)
	}
	if got := eng.Stats().ClockRollbacks; got != 1 {
		t.Fatalf("rollback counter=%d, want 1", got)
	}
}

// TestNoMappingLateOrNever distinguishes a port that never had a mapping from
// one that recently died.
func TestNoMappingLateOrNever(t *testing.T) {
	eng := newEngine(t, "nm", testCfg(), nil)
	never := mustProc(t, eng, in(1, 0, model.TCP, "203.0.113.10", 80, "198.51.100.1", 20077,
		flags(true, true, false, false)))
	if never.Reason != model.ReasonNoMapping || never.Class != model.ClassState {
		t.Fatalf("unsolicited reason=%s class=%s", never.Reason, never.Class)
	}

	mustProc(t, eng, out(2, 0, model.UDP, "10.0.0.2", 50001, "198.51.100.53", 53))
	mustProc(t, eng, in(3, 11, model.UDP, "198.51.100.53", 53, "198.51.100.1", 30000)) // expiry
	late := mustProc(t, eng, in(4, 12, model.UDP, "198.51.100.53", 53,
		"198.51.100.1", 30000))
	if late.Reason != model.ReasonMappingExpired {
		t.Fatalf("late reason=%s, want mapping_expired", late.Reason)
	}
}

// TestConcurrentFirstPackets fires first packets for many flows in parallel.
// The serialized engine must create exactly one mapping per distinct flow,
// assign distinct ports, and never duplicate an active port.
func TestConcurrentFirstPackets(t *testing.T) {
	eng := newEngine(t, "conc", testCfg(), nil)
	const n = 4 // exactly the UDP pool size
	var wg sync.WaitGroup
	results := make([]model.Decision, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			p := out(int64(i+1), 0, model.UDP,
				fmt.Sprintf("10.0.0.%d", 110+i), uint16(51000+i),
				"198.51.100.200", uint16(1000+i))
			results[i] = mustProc(t, eng, p)
		}(i)
	}
	wg.Wait()

	ports := map[uint16]bool{}
	for i, d := range results {
		if d.Verdict != model.AcceptTranslate {
			t.Fatalf("goroutine %d verdict=%v reason=%s", i, d.Verdict, d.Reason)
		}
		if ports[d.AllocatedPort] {
			t.Fatalf("port %d assigned to two concurrent flows", d.AllocatedPort)
		}
		ports[d.AllocatedPort] = true
	}
	if len(ports) != n || eng.Stats().ActiveMappings != n {
		t.Fatalf("distinct ports=%d active=%d, want %d/%d", len(ports),
			eng.Stats().ActiveMappings, n, n)
	}

	// One more concurrent burst must now exhaust, never duplicate a port.
	var wg2 sync.WaitGroup
	exhausted := make(chan bool, 8)
	for i := 0; i < 8; i++ {
		wg2.Add(1)
		go func(i int) {
			defer wg2.Done()
			d := mustProc(t, eng, out(int64(100+i), 0.1, model.UDP,
				"10.0.0.200", uint16(52000+i), "198.51.100.210", uint16(2000+i)))
			exhausted <- (d.Reason == model.ReasonPortExhausted)
		}(i)
	}
	wg2.Wait()
	close(exhausted)
	for e := range exhausted {
		if !e {
			t.Fatal("a concurrent first packet obtained a port after exhaustion")
		}
	}
}

// TestInputAndConflictClasses asserts the four-class discrimination required
// by the spec: input error vs state conflict vs exhaustion vs compute failure.
func TestInputAndConflictClasses(t *testing.T) {
	eng := newEngine(t, "cls", testCfg(), nil)

	frag := mustProc(t, eng, func() model.Packet {
		p := out(1, 0, model.TCP, "10.0.0.2", 40001, "203.0.113.10", 80,
			flags(true, false, false, false))
		p.Fragmented = true
		return p
	}())
	if frag.Reason != model.ReasonFragmentDropped || frag.Class != model.ClassInput {
		t.Fatalf("fragment reason=%s class=%s", frag.Reason, frag.Class)
	}

	icmp := mustProc(t, eng, out(2, 0, model.Protocol("ICMP"), "10.0.0.2", 0,
		"203.0.113.10", 0))
	if icmp.Reason != model.ReasonProtocolUnsupport || icmp.Class != model.ClassInput {
		t.Fatalf("icmp reason=%s class=%s", icmp.Reason, icmp.Class)
	}

	// TCP data with no mapping and no SYN is a state conflict, not input error.
	orphan := mustProc(t, eng, out(3, 0, model.TCP, "10.0.0.2", 40001,
		"203.0.113.10", 80, flags(false, true, false, false)))
	if orphan.Reason != model.ReasonNoMapping || orphan.Class != model.ClassState {
		t.Fatalf("orphan ACK reason=%s class=%s", orphan.Reason, orphan.Class)
	}

	// Inbound to the wrong external IP is an input error.
	wrongExt := mustProc(t, eng, in(4, 0, model.TCP, "203.0.113.10", 80,
		"198.51.100.9", 20000, flags(true, true, false, false)))
	if wrongExt.Reason != model.ReasonExternalMismatch || wrongExt.Class != model.ClassInput {
		t.Fatalf("wrong external reason=%s class=%s", wrongExt.Reason, wrongExt.Class)
	}
}

// TestComputeFailureClass forces a persistence fault and asserts the decision
// is compute_failure, distinct from every policy rejection, and that the
// allocator is rolled back so the port is not leaked.
func TestComputeFailureClass(t *testing.T) {
	mem := storage.NewMemory()
	faulty := &storage.Faulty{Inner: mem, FailPutMapping: true}
	eng := newEngine(t, "cf", testCfg(), faulty)

	res, err := eng.Process(context.Background(),
		out(1, 0, model.TCP, "10.0.0.2", 40001, "203.0.113.10", 80,
			flags(true, false, false, false)))
	if err == nil {
		t.Fatal("expected compute error from forced fault")
	}
	if res.Decision.Verdict != model.ComputeFailure ||
		res.Decision.Reason != model.ReasonStorageFailure ||
		res.Decision.Class != model.ClassCompute {
		t.Fatalf("verdict=%v reason=%s class=%s", res.Decision.Verdict,
			res.Decision.Reason, res.Decision.Class)
	}
	if got := eng.Stats().ComputeFailures; got != 1 {
		t.Fatalf("compute failures=%d, want 1", got)
	}
	// Port rollback: switch the fault off and the first allocation must still
	// be the bottom of the range.
	faulty.FailPutMapping = false
	again := mustProc(t, eng, out(2, 1, model.TCP, "10.0.0.2", 40001,
		"203.0.113.10", 80, flags(true, false, false, false)))
	if again.AllocatedPort != 20000 {
		t.Fatalf("port leaked after failed write: allocated %d, want 20000",
			again.AllocatedPort)
	}
}

// TestRSTReleasesPortForReuse verifies an accepted RST tears the mapping down
// deterministically and frees its port.
func TestRSTReleasesPortForReuse(t *testing.T) {
	eng := newEngine(t, "rst", testCfg(), nil)
	mustProc(t, eng, out(1, 0, model.TCP, "10.0.0.2", 40001, "203.0.113.10", 80,
		flags(true, false, false, false)))
	rst := mustProc(t, eng, in(2, 1, model.TCP, "203.0.113.10", 80,
		"198.51.100.1", 20000, flags(false, false, false, true)))
	if rst.StateAfter != "CLOSED" || eng.Stats().ActiveMappings != 0 {
		t.Fatalf("rst state=%s active=%d", rst.StateAfter, eng.Stats().ActiveMappings)
	}
	reuse := mustProc(t, eng, out(3, 2, model.TCP, "10.0.0.3", 40002,
		"203.0.113.20", 443, flags(true, false, false, false)))
	if reuse.AllocatedPort != 20000 {
		t.Fatalf("port after RST = %d, want 20000", reuse.AllocatedPort)
	}
}

// TestDecisionLogAndWatermark ensures intermediate state is retained per
// packet for replay: seq, pre/post tuples, states and rationale all persist.
func TestDecisionLogAndWatermark(t *testing.T) {
	mem := storage.NewMemory()
	eng := newEngine(t, "log", testCfg(), mem)
	mustProc(t, eng, out(1, 0, model.TCP, "10.0.0.2", 40001, "203.0.113.10", 80,
		flags(true, false, false, false)))

	dl, err := mem.ListDecisions(context.Background(), "log", 0, 0)
	if err != nil || len(dl) != 1 {
		t.Fatalf("decision log len=%d err=%v", len(dl), err)
	}
	d := dl[0]
	if d.Seq != 1 || d.Pre.SrcPort != 40001 || d.Post == nil || d.Post.SrcPort != 20000 ||
		d.RunID != "log" || d.Watermark.IsZero() || d.Rationale == "" {
		t.Fatalf("logged decision missing replay data: %+v", d)
	}
}
