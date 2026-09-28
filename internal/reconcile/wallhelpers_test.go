package reconcile

import (
	"context"
	"testing"
	"time"

	"admission/internal/admission"
	"admission/internal/plugins"
	"admission/internal/quota"
	"admission/internal/service"
	"admission/internal/storage"
	"admission/internal/types"
)

func wallDeps(t *testing.T) (*storage.SQLiteStore, *admission.Pipeline, *quota.MemoryLedger, *plugins.DelayPlugin) {
	store, err := storage.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	slow := &plugins.DelayPlugin{Name_: "sloww", Delay: time.Second, AllowedPaths: []string{"/spec/extra/d"}}
	cfg := admission.Config{
		DefaultTimeoutMS: 250, MaxMutationPasses: 3,
		Defaults: []admission.MutatorSpec{
			{Plugin: &plugins.ReplicaDefaulter{Default: 3}, FailurePolicy: admission.FailClose},
			{Plugin: &plugins.ResourcesDefaulter{DefaultCPU: "250m", DefaultMemory: "128Mi"}, FailurePolicy: admission.FailClose},
		},
		Mutators: []admission.MutatorSpec{{Plugin: slow, TimeoutMS: 20, FailurePolicy: admission.FailClose}},
		Validators: []admission.ValidatorSpec{
			{Plugin: &plugins.ReplicaRangeValidator{Min: 1, Max: 10}, FailurePolicy: admission.FailClose},
		},
	}
	pipe, err := admission.New(cfg, nil)
	if err != nil {
		t.Fatal(err)
	}
	return store, pipe, nil, slow
}

func wallSvc(t *testing.T, pipe *admission.Pipeline, store *storage.SQLiteStore, ledger *quota.MemoryLedger) *service.Service {
	var led quota.Adapter
	if ledger != nil {
		led = ledger
	}
	svc, err := service.New(service.Deps{Pipeline: pipe, Store: store, Ledger: led})
	if err != nil {
		t.Fatal(err)
	}
	return svc
}

func wallReview(uid string) types.Review {
	return types.Review{UID: uid, Operation: "CREATE", Object: types.Object{
		APIVersion: "v", Kind: "Workload",
		Metadata: types.Metadata{Namespace: "ns", Name: uid},
		Spec:     types.Spec{CPU: "500m", Memory: "128Mi"},
	}}
}

func wallAudit(t *testing.T, store *storage.SQLiteStore, uid string) []types.AuditEvent {
	events, err := store.RecentAudit(context.Background(), 50)
	if err != nil {
		t.Fatal(err)
	}
	var out []types.AuditEvent
	for _, e := range events {
		if e.UID == uid {
			out = append(out, e)
		}
	}
	return out
}
