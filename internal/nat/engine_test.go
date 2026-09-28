package nat_test

import (
	"context"
	"fmt"
	"strconv"
	"sync"
	"testing"
	"time"

	"natlab/internal/config"
	"natlab/internal/memstore"
	"natlab/internal/model"
	"natlab/internal/nat"
	"natlab/internal/testutil"
)

const baseTS = "2026-01-01T00:00:00Z"

func mustTS(t *testing.T, offsetSec float64) time.Time {
	t.Helper()
	base, err := time.Parse(time.RFC3339, baseTS)
	if err != nil {
		t.Fatal(err)
	}
	return base.Add(time.Duration(offsetSec * float64(time.Second)))
}

func newEngine(t *testing.T, cfg *config.Config) (*nat.Engine, *memstore.MemStore) {
	t.Helper()
	st := memstore.New()
	return nat.NewEngine(cfg, st), st
}

func testCfg() *config.Config {
	cfg := config.Defaults()
	cfg.PortLow = 40000
	cfg.PortHigh = 40002
	cfg.Timeouts = config.Timeouts{
		TCPSynSent: "5s", TCPTransient: "60s", TCPEstablished: "100s", UDP: "10s",
	}
	if err := cfg.Resolve(); err != nil {
		panic(err)
	}
	return cfg
}

func udpOut(ts time.Time, srcIP string, srcPort uint16, dstIP string, dstPort uint16) model.Packet {
	return model.Packet{
		ObservedAt: ts, Direction: model.Outbound,
		FiveTuple: model.FiveTuple{SrcIP: srcIP, SrcPort: srcPort, DstIP: dstIP, DstPort: dstPort, Protocol: model.UDP},
	}
}

func udpIn(ts time.Time, srcIP string, srcPort uint16, extPort uint16) model.Packet {
	return model.Packet{
		ObservedAt: ts, Direction: model.Inbound,
		FiveTuple: model.FiveTuple{SrcIP: srcIP, SrcPort: srcPort, DstIP: "203.0.113.1", DstPort: extPort, Protocol: model.UDP},
	}
}

func tcpOut(ts time.Time, srcPort uint16, dstIP string, dstPort uint16, flags string) model.Packet {
	return model.Packet{
		ObservedAt: ts, Direction: model.Outbound, Flags: flags,
		FiveTuple: model.FiveTuple{SrcIP: "10.1.1.10", SrcPort: srcPort, DstIP: dstIP, DstPort: dstPort, Protocol: model.TCP},
	}
}

func tcpIn(ts time.Time, srcIP string, srcPort, extPort uint16, flags string) model.Packet {
	return model.Packet{
		ObservedAt: ts, Direction: model.Inbound, Flags: flags,
		FiveTuple: model.FiveTuple{SrcIP: srcIP, SrcPort: srcPort, DstIP: "203.0.113.1", DstPort: extPort, Protocol: model.TCP},
	}
}

// TestPortExhaustion drives a 3-port pool to exhaustion and asserts the exact
// ports and the PORT_EXHAUSTED category.
func TestPortExhaustion(t *testing.T) {
	rl := testutil.NewRun(t, "nat")
	defer rl.Finish(!t.Failed())
	eng, _ := newEngine(t, testCfg())
	ctx := context.Background()

	peers := []string{"198.51.100.1", "198.51.100.2", "198.51.100.3", "198.51.100.4"}
	for i, peer := range peers {
		pkt := udpOut(mustTS(t, float64(i)), "10.1.1.10", uint16(5000+i), peer, 53)
		res, err := eng.Evaluate(ctx, "exhaust", pkt)
		if err != nil {
			t.Fatalf("packet %d compute error: %v", i, err)
		}
		if i < 3 {
			if !res.Decision.Accepted {
				t.Fatalf("packet %d rejected: %s %s", i, res.Decision.Category, res.Decision.Code)
			}
			want := uint16(40000 + i)
			if res.Decision.Mapping.MappedPort != want {
				t.Fatalf("packet %d mapped port = %d, want %d", i, res.Decision.Mapping.MappedPort, want)
			}
			rl.Step(i, "udp-out", "accepted", true, "port="+fmt.Sprint(res.Decision.Mapping.MappedPort))
		} else {
			if res.Decision.Accepted {
				t.Fatalf("packet %d must be rejected, got port %d", i, res.Decision.Mapping.MappedPort)
			}
			if res.Decision.Category != model.CatResourceExhausted || res.Decision.Code != model.CodePortExhausted {
				t.Fatalf("packet %d = %s/%s, want resource_exhausted/PORT_EXHAUSTED",
					i, res.Decision.Category, res.Decision.Code)
			}
			rl.Step(i, "udp-out", "PORT_EXHAUSTED", false, "pool 40000-40002 full")
		}
	}
}

