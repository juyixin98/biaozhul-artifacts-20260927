package integration_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"natlab/internal/config"
	"natlab/internal/model"
	"natlab/internal/replay"
	"natlab/internal/storage"
)

// TestSQLitePersistenceAcrossReopen replays a prefix of a trace into a file
// database, closes it, reopens, restores the engine, and continues. The
// mapping created before the reopen must still translate correctly afterwards.
func TestSQLitePersistenceAcrossReopen(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "reopen.db")
	cfg := config.Default()
	cfg.SQLitePath = dbPath
	ctx := context.Background()

	// Prefix trace: open TCP + complete handshake, all before the reopen.
	prefix := &replay.Trace{
		RunID: "r-restore-01", Name: "restore prefix", StartAt: time.Now().UTC(),
		Packets: []model.Packet{
			model.NewPacket(1, time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), model.Outbound,
				"10.0.0.2", 40001, "203.0.113.10", 80, model.TCP, model.TCPFlagBits{SYN: true}),
			model.NewPacket(2, time.Date(2026, 1, 1, 0, 0, 1, 0, time.UTC), model.Inbound,
				"203.0.113.10", 80, "198.51.100.1", 20000, model.TCP,
				model.TCPFlagBits{SYN: true, ACK: true}),
		},
	}
	{
		st, err := storage.OpenSQLite(dbPath)
		if err != nil {
			t.Fatal(err)
		}
		rep, err := replay.NewRunner(cfg, st).Run(ctx, prefix)
		if err != nil {
			t.Fatalf("prefix run: %v", err)
		}
		if got := rep.Stats.MappingsCreated; got != 1 {
			t.Fatalf("created=%d", got)
		}
		if err := st.Close(); err != nil {
			t.Fatal(err)
		}
	}

	// Reopen and continue: the established mapping must still be there and the
	// watermark must not have rolled back.
	{
		st, err := storage.OpenSQLite(dbPath)
		if err != nil {
			t.Fatal(err)
		}
		defer st.Close()
		info, err := st.GetRun(ctx, "r-restore-01")
		if err != nil {
			t.Fatal(err)
		}
		if info.Watermark.IsZero() {
			t.Fatal("watermark was not persisted")
		}
		rep, err := replay.NewRunner(cfg, st).Run(ctx, &replay.Trace{
			RunID: "r-restore-01", Name: "after reopen",
			Packets: []model.Packet{
				model.NewPacket(3, time.Date(2026, 1, 1, 0, 0, 2, 0, time.UTC), model.Outbound,
					"10.0.0.2", 40001, "203.0.113.10", 80, model.TCP,
					model.TCPFlagBits{ACK: true}),
				model.NewPacket(4, time.Date(2026, 1, 1, 0, 0, 3, 0, time.UTC), model.Inbound,
					"203.0.113.10", 80, "198.51.100.1", 20000, model.TCP,
					model.TCPFlagBits{ACK: true}),
			},
		})
		if err != nil {
			t.Fatalf("resumed run: %v", err)
		}
		bySeq := decisionsBySeq(rep)
		assertRow(t, bySeq[3], expect{verdict: model.AcceptForward, postSrc: 20000,
			state: "ESTABLISHED"})
		assertRow(t, bySeq[4], expect{verdict: model.AcceptForward, postDst: 40001,
			state: "ESTABLISHED"})
		if rep.Stats.ActiveMappings != 1 {
			t.Fatalf("active after restore=%d, want 1", rep.Stats.ActiveMappings)
		}
	}
}

// TestExpiredMappingReapedOnRestore persists a mapping whose deadline has
// passed, reopens, and asserts Restore reaps it instead of accepting traffic.
func TestExpiredMappingReapedOnRestore(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "restore-expired.db")
	cfg := config.Default()
	cfg.UDPTimeout = config.Duration{Duration: time.Second}
	ctx := context.Background()

	{
		st, err := storage.OpenSQLite(dbPath)
		if err != nil {
			t.Fatal(err)
		}
		rep, err := replay.NewRunner(cfg, st).Run(ctx, &replay.Trace{
			RunID: "r-restore-exp",
			Packets: []model.Packet{
				model.NewPacket(1, time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), model.Outbound,
					"10.0.0.2", 50001, "198.51.100.53", 53, model.UDP, model.TCPFlagBits{}),
			},
		})
		if err != nil || rep.Stats.MappingsCreated != 1 {
			t.Fatalf("prefix err=%v rep=%+v", err, rep.Stats)
		}
		st.Close()
	}

	{
		st, err := storage.OpenSQLite(dbPath)
		if err != nil {
			t.Fatal(err)
		}
		defer st.Close()
		// Continue far after expiry: Restore must reap at the stored watermark
		// boundary and the packet must not be translated.
		rep, err := replay.NewRunner(cfg, st).Run(ctx, &replay.Trace{
			RunID: "r-restore-exp",
			Packets: []model.Packet{
				model.NewPacket(2, time.Date(2026, 1, 1, 0, 1, 0, 0, time.UTC), model.Inbound,
					"198.51.100.53", 53, "198.51.100.1", 30000, model.UDP, model.TCPFlagBits{}),
			},
		})
		if err != nil {
			t.Fatal(err)
		}
		d := decisionsBySeq(rep)[2]
		if d.Verdict != model.Reject || d.Reason != model.ReasonMappingExpired {
			t.Fatalf("after restore verdict=%s reason=%s", d.Verdict, d.Reason)
		}
	}
}

