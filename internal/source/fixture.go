// Package source adapts local, synthetic desired-state inputs into validated
// domain snapshots. There are no real cluster credentials: the only shipped
// implementation reads a JSON fixture file, which stands in for an
// informer/list-watch adapter in a real deployment.
package source

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"

	"netpolicy/internal/domain"
)

// Fixture is the on-disk desired-state format. It deliberately mirrors the
// wire shapes of namespaces/pods/networkpolicies so a future Kubernetes
// adapter can produce the same struct without translation.
type Fixture struct {
	Namespaces []domain.Namespace `json:"namespaces"`
	Endpoints  []domain.Endpoint  `json:"endpoints"`
	Policies   []domain.Policy    `json:"policies"`
}

// FixtureSource reads desired state from one JSON file on every Fetch. The
// reconcile loop polls it; swapping the file between runs simulates a
// watch event.
type FixtureSource struct {
	Path string
	// Fault, when non-nil, short-circuits Fetch. Tests use it to exercise
	// reconcile failure categories without touching the disk.
	Fault error
}

// Fetch reads, parses and structurally validates the fixture. The returned
// snapshot carries no revision: revisioning is the store's responsibility,
// which keeps label snapshots and policy versions assigned in one place.
func (f *FixtureSource) Fetch(ctx context.Context) (*domain.Snapshot, error) {
	if f.Fault != nil {
		return nil, f.Fault
	}
	raw, err := os.ReadFile(f.Path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, domain.ValidationError{Kind: domain.ErrSourceNotFound, Detail: f.Path}
		}
		return nil, fmt.Errorf("read fixture %s: %w", f.Path, err)
	}
	return Parse(raw)
}

// Parse decodes raw fixture bytes, validates and normalizes them.
func Parse(raw []byte) (*domain.Snapshot, error) {
	var fx Fixture
	if err := json.Unmarshal(raw, &fx); err != nil {
		return nil, domain.ValidationError{Kind: domain.ErrSourceSyntax, Detail: err.Error()}
	}
	snap := &domain.Snapshot{
		Namespaces: fx.Namespaces,
		Endpoints:  fx.Endpoints,
		Policies:   fx.Policies,
	}
	if err := snap.Validate(); err != nil {
		return nil, err
	}
	snap.Normalize()
	h, err := contentHash(snap)
	if err != nil {
		return nil, fmt.Errorf("hash fixture: %w", err)
	}
	snap.SourceHash = h
	return snap, nil
}

// contentHash hashes the logical content (namespaces, endpoints, policies)
// but not the revision, so identical inputs hash identically regardless of
// which version number they were last stored under.
func contentHash(snap *domain.Snapshot) (string, error) {
	b, err := json.Marshal(struct {
		Namespaces []domain.Namespace `json:"namespaces"`
		Endpoints  []domain.Endpoint  `json:"endpoints"`
		Policies   []domain.Policy    `json:"policies"`
	}{snap.Namespaces, snap.Endpoints, snap.Policies})
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:]), nil
}