// TestBidirectionalFlow walks a TCP handshake and data exchange both ways,
// checking the translated 5-tuples and state transitions.
func TestBidirectionalFlow(t *testing.T) {
	rl := testutil.NewRun(t, "nat")
	defer rl.Finish(!t.Failed())
	eng, _ := newEngine(t, testCfg())
	ctx := context.Background()
	const run = "bidi"

	type step struct {
		pkt   model.Packet
		state string
	}
	steps := []step{
		{tcpOut(mustTS(t, 0), 41000, "198.51.100.50", 443, "SYN"), model.StateSynSent},
		{tcpIn(mustTS(t, 0.1), "198.51.100.50", 443, 40000, "SYN+ACK"), model.StateSynAckRcvd},
		{tcpOut(mustTS(t, 0.2), 41000, "198.51.100.50", 443, "ACK"), model.StateEstablished},
		{udpLike(tcpOut(mustTS(t, 1), 41000, "198.51.100.50", 443, "ACK")), model.StateEstablished},
		{tcpIn(mustTS(t, 2), "198.51.100.50", 443, 40000, "ACK"), model.StateEstablished},
	}
	for i, s := range steps {
		res, err := eng.Evaluate(ctx, run, s.pkt)
		if err != nil {
			t.Fatalf("step %d compute error: %v", i, err)
		}
		if !res.Decision.Accepted {
			t.Fatalf("step %d rejected: %s/%s %s", i, res.Decision.Category, res.Decision.Code, res.Decision.Reason)
		}
		if res.Decision.Mapping.State != s.state {
			t.Fatalf("step %d state = %s, want %s", i, res.Decision.Mapping.State, s.state)
		}
		if tr := res.Decision.Translated; tr == nil || tr.Protocol != model.TCP {
			t.Fatalf("step %d missing translated tuple", i)
		}
		rl.Step(i, s.state, "accepted", true, "port=40000")
	}

	// Outbound translation rewrites source to public:40000.
	res0, _ := eng.Evaluate(ctx, run, tcpOut(mustTS(t, 3), 41000, "198.51.100.50", 443, "ACK"))
	if res0.Decision.Translated.SrcIP != "203.0.113.1" || res0.Decision.Translated.SrcPort != 40000 {
		t.Fatalf("outbound translation = %+v", res0.Decision.Translated)
	}
	// Inbound translation rewrites destination to private endpoint.
	resIn, err := eng.Evaluate(ctx, run, tcpIn(mustTS(t, 4), "198.51.100.50", 443, 40000, "ACK"))
	if err != nil || !resIn.Decision.Accepted {
		t.Fatalf("inbound data: %v accepted=%v", err, resIn != nil && resIn.Decision.Accepted)
	}
	if resIn.Decision.Translated.DstIP != "10.1.1.10" || resIn.Decision.Translated.DstPort != 41000 {
		t.Fatalf("inbound translation = %+v", resIn.Decision.Translated)
	}
}

// udpLike is a no-op wrapper keeping step tables uniform.
func udpLike(p model.Packet) model.Packet { return p }

