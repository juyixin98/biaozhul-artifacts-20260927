package oracle_test

import (
	"bytes"
	"testing"

	"ipfragreasm/internal/fixture"
	"ipfragreasm/internal/oracle"
)

func f(off int, b []byte, more bool) oracle.Frag {
	return oracle.Frag{Offset: off, Payload: b, More: more}
}

func TestOracleCompleteAndNoEarlyEmit(t *testing.T) {
	data := fixture.PatternPayload(32)
	// 末片先到但缺其它片：必须 pending，不提前输出。
	r := oracle.Reassemble([]oracle.Frag{f(16, data[16:], false)}, 65535, false)
	if r.Verdict != oracle.VerdictPending || r.Assembled != nil {
		t.Fatalf("仅末片应 pending 且无输出，实际 %s", r.Verdict)
	}
	// 补齐。
	r = oracle.Reassemble([]oracle.Frag{
		f(16, data[16:], false), f(0, data[:16], true),
	}, 65535, false)
	if r.Verdict != oracle.VerdictComplete || !bytes.Equal(r.Assembled, data) {
		t.Fatalf("补齐后应 complete，实际 %s", r.Verdict)
	}
}

func TestOracleRejectClasses(t *testing.T) {
	a := make([]byte, 16)
	b := make([]byte, 16)
	b[0] = 1

	// 同区间不同字节 -> overlap。
	r := oracle.Reassemble([]oracle.Frag{
		f(0, a, true), f(0, b, true),
	}, 65535, false)
	if r.Verdict != oracle.VerdictRejected || r.Category != oracle.CategoryOverlap {
		t.Fatalf("应 overlap，实际 %s/%s", r.Verdict, r.Category)
	}

	// 完全重复不计覆盖。
	r = oracle.Reassemble([]oracle.Frag{
		f(0, a, true), f(0, a, true), f(16, make([]byte, 8), false),
	}, 65535, false)
	if r.Verdict != oracle.VerdictComplete || r.Duplicates != 1 {
		t.Fatalf("完全重复应幂等完成, verdict=%s dup=%d", r.Verdict, r.Duplicates)
	}

	// MF=1 长度非 8 倍数。
	r = oracle.Reassemble([]oracle.Frag{f(0, make([]byte, 7), true)}, 65535, false)
	if r.Category != oracle.CategoryUnaligned {
		t.Fatalf("应 unaligned，实际 %s", r.Category)
	}

	// 冲突末片。
	r = oracle.Reassemble([]oracle.Frag{
		f(0, make([]byte, 8), false), f(8, make([]byte, 8), false),
	}, 65535, false)
	if r.Category != oracle.CategoryConflictLast {
		t.Fatalf("两个不同终点末片应 conflict_last，实际 %s", r.Category)
	}

	// 超长。
	r = oracle.Reassemble([]oracle.Frag{f(0, make([]byte, 96), true)}, 64, false)
	if r.Category != oracle.CategoryTooLarge {
		t.Fatalf("应 too_large，实际 %s", r.Category)
	}
}

func TestOracleTimeout(t *testing.T) {
	r := oracle.Reassemble([]oracle.Frag{f(0, make([]byte, 8), true)}, 65535, true)
	if r.Verdict != oracle.VerdictTimedOut {
		t.Fatalf("缺末片且超时应 timed_out，实际 %s", r.Verdict)
	}
}
