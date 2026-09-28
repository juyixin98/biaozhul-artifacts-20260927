// Package token is the compute kernel: a minimal integer token ledger.
//
// It deliberately knows nothing about snapshots, networks or HTTP. A snapshot
// freezes a copy of the balances at one logical instant; transfers keep
// mutating the live ledger afterwards.
package token

import (
	"fmt"
	"sort"
	"sync"

	"clsnap/internal/errs"
)

// Ledger holds non-negative integer balances keyed by account id.
type Ledger struct {
	mu       sync.Mutex
	balances map[string]int64
}

func NewLedger(initial map[string]int64) (*Ledger, error) {
	l := &Ledger{balances: make(map[string]int64, len(initial))}
	for acct, v := range initial {
		if v < 0 {
			return nil, errs.New(errs.ClassInputInvalid, errs.CodeBadAmount,
				fmt.Sprintf("initial balance for %q is negative: %d", acct, v), nil)
		}
		l.balances[acct] = v
	}
	return l, nil
}

// Debit removes amount from account. A well-formed request that overdrafts is a
// compute failure (422), distinct from malformed input (400).
func (l *Ledger) Debit(account string, amount int64) error {
	if amount <= 0 {
		return errs.New(errs.ClassInputInvalid, errs.CodeBadAmount,
			fmt.Sprintf("debit amount must be positive, got %d", amount), nil)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	bal, ok := l.balances[account]
	if !ok {
		return errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer,
			fmt.Sprintf("unknown account %q", account), nil)
	}
	if bal < amount {
		return errs.New(errs.ClassComputeFailure, errs.CodeInsufficientFunds,
			fmt.Sprintf("account %q has %d, cannot debit %d", account, bal, amount), nil)
	}
	l.balances[account] = bal - amount
	return nil
}

// Credit adds amount to account.
func (l *Ledger) Credit(account string, amount int64) error {
	if amount <= 0 {
		return errs.New(errs.ClassInputInvalid, errs.CodeBadAmount,
			fmt.Sprintf("credit amount must be positive, got %d", amount), nil)
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if _, ok := l.balances[account]; !ok {
		return errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer,
			fmt.Sprintf("unknown account %q", account), nil)
	}
	l.balances[account] += amount
	return nil
}

// Balance returns the current live balance.
func (l *Ledger) Balance(account string) int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.balances[account]
}

// Snapshot returns a deep, point-in-time copy safe to retain while the ledger
// keeps mutating.
func (l *Ledger) Snapshot() map[string]int64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make(map[string]int64, len(l.balances))
	for k, v := range l.balances {
		out[k] = v
	}
	return out
}

// Total returns the sum of all balances. For a closed token network this is the
// conservation invariant every complete snapshot must satisfy.
func Total(balances map[string]int64) int64 {
	var t int64
	for _, v := range balances {
		t += v
	}
	return t
}

// SortedAccount returns account ids in stable order for reports.
func SortedAccounts(balances map[string]int64) []string {
	out := make([]string, 0, len(balances))
	for k := range balances {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