// TestReuseAfterTimeout verifies TCP/UDP have separate timeouts indirectly via
// UDP expiry: the port is released at exactly now >= expires and reused by the
// next first-packet; an active mapping is never double-booked.
func TestReuseAfterTimeout(t *testing.T) {
	rl := testutil.NewRun(t, "nat")
	defer rl.Finish(!t.Failed())
	eng, st := newEngine(t, testCfg())
	ctx := context.Background()
	const run = "reuse"

	// t=0 mapping to peer A, expires at t=10.
	res, err := eng.Evaluate(ctx, run, udpOut(mustTS(t, 0), "10.1.1.20", 6000, "198.51.100.70", 53))
	if err != nil || !res.Decision.Accepted || res.Decision.Mapping.MappedPort != 40000 {
		t.Fatalf("initial mapping: err=%v res=%+v", err, res)
	}
	// t=9 return is fine (1 second to spare).
	res, err = eng.Evaluate(ctx, run, udpIn(mustTS(t, 9), "198.51.100.70", 53, 40000))
	if err != nil || !res.Decision.Accepted {
		t.Fatalf("return at t=9: err=%v accepted=%v", err, res != nil && res.Decision.Accepted)
	}
	// Expiry was refreshed: now expires at t=19. Fill the rest of the pool with
	// peers created at t=15 so they are still alive at t=20.
	res, _ = eng.Evaluate(ctx, run, udpOut(mustTS(t, 15), "10.1.1.20", 6001, "198.51.100.71", 53))
	if !res.Decision.Accepted || res.Decision.Mapping.MappedPort != 40001 {
		t.Fatalf("second mapping port = %+v", res)
	}
	res, _ = eng.Evaluate(ctx, run, udpOut(mustTS(t, 15.1), "10.1.1.20", 6002, "198.51.100.72", 53))
	if !res.Decision.Accepted || res.Decision.Mapping.MappedPort != 40002 {
		t.Fatalf("third mapping port = %+v", res)
	}
	// t=20: the t=9-refreshed mapping (peer A, port 40000) expires at t=19;
	// the two t=15 mappings survive (expire at t=25), so exactly one is swept.
	res, err = eng.Evaluate(ctx, run, udpIn(mustTS(t, 20), "198.51.100.70", 53, 40000))
	if err != nil {
		t.Fatal(err)
	}
	if res.Decision.Accepted || res.Decision.Code != model.CodeLateReturnExpired {
		t.Fatalf("late return at t=20 accepted=%v code=%s", res.Decision.Accepted, res.Decision.Code)
	}
	if res.Decision.Swept != 1 {
		t.Fatalf("swept = %d, want 1", res.Decision.Swept)
	}
	rl.Infof("swept=1 at t=20; port 40000 released (late return rejected, event id %d)", res.Event.ID)

	// A new first-packet reuses the freed 40000.
	res, err = eng.Evaluate(ctx, run, udpOut(mustTS(t, 20.1), "10.1.1.21", 6010, "198.51.100.99", 53))
	if err != nil || !res.Decision.Accepted {
		t.Fatalf("reuse mapping: err=%v accepted=%v", err, res != nil && res.Decision.Accepted)
	}
	if res.Decision.Mapping.MappedPort != 40000 {
		t.Fatalf("reused port = %d, want 40000", res.Decision.Mapping.MappedPort)
	}
	// Store invariant: only one active owner of 40000.
	ms, _ := st.ListMappings(ctx, run, true)
	owners := 0
	for _, m := range ms {
		if m.MappedPort == 40000 {
			owners++
		}
	}
	if owners != 1 {
		t.Fatalf("active owners of 40000 = %d, want 1", owners)
	}
}

// TestLateReturnCategories distinguishes never-owned, expired and filtered.
func TestLateReturnCategories(t *testing.T) {
	eng, _ := newEngine(t, testCfg())
	ctx := context.Background()
	const run = "late"

	// Inbound to never-owned port.
	res, err := eng.Evaluate(ctx, run, udpIn(mustTS(t, 0), "198.51.100.1", 80, 55555))
	if err != nil || res.Decision.Accepted || res.Decision.Code != model.CodeInboundNoMapping {
		t.Fatalf("never-owned: err=%v code=%s", err, res.Decision.Code)
	}
	// Create a mapping then let it expire.
	res, _ = eng.Evaluate(ctx, run, udpOut(mustTS(t, 1), "10.1.1.30", 7000, "198.51.100.90", 53))
	if !res.Decision.Accepted {
		t.Fatal("mapping setup rejected")
	}
	res, err = eng.Evaluate(ctx, run, udpIn(mustTS(t, 12), "198.51.100.90", 53, 40000))
	if err != nil || res.Decision.Accepted || res.Decision.Code != model.CodeLateReturnExpired {
		t.Fatalf("expired return: err=%v code=%s", err, res.Decision.Code)
	}
	// Wrong peer to a live mapping is endpoint-filtered.
	res, _ = eng.Evaluate(ctx, run, udpOut(mustTS(t, 13), "10.1.1.30", 7001, "198.51.100.91", 53))
	port := res.Decision.Mapping.MappedPort
	res, err = eng.Evaluate(ctx, run, udpIn(mustTS(t, 14), "198.51.100.250", 53, port))
	if err != nil || res.Decision.Accepted || res.Decision.Code != model.CodeEndpointFiltered {
		t.Fatalf("impostor: err=%v code=%s", err, res.Decision.Code)
	}
}

