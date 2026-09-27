package reassembly

import (
	"context"
	"testing"

	"tcpreasm/internal/config"
)

func mkDir(policy config.OverlapPolicy) *dirAssembler {
	d := &dirAssembler{
		flowKey: "f", genIndex: 1, direction: "c2s",
		policy: policy, maxBuffer: 1 << 20, evTail: 256 << 10,
		hasISN: true, isn: 99, synAbs: 1000, rcvNxt: 1001,
	}
	return d
}

func TestIdenticalRetransmitNotEmittedTwice(t *testing.T) {
	d := mkDir(config.PolicyFirstWins)
	first := []byte("ABCDEFGHIJ")
	dup, conflicts := d.insert(1001, first, "p1")
	if dup != 0 || len(conflicts) != 0 {
		t.Fatalf("first insert: dup=%d conflicts=%d", dup, len(conflicts))
	}
	emits, _ := d.deliver(context.Background(), "p1")
	if len(emits) != 1 || d.rcvNxt != 1011 {
		t.Fatalf("first delivery broken: emits=%d nxt=%d", len(emits), d.rcvNxt)
	}
	// Identical retransmission of the delivered range.
	dup, conflicts = d.insert(1001, first, "p1-rex")
	if dup != 10 {
		t.Fatalf("identical retransmit must dedup all 10 bytes, got %d", dup)
	}
	if len(conflicts) != 0 {
		t.Fatalf("identical bytes must not be conflicts, got %d", len(conflicts))
	}
	emits, _ = d.deliver(context.Background(), "p1-rex")
	if len(emits) != 0 {
		t.Fatalf("retransmission emitted %d new chunks", len(emits))
	}
}

func TestContradictionDeliveredImmutable(t *testing.T) {
	d := mkDir(config.PolicyLastWins) // even last-wins cannot rewrite delivered
	_, _ = d.insert(1001, []byte("ABCDEFGHIJ"), "p1")
	_, _ = d.deliver(context.Background(), "p1")
	bad := []byte("AXCDEFGHIJ")
	_, conflicts := d.insert(1001, bad, "p2")
	if len(conflicts) != 1 || !conflicts[0].delivered {
		t.Fatalf("want one delivered conflict, got %+v", conflicts)
	}
	if conflicts[0].startAbs != 1002 || conflicts[0].endAbs != 1003 {
		t.Fatalf("conflict must pinpoint byte 1002, got [%d,%d)", conflicts[0].startAbs, conflicts[0].endAbs)
	}
	if got, ok := d.evidenceAt(1002, 1003); !ok || got[0] != 'B' {
		t.Fatalf("delivered byte must remain B, got %q ok=%v", got, ok)
	}
}

func TestPoliciesOverBufferedOverlap(t *testing.T) {
	type polCase struct {
		policy                    config.OverlapPolicy
		want16                    byte
		wantDeliveredAfterFillLen uint64
		quarantine                bool
	}
	// Offsets 0..29 carry RangeStream(30, 50) i.e. byte at offset i is
	// 0x32+i; offset 16 is therefore 0x42 ('B'), offset 19 is 0x45 ('E').
	for _, tc := range []polCase{
		{config.PolicyFirstWins, 0x42, 30, false},
		{config.PolicyLastWins, 'X', 30, false},
		{config.PolicyQuarantine, 0, 16, true},
	} {
		t.Run(string(tc.policy), func(t *testing.T) {
			d := mkDir(tc.policy)
			// Buffer offsets 10..19: the real bytes 0x3c..0x45.
			inc := []byte{0x3c, 0x3d, 0x3e, 0x3f, 0x40, 0x41, 0x42, 0x43, 0x44, 0x45}
			_, _ = d.insert(1011, inc, "inc") // absolute = synAbs+1+off
			// Overlap at offsets 16..19 with contradictory X, agreeing tail 20..25.
			cover := append([]byte("XXXX"), []byte{0x46, 0x47, 0x48, 0x49, 0x4a, 0x4b}...)
			_, conflicts := d.insert(1017, cover, "new") // offsets 16..25
			if len(conflicts) != 1 || conflicts[0].startAbs != 1017 || conflicts[0].endAbs != 1021 {
				t.Fatalf("conflict range wrong: %+v", conflicts)
			}
			// Fill offset 0..9 (absolute 1001..1010) to advance delivery.
			_, _ = d.insert(1001, []byte("0123456789"), "fill-head")
			// Fill offsets 26..29.
			_, _ = d.insert(1027, []byte("WXYZ"), "fill-tail")
			emits, _ := d.deliver(context.Background(), "fill-head")
			total := d.rcvNxt - 1001
			if total != tc.wantDeliveredAfterFillLen {
				t.Fatalf("delivered=%d want %d", total, tc.wantDeliveredAfterFillLen)
			}
			if tc.quarantine {
				if len(d.poisoned) == 0 {
					t.Fatal("quarantine must retain held copies")
				}
				return
			}
			// Assemble delivered bytes and check offset 16.
			var got []byte
			for _, e := range emits {
				got = append(got, e.data...)
			}
			if len(got) < 17 || got[16] != tc.want16 {
				t.Fatalf("byte 16 = %q want %q (delivered %d bytes)", got[16], tc.want16, len(got))
			}
		})
	}
}

func TestSYNAndFINConsumeOne(t *testing.T) {
	// Directly exercise the engine-level accounting through dirAssembler
	// state: after SYN rcvNxt=ISN+1; FIN sits one after the data.
	d := mkDir(config.PolicyFirstWins)
	if d.off(d.rcvNxt) != 0 {
		t.Fatal("first data byte must be stream offset 0, SYN consumed one")
	}
	d.finSeen, d.finAbs = true, 1011 // FIN right after ten data bytes
	_, _ = d.insert(1001, []byte("0123456789"), "p")
	_, _ = d.deliver(context.Background(), "p")
	if !d.finDone {
		t.Fatal("FIN must complete once data through finAbs-1 is delivered")
	}
	if d.deliveredOffset() != 10 {
		t.Fatalf("FIN must not add a data byte: delivered=%d", d.deliveredOffset())
	}
}

func TestOutOfOrderOnlyContiguousDelivered(t *testing.T) {
	d := mkDir(config.PolicyFirstWins)
	// Bytes 10..19 arrive first (buffered behind a hole), then 0..9.
	_, _ = d.insert(1011, []byte("KLMNOPQRST"), "late2")
	emits, _ := d.deliver(context.Background(), "late2")
	if len(emits) != 0 {
		t.Fatal("nothing must be delivered while offset 0 is missing")
	}
	_, _ = d.insert(1001, []byte("ABCDEFGHIJ"), "late1")
	emits, _ = d.deliver(context.Background(), "late1")
	if len(emits) == 0 {
		t.Fatal("filling the hole must release buffered bytes")
	}
	var got []byte
	for _, e := range emits {
		got = append(got, e.data...)
	}
	if string(got) != "ABCDEFGHIJKLMNOPQRST" {
		t.Fatalf("reassembled stream wrong: %q", got)
	}
}
