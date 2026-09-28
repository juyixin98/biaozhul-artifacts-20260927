package kernel

import "time"

// This file holds the low-level state transition primitives. Both the command
// path (which builds an event batch against a scratch State) and the reducer
// (which rebuilds State on replay) call these same functions, so live mutation
// and event replay cannot diverge.

// addMember inserts a pending member (generation 0 until assigned work).
func addMember(s *State, id string, at time.Time) {
	s.Members[id] = &Member{ID: id, Generation: 0, JoinedAt: at, LastSeen: at, Active: true}
}

// dropMember removes a member record entirely.
func dropMember(s *State, id string) {
	if m := s.Members[id]; m != nil {
		m.Active = false
	}
	delete(s.Members, id)
}

// revoke withdraws a partition from oldOwner. After this call the partition has
// NO valid owner (old cleared) and waits as PENDING_GRANT for newOwner. When
// force is true the provenance is marked uncertain.
func revoke(s *State, pid int, oldOwner string, oldGen int64, newOwner string, force bool) {
	p := s.Partitions[pid]
	p.Phase = PPendingGrant
	p.PrevOwner = oldOwner
	p.PrevGeneration = oldGen
	p.PendingOwner = newOwner
	p.Owner = ""
	p.Generation = 0
	if force {
		p.Uncertain = true
	}
}

// beginRevoke moves a stable partition into REVOKING: the old owner remains the
// valid owner until it confirms release.
func beginRevoke(s *State, pid int, oldOwner string, oldGen int64, newOwner string) {
	p := s.Partitions[pid]
	p.Phase = PRevoking
	p.PrevOwner = oldOwner
	p.PrevGeneration = oldGen
	p.PendingOwner = newOwner
}

// grant gives a released partition to newOwner at newGen. An empty newOwner
// settles the partition back to an unassigned EMPTY slot (used when the last
// member departs).
func grant(s *State, pid int, newOwner string, newGen int64) {
	p := s.Partitions[pid]
	p.Owner = newOwner
	p.Generation = newGen
	p.PrevOwner = ""
	p.PrevGeneration = 0
	p.PendingOwner = ""
	if newOwner == "" {
		p.Phase = PEmpty
	} else {
		p.Phase = PStable
	}
}

// retain advances a stable owner's generation without moving the partition.
func retain(s *State, pid int, gen int64) {
	p := s.Partitions[pid]
	p.Phase = PStable
	p.Generation = gen
}

// commitOffset records a validated commit.
func commitOffset(s *State, pid int, owner string, gen int64, off int64) {
	p := s.Partitions[pid]
	p.Offset = off
	p.OffsetOwner = owner
	p.OffsetGen = gen
	p.Uncertain = false
}

// deriveMemberGens recomputes each active member's fenced generation purely
// from partition ownership:
//
//   - pinned to the revoked generation while it still owes a revocation,
//   - otherwise the maximum generation of partitions it validly owns,
//   - pending (no work, owes nothing) -> current group generation.
func deriveMemberGens(s *State) {
	for _, m := range s.Members {
		if !m.Active {
			continue
		}
		gen := s.Generation
		pinned := false
		var owned int64 = -1
		for _, p := range s.Partitions {
			if p.Phase == PRevoking && p.PrevOwner == m.ID {
				gen = p.PrevGeneration
				pinned = true
			}
			if p.Phase == PStable && p.Owner == m.ID {
				if p.Generation > owned {
					owned = p.Generation
				}
			}
		}
		if !pinned && owned >= 0 {
			gen = owned
		}
		m.Generation = gen
	}
}
