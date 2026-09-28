package replay_test

import (
	"context"
	"testing"

	"pvsim/model"
	"pvsim/replay"
	"pvsim/store"
)

const miniScenario = `{
  "name": "mini", "max_steps": 100,
  "routers": [{"name":"r9","asn":65009},{"name":"r1","asn":65001}],
  "sessions": [{"id":"s91","a":"r9","b":"r1","type":"ebgp"}],
  "events": [{"seq":1,"router":"r1","peer":"r9","kind":"update","prefix":"P",
              "attrs":{"as_path":[65009],"origin":"igp"}}]
}`

func envelope(t *testing.T, id string) []byte {
	t.Helper()
	s := `{"scenario":` + miniScenario + `}`
	if id != "" {
		s = `{"run_id":"` + id + `","scenario":` + miniScenario + `}`
	}
	return []byte(s)
}

func newSvc(t *testing.T) *replay.Service {
	t.Helper()
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { st.Close() })
	return replay.NewService(st)
}

func wantKind(t *testing.T, err error, k model.Kind, code string) {
	t.Helper()
	if err == nil {
		t.Fatalf("expected %s/%s error, got nil", k, code)
	}
	me, ok := model.AsError(err)
	if !ok {
		t.Fatalf("err %v is not typed", err)
	}
	if me.Kind != k || me.Code != code {
		t.Fatalf("err = %s/%s, want %s/%s", me.Kind, me.Code, k, code)
	}
}

func TestSubmitAndGet(t *testing.T) {
	svc := newSvc(t)
	ctx := context.Background()
	sum, res, err := svc.Submit(ctx, envelope(t, "abc"), "")
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if sum.RunID != "abc" || !sum.Converged {
		t.Fatalf("summary = %+v", sum)
	}
	if !res.Converged || res.Steps <= 0 {
		t.Fatalf("result = %+v", res)
	}
	got, _, err := svc.Get(ctx, "abc")
	if err != nil || got.RunID != "abc" {
		t.Fatalf("get: %v %+v", err, got)
	}
}

func TestSubmitAutoID(t *testing.T) {
	svc := newSvc(t)
	sum, _, err := svc.Submit(context.Background(), envelope(t, ""), "")
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	if len(sum.RunID) == 0 || sum.RunID[:4] != "run-" {
		t.Fatalf("auto run id = %q", sum.RunID)
	}
}

func TestSyntaxAndValidationErrors(t *testing.T) {
	svc := newSvc(t)
	ctx := context.Background()
	_, _, err := svc.Submit(ctx, []byte(`{not json`), "")
	wantKind(t, err, model.KindInput, "PAYLOAD_SYNTAX")

	_, _, err = svc.Submit(ctx, []byte(`{}`), "")
	wantKind(t, err, model.KindInput, "MISSING_SCENARIO")

	_, _, err = svc.Submit(ctx, []byte(`{"scenario":{"routers":[],"events":[]}}`), "")
	wantKind(t, err, model.KindInput, "INVALID_CONFIG")
}

func TestPayloadTooLarge(t *testing.T) {
	svc := newSvc(t)
	big := make([]byte, replay.MaxPayloadBytes+1)
	for i := range big {
		big[i] = 'x'
	}
	_, _, err := svc.Submit(context.Background(), big, "")
	wantKind(t, err, model.KindResourceExhausted, "PAYLOAD_TOO_LARGE")
}

func TestInvalidRunID(t *testing.T) {
	svc := newSvc(t)
	_, _, err := svc.Submit(context.Background(), envelope(t, "bad/id!"), "")
	wantKind(t, err, model.KindInput, "INVALID_RUN_ID")
}

func TestDuplicateRunIDConflict(t *testing.T) {
	svc := newSvc(t)
	ctx := context.Background()
	if _, _, err := svc.Submit(ctx, envelope(t, "dup"), ""); err != nil {
		t.Fatalf("first: %v", err)
	}
	_, _, err := svc.Submit(ctx, envelope(t, "dup"), "")
	wantKind(t, err, model.KindStateConflict, "RUN_ID_EXISTS")
}

func TestGetMissingIsNotFound(t *testing.T) {
	svc := newSvc(t)
	_, _, err := svc.Get(context.Background(), "ghost")
	wantKind(t, err, model.KindNotFound, "RUN_NOT_FOUND")
}

func TestReplayCreatesNewRun(t *testing.T) {
	svc := newSvc(t)
	ctx := context.Background()
	first, _, err := svc.Submit(ctx, envelope(t, "orig"), "")
	if err != nil {
		t.Fatalf("first: %v", err)
	}
	second, _, err := svc.Replay(ctx, first.RunID)
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if second.RunID == first.RunID {
		t.Fatalf("replay reused id %q", second.RunID)
	}
	if !second.Converged {
		t.Fatalf("replay not converged")
	}
	// Original remains retrievable.
	if _, _, err := svc.Get(ctx, first.RunID); err != nil {
		t.Fatalf("original missing after replay: %v", err)
	}
}

func TestReplayMissing(t *testing.T) {
	svc := newSvc(t)
	_, _, err := svc.Replay(context.Background(), "ghost")
	wantKind(t, err, model.KindNotFound, "RUN_NOT_FOUND")
}
