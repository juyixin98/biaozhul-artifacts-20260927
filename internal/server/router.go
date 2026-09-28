package server

import (
	"sync"

	"clsnap/internal/protocol"
)

// StaticRouter is the fixed account -> owner table built from the three node
// configurations. Account sets are disjoint by construction.
type StaticRouter struct {
	mu    sync.RWMutex
	owner map[string]protocol.NodeID
}

// NewStaticRouter builds a router from per-node account lists.
func NewStaticRouter(seed map[protocol.NodeID][]protocol.Account) *StaticRouter {
	r := &StaticRouter{owner: make(map[string]protocol.NodeID)}
	for node, accts := range seed {
		for _, a := range accts {
			r.owner[a.ID] = node
		}
	}
	return r
}

// OwnerOf implements snapshot.Router.
func (r *StaticRouter) OwnerOf(account string) (protocol.NodeID, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	n, ok := r.owner[account]
	return n, ok
}
