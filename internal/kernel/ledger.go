// Package kernel is the pure computation core: a small integer token ledger.
//
// It deliberately knows nothing about HTTP, markers, channels or databases.
// Its inputs and outputs are plain Go values, which is what makes it unit
// testable against hand-computed expected balances and lets the replay tool
// re-drive the exact same code path independently of the network.
//
// Concurrency note: standard Chandy-Lamport does not freeze a node after it
// records local state — nodes keep issuing and accepting transfers while a
// round is in progress elsewhere. Correctness comes from the FIFO marker
// rule, not from pausing. The ledger therefore has no "frozen account"
// concept; it only enforces balances, overflow and idempotency.
package kernel

import (
	"fmt"
	"math"
	"sync"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
)

// Ledger owns the accounts of one node. Account sets are disjoint across
// nodes; cross-node transfers arrive as messages and are applied with Credit.
type Ledger struct {
	mu       sync.Mutex
	nodeID   protocol.NodeID
	accounts map[string]*protocol.Account
	// seen stores idempotency keys already debited on this node. The
	// coordinator additionally persists them for dedup across restarts.
	seen map[string]bool
}

// NewLedger constructs a ledger for nodeID seeded with the given accounts.
// Every account's Owner is forced to nodeID; an account owned elsewhere is an
// input error (fixture misconfiguration).
func NewLedger(nodeID protocol.NodeID, seed []protocol.Account) (*Ledger, error) {
	l := &Ledger{
		nodeID:   nodeID,
		accounts: make(map[string]*protocol.Account),
		seen:     make(map[string]bool),
	}
	for _, a := range seed {
		if a.Owner != "" && a.Owner != nodeID {
			return nil, apperr.Inputf(apperr.CodeMalformed,
				"account %s owner %q does not belong to node %q", a.ID, a.Owner, nodeID)
		}
		a.Owner = nodeID
		if _, dup := l.accounts[a.ID]; dup {
			return nil, apperr.Inputf(apperr.CodeMalformed, "duplicate account %s in seed", a.ID)
		}
		l.accounts[a.ID] = &a
	}
	return l, nil
}

// NodeID returns the owning node.
func (l *Ledger) NodeID() protocol.NodeID { return l.nodeID }

// Snapshot returns a deep, immutable copy of all accounts plus their sum.
// This is the "record local state" step of Chandy-Lamport.
func (l *Ledger) Snapshot() (map[string]protocol.Account, uint64, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make(map[string]protocol.Account, len(l.accounts))
	var sum uint64
	for id, a := range l.accounts {
		out[id] = *a
		if sum > math.MaxUint64-a.Balance {
			return nil, 0, apperr.Failure(apperr.CodeKernelInvariant, "kernel.Snapshot",
				"total balance overflow while recording", nil)
		}
		sum += a.Balance
	}
	return out, sum, nil
}

// Total returns the sum of balances (used by status endpoints).
func (l *Ledger) Total() (uint64, error) {
	_, total, err := l.Snapshot()
	return total, err
}

// Debit applies the sender side of a transfer on the node that owns From.
// It rejects unknown accounts (input_error), bad amounts (input_error),
// insufficient funds (input_error) and duplicate refs (state_conflict).
func (l *Ledger) Debit(t protocol.Transfer) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err := t.Validate(); err != nil {
		return err
	}
	if l.seen[t.Ref] {
		return apperr.Conflictf(apperr.CodeSnapshotInProgress,
			"transfer ref %q already applied on node %q", t.Ref, l.nodeID)
	}
	a, ok := l.accounts[t.From]
	if !ok {
		return apperr.Inputf(apperr.CodeUnknownAccount,
			"debit: account %q not on node %q", t.From, l.nodeID)
	}
	if t.Amount > a.Balance {
		return apperr.Inputf(apperr.CodeInsufficientFund,
			"debit %s: balance %d < amount %d", t.Ref, a.Balance, t.Amount)
	}
	a.Balance -= t.Amount
	l.seen[t.Ref] = true
	return nil
}

// Credit applies the receiver side. An unknown destination account is a
// kernel invariant failure: the topology's account sets are fixed and
// disjoint, so an accepted transfer must always land on a known account.
func (l *Ledger) Credit(t protocol.Transfer) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if t.Amount == 0 {
		return apperr.Inputf(apperr.CodeBadAmount, "credit %s: amount must be > 0", t.Ref)
	}
	a, ok := l.accounts[t.To]
	if !ok {
		return apperr.Failure(apperr.CodeKernelInvariant, "kernel.Credit",
			fmt.Sprintf("destination account %q not on node %q for transfer %q", t.To, l.nodeID, t.Ref), nil)
	}
	if a.Balance > math.MaxUint64-t.Amount {
		return apperr.Failure(apperr.CodeKernelInvariant, "kernel.Credit",
			"balance overflow on credit of "+t.Ref, nil)
	}
	a.Balance += t.Amount
	return nil
}

// Account returns a copy of one account (or input error if unknown).
func (l *Ledger) Account(id string) (protocol.Account, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	a, ok := l.accounts[id]
	if !ok {
		return protocol.Account{}, apperr.Inputf(apperr.CodeUnknownAccount, "account %q", id)
	}
	return *a, nil
}

// Accounts returns a deep copy of the account map.
func (l *Ledger) Accounts() map[string]protocol.Account {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make(map[string]protocol.Account, len(l.accounts))
	for id, a := range l.accounts {
		out[id] = *a
	}
	return out
}

// Restore replaces all account state from a recorded local state. Used by the
// replay tool only.
func (l *Ledger) Restore(state map[string]protocol.Account) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.accounts = make(map[string]*protocol.Account, len(state))
	for id, a := range state {
		aa := a
		l.accounts[id] = &aa
	}
}
