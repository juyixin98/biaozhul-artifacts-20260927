package adapter

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func TestFileAdapter_WriteAndRead(t *testing.T) {
	dir := t.TempDir()
	f := &FileAdapter{RootDir: dir}
	out := f.Apply(context.Background(), DesiredState{
		Kind: "widget", Name: "w1", Revision: 7,
		Live: json.RawMessage(`{"a":1}`),
	})
	if !out.Synced {
		t.Fatalf("apply not synced: %+v", out)
	}
	body, err := f.ReadBack("widget", "w1")
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatal(err)
	}
	if got["revision"].(float64) != 7 {
		t.Fatalf("revision wrong: %v", got["revision"])
	}
	if live := got["live"].(map[string]any); live["a"].(float64) != 1 {
		t.Fatalf("live wrong: %v", got["live"])
	}
}

func TestFileAdapter_AtomicRenameLeavesNoTmp(t *testing.T) {
	dir := t.TempDir()
	f := &FileAdapter{RootDir: dir}
	for i := 0; i < 10; i++ {
		if o := f.Apply(context.Background(), DesiredState{
			Kind: "k", Name: "n", Revision: int64(i), Live: json.RawMessage(`{}`),
		}); !o.Synced {
			t.Fatal(o)
		}
	}
	entries, _ := os.ReadDir(filepath.Join(dir, "k"))
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".tmp-") {
			t.Fatalf("stale temp file left behind: %s", e.Name())
		}
	}
}

func TestFaultAdapter_FailsThenRecovers(t *testing.T) {
	dir := t.TempDir()
	inner := &FileAdapter{RootDir: dir}
	a := &FaultAdapter{
		Inner: inner,
		Cfg: FaultConfig{
			FailN:  map[string]int{"widget/x": 2},
			Reason: "boom",
		},
	}
	d := DesiredState{Kind: "widget", Name: "x", Revision: 1, Live: json.RawMessage(`{}`)}
	o1 := a.Apply(context.Background(), d)
	o2 := a.Apply(context.Background(), d)
	if o1.Synced || o2.Synced || !o1.Retryable || !o2.Retryable {
		t.Fatalf("first two calls must be retryable failures: %+v %+v", o1, o2)
	}
	o3 := a.Apply(context.Background(), d)
	if !o3.Synced {
		t.Fatalf("third call must recover: %+v", o3)
	}
	if len(a.Calls()) != 3 {
		t.Fatalf("calls recorded = %d, want 3", len(a.Calls()))
	}
}

func TestFaultAdapter_PermanentIsNotRetryable(t *testing.T) {
	a := &FaultAdapter{
		Inner: UnavailableAdapter{},
		Cfg:   FaultConfig{FailN: map[string]int{"k/n": 1}, Permanent: true},
	}
	o := a.Apply(context.Background(), DesiredState{Kind: "k", Name: "n"})
	if o.Synced || o.Retryable {
		t.Fatalf("permanent fault must be non-retryable: %+v", o)
	}
}

func TestFileAdapter_ConcurrentWrites(t *testing.T) {
	dir := t.TempDir()
	f := &FileAdapter{RootDir: dir}
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		i := i
		go func() {
			defer wg.Done()
			live, _ := json.Marshal(map[string]int{"i": i})
			o := f.Apply(context.Background(), DesiredState{
				Kind: "k", Name: "n", Revision: int64(i), Live: live,
			})
			if !o.Synced {
				t.Errorf("write %d failed: %+v", i, o)
			}
		}()
	}
	wg.Wait()
}