// TestConcurrentFirstPackets hammers the engine: N goroutines create flows
// simultaneously; every accepted port must be unique and exhaustion must be
// reported cleanly when the pool is smaller than N.
func TestConcurrentFirstPackets(t *testing.T) {
	rl := testutil.NewRun(t, "nat")
	defer rl.Finish(!t.Failed())
	eng, _ := newEngine(t, testCfg()) // pool 40000-40002: 3 ports
	ctx := context.Background()
	const run = "conc"

	const n = 25
	start := make(chan struct{})
	var wg sync.WaitGroup
	results := make([]*nat.RunResult, n)
	errs := make([]error, n)
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(i int) {
			defer wg.Done()
			pkt := udpOut(mustTS(t, 0), "10.1.1.40", uint16(8000+i), "198.51.100."+strconv.Itoa(100+i), 53)
			<-start
			results[i], errs[i] = eng.Evaluate(ctx, run, pkt)
		}(i)
	}
	close(start)
	wg.Wait()

	seen := map[uint16]int{}
	accepted, exhausted := 0, 0
	for i := 0; i < n; i++ {
		if errs[i] != nil {
			t.Fatalf("goroutine %d compute error: %v", i, errs[i])
		}
		d := results[i].Decision
		if d.Accepted {
			accepted++
			seen[d.Mapping.MappedPort]++
		} else {
			if d.Code != model.CodePortExhausted {
				t.Fatalf("goroutine %d unexpected rejection %s/%s", i, d.Category, d.Code)
			}
			exhausted++
		}
	}
	if accepted != 3 || exhausted != n-3 {
		t.Fatalf("accepted=%d exhausted=%d, want 3 and %d", accepted, exhausted, n-3)
	}
	for port, count := range seen {
		if count != 1 {
			t.Fatalf("port %d allocated %d times", port, count)
		}
	}
	rl.Infof("concurrent: accepted=%d exhausted=%d unique-ports=%d run=%s", accepted, exhausted, len(seen), rl.RunID)
}

// TestClockRewindDoesNotRevive verifies stale timestamps never extend a life.
func TestClockRewindDoesNotRevive(t *testing.T) {
	eng, _ := newEngine(t, testCfg())
	ctx := context.Background()
	const run = "rewind"

	// Mapping at t=0 expires at t=10.
	if r, _ := eng.Evaluate(ctx, run, udpOut(mustTS(t, 0), "10.1.1.50", 9000, "198.51.100.100", 53)); !r.Decision.Accepted {
		t.Fatal("setup rejected")
	}
	// t=12 sweeps it; a new flow occupies 40000 and fill the pool with 40001,40002.
	if r, _ := eng.Evaluate(ctx, run, udpOut(mustTS(t, 12), "10.1.1.51", 9001, "198.51.100.101", 53)); r.Decision.Swept != 1 || r.Decision.Mapping.MappedPort != 40000 {
		t.Fatalf("t=12 sweep/reuse wrong: swept=%d port=%d", r.Decision.Swept, r.Decision.Mapping.MappedPort)
	}
	if r, _ := eng.Evaluate(ctx, run, udpOut(mustTS(t, 12.1), "10.1.1.52", 9002, "198.51.100.102", 53)); !r.Decision.Accepted {
		t.Fatal("pool fill 2 rejected")
	}
	if r, _ := eng.Evaluate(ctx, run, udpOut(mustTS(t, 12.2), "10.1.1.53", 9003, "198.51.100.103", 53)); !r.Decision.Accepted {
		t.Fatal("pool fill 3 rejected")
	}
	// Stale t=5 outbound: clock rewind flagged, effective clock still t=12.2 ->
	// no revival and pool exhausted.
	r, _ := eng.Evaluate(ctx, run, udpOut(mustTS(t, 5), "10.1.1.50", 9000, "198.51.100.100", 53))
	if !r.Decision.ClockRewind {
		t.Fatalf("stale packet must flag clock_rewind")
	}
	if r.Decision.EffectiveAt != mustTS(t, 12.2) {
		t.Fatalf("effective clock = %v, want t=12.2", r.Decision.EffectiveAt)
	}
	if r.Decision.Accepted || r.Decision.Code != model.CodePortExhausted {
		t.Fatalf("stale outbound accepted=%v code=%s", r.Decision.Accepted, r.Decision.Code)
	}
	// Stale t=6 inbound targets port 40000 now owned by a different flow.
	r, _ = eng.Evaluate(ctx, run, udpIn(mustTS(t, 6), "198.51.100.100", 53, 40000))
	if r.Decision.Accepted || r.Decision.Code != model.CodeEndpointFiltered || !r.Decision.ClockRewind {
		t.Fatalf("stale inbound accepted=%v code=%s rewind=%v", r.Decision.Accepted, r.Decision.Code, r.Decision.ClockRewind)
	}
}

