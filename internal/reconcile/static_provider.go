package reconcile

import "context"

// StaticProvider is a local, in-memory DesiredProvider used by the demo
// server and tests. It represents a synthetic external participant (the
// "compute fleet inventory") without any network dependency.
type StaticProvider struct {
	desired map[string][]DesiredInstance
}

// NewStaticProvider constructs a provider from a cluster-id keyed map.
func NewStaticProvider(m map[string][]DesiredInstance) *StaticProvider {
	cp := map[string][]DesiredInstance{}
	for k, v := range m {
		cp[k] = append([]DesiredInstance(nil), v...)
	}
	return &StaticProvider{desired: cp}
}

// Desired implements DesiredProvider.
func (p *StaticProvider) Desired(_ context.Context, clusterID string) ([]DesiredInstance, error) {
	return append([]DesiredInstance(nil), p.desired[clusterID]...), nil
}

// Set replaces the desired set (test helper).
func (p *StaticProvider) Set(clusterID string, d []DesiredInstance) {
	p.desired[clusterID] = append([]DesiredInstance(nil), d...)
}
