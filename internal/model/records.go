package model

import "time"

// DeletedOwner is a tombstone record: when an owner resource is
// physically deleted we remember its incarnation (namespace/name/UID)
// together with the propagation policy of its delete. Cascade
// evaluation uses the policy to decide whether a remaining reference
// means "orphan the dependent" or "delete the dependent", and tests can
// audit the decision basis after the owner row itself is gone.
type DeletedOwner struct {
	UID       string
	Namespace string
	Name      string
	Policy    string
	DeletedAt time.Time
	Tick      int64
}

// CycleMark persists one diagnosed ownership cycle (a closed chain of
// foreground blocking owner references) and which node was chosen to
// break it. The controller never blocks forever on a cycle: each tick
// that detects one deterministically breaks it at the lexicographically
// smallest UID of the cycle.
type CycleMark struct {
	ID        int64
	Tick      int64
	Cycle     []string // ordered UIDs forming the closed chain
	BrokenUID string   // node at which the block was removed
	Reason    string
	NotedAt   time.Time
}