// TestInvalidInputs checks a representative set of invalid_input codes.
func TestInvalidInputs(t *testing.T) {
	eng, _ := newEngine(t, testCfg())
	ctx := context.Background()
	const run = "invalid"

	cases := []struct {
		name string
		pkt  model.Packet
		code string
	}{
		{"zero ts", model.Packet{Direction: model.Outbound, FiveTuple: model.FiveTuple{SrcIP: "10.0.0.1", SrcPort: 1, DstIP: "1.1.1.1", DstPort: 1, Protocol: model.UDP}}, model.CodeInvalidTimestamp},
		{"bad direction", withDirection(udpOut(mustTS(t, 0), "10.0.0.1", 1, "1.1.1.1", 1), "weird"), model.CodeBadDirection},
		{"icmp", withProto(udpOut(mustTS(t, 0), "10.0.0.1", 1, "1.1.1.1", 1), "ICMP"), model.CodeUnsupportedProto},
		{"udp flags", withFlags(udpOut(mustTS(t, 0), "10.0.0.1", 1, "1.1.1.1", 1), "ACK"), model.CodeUDPFlags},
		{"bad src ip", udpOut(mustTS(t, 0), "10.0.0.x", 1, "1.1.1.1", 1), model.CodeBadSrcIP},
		{"zero src port", udpOut(mustTS(t, 0), "10.0.0.1", 0, "1.1.1.1", 1), model.CodeBadSrcPort},
		{"fragment", withFrag(udpOut(mustTS(t, 0), "10.0.0.1", 1, "1.1.1.1", 1)), model.CodeFragment},
		{"src not private", udpOut(mustTS(t, 0), "8.8.8.8", 1, "1.1.1.1", 1), model.CodeSrcNotPrivate},
		{"tcp no flags", tcpOut(mustTS(t, 0), 1, "1.1.1.1", 1, ""), model.CodeBadFlag},
	}
	for i, tc := range cases {
		res, err := eng.Evaluate(ctx, run, tc.pkt)
		if err != nil {
			t.Fatalf("%s: compute error: %v", tc.name, err)
		}
		if res.Decision.Accepted {
			t.Fatalf("%s: accepted, want rejection", tc.name)
		}
		if res.Decision.Category != model.CatInvalidInput {
			t.Fatalf("%s: category=%s, want invalid_input", tc.name, res.Decision.Category)
		}
		if res.Decision.Code != tc.code {
			t.Fatalf("%s: code=%s, want %s (case %d)", tc.name, res.Decision.Code, tc.code, i)
		}
	}
}

// helpers for mutating packets in the invalid-input table.

func withProto(p model.Packet, proto model.Protocol) model.Packet {
	p.FiveTuple.Protocol = proto
	return p
}
func withFlags(p model.Packet, f string) model.Packet              { p.Flags = f; return p }
func withDirection(p model.Packet, d model.Direction) model.Packet { p.Direction = d; return p }
func withFrag(p model.Packet) model.Packet {
	p.Fragment = &model.FragInfo{Offset: 16, MoreFragments: true}
	return p
}