// TestHTTPServer exercises the replay interface over httptest, never touching
// a real network socket beyond the in-process server.
func TestHTTPServer(t *testing.T) {
	st := storage.NewMemory()
	srv := httptest.NewServer(replay.NewServer(config.Default(), st).Handler())
	defer srv.Close()

	// health
	if r, err := http.Get(srv.URL + "/healthz"); err != nil || r.StatusCode != 200 {
		t.Fatalf("healthz status=%d err=%v", r.StatusCode, err)
	}

	// create run
	body := `{"run_id":"r-http-01","name":"http smoke"}`
	resp, err := http.Post(srv.URL+"/v1/runs", "application/json", strings.NewReader(body))
	if err != nil || resp.StatusCode != http.StatusCreated {
		t.Fatalf("create run status=%d err=%v", resp.StatusCode, err)
	}
	resp.Body.Close()

	send := func(p model.Packet) map[string]any {
		b, _ := json.Marshal(p)
		r, err := http.Post(srv.URL+"/v1/runs/r-http-01/packets", "application/json",
			strings.NewReader(string(b)))
		if err != nil {
			t.Fatal(err)
		}
		defer r.Body.Close()
		var env struct {
			OK    bool `json:"ok"`
			Error *struct {
				Class  model.Class `json:"class"`
				Reason string      `json:"reason"`
			} `json:"error"`
			Data struct {
				Decision model.Decision `json:"decision"`
			} `json:"data"`
		}
		if err := json.NewDecoder(r.Body).Decode(&env); err != nil {
			t.Fatal(err)
		}
		return map[string]any{
			"status":   r.StatusCode,
			"ok":       env.OK,
			"err":      env.Error,
			"decision": env.Data.Decision,
		}
	}

	ts := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	syn := model.NewPacket(1, ts, model.Outbound, "10.0.0.2", 40001,
		"203.0.113.10", 80, model.TCP, model.TCPFlagBits{SYN: true})
	r1 := send(syn)
	d1 := r1["decision"].(model.Decision)
	if d1.Verdict != model.AcceptTranslate || d1.Post.SrcPort != 20000 {
		t.Fatalf("http syn: %+v", d1)
	}

	// Malformed JSON => 400 input class.
	rBad, err := http.Post(srv.URL+"/v1/runs/r-http-01/packets", "application/json",
		strings.NewReader("{not json"))
	if err != nil || rBad.StatusCode != http.StatusBadRequest {
		t.Fatalf("malformed status=%d err=%v", rBad.StatusCode, err)
	}
	rBad.Body.Close()

	// Decisions log is retrievable.
	rl, err := http.Get(srv.URL + "/v1/runs/r-http-01/decisions")
	if err != nil || rl.StatusCode != 200 {
		t.Fatalf("decisions status=%d", rl.StatusCode)
	}
	var logged struct {
		Data struct {
			Decisions []model.Decision `json:"decisions"`
		} `json:"data"`
	}
	if err := json.NewDecoder(rl.Body).Decode(&logged); err != nil {
		t.Fatal(err)
	}
	rl.Body.Close()
	if len(logged.Data.Decisions) != 1 || logged.Data.Decisions[0].RunID != "r-http-01" {
		t.Fatalf("decision log=%+v", logged.Data.Decisions)
	}

	// 404 for unknown run sub-resource.
	r404, err := http.Get(srv.URL + "/v1/runs/unknown/stats")
	if err != nil || r404.StatusCode != http.StatusNotFound {
		t.Fatalf("unknown run status=%d", r404.StatusCode)
	}
	r404.Body.Close()
}
