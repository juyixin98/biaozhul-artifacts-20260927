// Package config loads the node configuration files shipped under configs/.
// One file describes one node process (its HTTP address, peers, account seed
// and the Postgres DSN). All values are local/synthetic — no external
// accounts.
package config

import (
	"encoding/json"
	"os"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
)

// Node is one process configuration.
type Node struct {
	ID            protocol.NodeID   `json:"id"`
	Listen        string            `json:"listen"`          // host:port for stdlib HTTP
	Peers         []Peer            `json:"peers"`           // other two processes
	Accounts      []protocol.Account `json:"accounts"`       // synthetic seed ledger
	StoreDriver   string            `json:"store_driver"`    // "postgres" | "memory"
	PostgresDSN   string            `json:"postgres_dsn"`    // used when driver=postgres
	LogDir        string            `json:"log_dir"`         // run journal output dir
	FlushMS       int               `json:"flush_ms"`        // outbox pump interval (HTTP mode)
}

// Peer names one neighbor and its HTTP base URL.
type Peer struct {
	ID  protocol.NodeID `json:"id"`
	URL string          `json:"url"`
}

// Load reads and validates a node config.
func Load(path string) (*Node, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, apperr.Failure(apperr.CodeStoreIO, "config.Load", "read "+path, err)
	}
	var n Node
	if err := json.Unmarshal(b, &n); err != nil {
		return nil, apperr.Inputf(apperr.CodeMalformed, "config %s: %v", path, err)
	}
	if err := n.Validate(); err != nil {
		return nil, err
	}
	return &n, nil
}

// Validate checks the fields used by every code path.
func (n *Node) Validate() error {
	if err := n.ID.Valid(); err != nil {
		return apperr.Inputf(apperr.CodeMalformed, "config: %v", err)
	}
	if n.Listen == "" {
		return apperr.Inputf(apperr.CodeMalformed, "config %s: listen required", n.ID)
	}
	if n.StoreDriver != "postgres" && n.StoreDriver != "memory" {
		return apperr.Inputf(apperr.CodeMalformed,
			"config %s: store_driver must be postgres or memory", n.ID)
	}
	if n.StoreDriver == "postgres" && n.PostgresDSN == "" {
		return apperr.Inputf(apperr.CodeMalformed,
			"config %s: postgres_dsn required with store_driver=postgres", n.ID)
	}
	ids := map[protocol.NodeID]bool{n.ID: true}
	for _, p := range n.Peers {
		if err := p.ID.Valid(); err != nil {
			return apperr.Inputf(apperr.CodeMalformed, "config peer: %v", err)
		}
		if p.ID == n.ID {
			return apperr.Inputf(apperr.CodeMalformed, "node %s lists itself as peer", n.ID)
		}
		if ids[p.ID] {
			return apperr.Inputf(apperr.CodeMalformed, "duplicate peer %s", p.ID)
		}
		ids[p.ID] = true
		if n.StoreDriver == "postgres" && p.URL == "" {
			return apperr.Inputf(apperr.CodeMalformed, "peer %s needs url", p.ID)
		}
	}
	if len(n.Peers) != 2 {
		return apperr.Inputf(apperr.CodeMalformed,
			"node %s must define exactly 2 peers for the 3-process topology, got %d", n.ID, len(n.Peers))
	}
	seenAccts := map[string]bool{}
	for _, a := range n.Accounts {
		if a.ID == "" {
			return apperr.Inputf(apperr.CodeMalformed, "empty account id")
		}
		if seenAccts[a.ID] {
			return apperr.Inputf(apperr.CodeMalformed, "duplicate account %s", a.ID)
		}
		seenAccts[a.ID] = true
	}
	return nil
}

// PeerIDs returns just the peer id list in config order.
func (n *Node) PeerIDs() []protocol.NodeID {
	out := make([]protocol.NodeID, len(n.Peers))
	for i, p := range n.Peers {
		out[i] = p.ID
	}
	return out
}
