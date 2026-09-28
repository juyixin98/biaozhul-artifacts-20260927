package kernel_test

import (
	"math"
	"testing"

	"clsnap/internal/apperr"
	"clsnap/internal/kernel"
	"clsnap/internal/protocol"
)

func seed() []protocol.Account {
	return []protocol.Account{
		{ID: "a", Owner: "n1", Balance: 100},
		{ID: "b", Owner: "n1", Balance: 20},
	}
}

func TestDebitCreditHappyPath(t *testing.T) {
	l, err := kernel.NewLedger("n1", seed())
	if err != nil {
		t.Fatal(err)
	}
	before, _ := l.Total()
	if before != 120 {
		t.Fatalf("seed total = %d, want 120", before)
	}
	if err := l.Debit(protocol.Transfer{Ref: "t1", From: "a", To: "c", Amount: 30}); err != nil {
		t.Fatalf("debit: %v", err)
	}
	if err := l.Credit(protocol.Transfer{Ref: "t1", From: "a", To: "b", Amount: 30}); err != nil {
		t.Fatalf("credit: %v", err)
	}
	a, _ := l.Account("a")
	b, _ := l.Account("b")
	if a.Balance != 70 || b.Balance != 50 {
		t.Fatalf("balances a=%d b=%d, want 70/50", a.Balance, b.Balance)
	}
	if after, _ := l.Total(); after != 120 {
		t.Fatalf("local transfer changed total: %d", after)
	}
}

func TestDebitErrorsAreClassified(t *testing.T) {
	l, _ := kernel.NewLedger("n1", seed())

	cases := []struct {
		name string
		t    protocol.Transfer
		want apperr.Kind
		code string
	}{
		{"zero amount", protocol.Transfer{Ref: "x", From: "a", To: "b", Amount: 0}, apperr.KindInput, apperr.CodeBadAmount},
		{"same from/to", protocol.Transfer{Ref: "x", From: "a", To: "a", Amount: 1}, apperr.KindInput, apperr.CodeMalformed},
		{"unknown source", protocol.Transfer{Ref: "x", From: "zz", To: "b", Amount: 1}, apperr.KindInput, apperr.CodeUnknownAccount},
		{"insufficient funds", protocol.Transfer{Ref: "x", From: "b", To: "a", Amount: 21}, apperr.KindInput, apperr.CodeInsufficientFund},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := l.Debit(tc.t)
			ae, ok := apperr.As(err)
			if !ok {
				t.Fatalf("expected structured error, got %v", err)
			}
			if ae.Kind != tc.want || ae.Code != tc.code {
				t.Fatalf("got %s/%s, want %s/%s", ae.Kind, ae.Code, tc.want, tc.code)
			}
		})
	}
}

func TestDuplicateRefRejected(t *testing.T) {
	l, _ := kernel.NewLedger("n1", seed())
	tx := protocol.Transfer{Ref: "dup", From: "a", To: "b", Amount: 5}
	if err := l.Debit(tx); err != nil {
		t.Fatal(err)
	}
	err := l.Debit(tx)
	if !apperr.IsKind(err, apperr.KindConflict) {
		t.Fatalf("duplicate ref kind = %v, want state_conflict", err)
	}
	a, _ := l.Account("a")
	if a.Balance != 95 {
		t.Fatalf("duplicate debit applied twice: a=%d", a.Balance)
	}
}

func TestCreditUnknownAccountIsInvariantFailure(t *testing.T) {
	l, _ := kernel.NewLedger("n1", seed())
	err := l.Credit(protocol.Transfer{Ref: "x", From: "a", To: "ghost", Amount: 1})
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindFailure || ae.Code != apperr.CodeKernelInvariant {
		t.Fatalf("got %v, want computation_failure/kernel_invariant", err)
	}
}

func TestSeedRejectsForeignAndDuplicateAccounts(t *testing.T) {
	if _, err := kernel.NewLedger("n1", []protocol.Account{{ID: "a", Owner: "n2", Balance: 1}}); !apperr.IsKind(err, apperr.KindInput) {
		t.Fatalf("foreign owner: %v", err)
	}
	if _, err := kernel.NewLedger("n1", []protocol.Account{
		{ID: "a", Balance: 1}, {ID: "a", Balance: 2},
	}); !apperr.IsKind(err, apperr.KindInput) {
		t.Fatalf("duplicate seed account: %v", err)
	}
}

func TestSnapshotIsDeepAndStableUnderMutation(t *testing.T) {
	l, _ := kernel.NewLedger("n1", seed())
	snap, total, err := l.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	if total != 120 {
		t.Fatalf("total %d", total)
	}
	cp := snap["a"]
	cp.Balance = 1 // mutate only the returned snapshot copy
	snap["a"] = cp
	again, _ := l.Account("a")
	if again.Balance != 100 {
		t.Fatalf("snapshot aliased live state: live a=%d", again.Balance)
	}
}

// Sentinel overflow guard: crediting near MaxUint64 must be a classified
// computation failure rather than silent wraparound.
func TestCreditOverflow(t *testing.T) {
	l, _ := kernel.NewLedger("n1", []protocol.Account{{ID: "a", Balance: math.MaxUint64 - 2}})
	err := l.Credit(protocol.Transfer{Ref: "x", From: "b", To: "a", Amount: 5})
	ae, ok := apperr.As(err)
	if !ok || ae.Kind != apperr.KindFailure || ae.Code != apperr.CodeKernelInvariant {
		t.Fatalf("overflow produced %v", err)
	}
}
