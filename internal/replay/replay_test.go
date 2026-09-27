package replay

import (
	"strings"
	"testing"
	"time"

	"natlab/internal/model"
)

func TestParseJSONObjectForm(t *testing.T) {
	body := `{
      "run_id":"r-json","name":"obj",
      "packets":[
        {"seq":1,"observed_at":"2026-01-01T00:00:00Z","direction":"outbound",
         "tuple":{"src_ip":"10.0.0.2","src_port":40001,"dst_ip":"203.0.113.10","dst_port":80,"proto":"TCP"},
         "tcp_flags":{"syn":true}}
      ]}`
	tr, err := ParseTrace([]byte(body), "inline")
	if err != nil {
		t.Fatal(err)
	}
	if tr.RunID != "r-json" || len(tr.Packets) != 1 || tr.Packets[0].Seq != 1 {
		t.Fatalf("parsed %+v", tr)
	}
}

func TestParseTraceJSONLForm(t *testing.T) {
	// Header line followed by packet lines; packets omit seq and must be
	// auto-numbered from 1.
	body := strings.Join([]string{
		`{"run_id":"r-jsonl","name":"line"}`,
		`{"observed_at":"2026-01-01T00:00:00Z","direction":"outbound",
		  "tuple":{"src_ip":"10.0.0.2","src_port":40001,"dst_ip":"203.0.113.10","dst_port":80,"proto":"UDP"}}`,
		`{"observed_at":"2026-01-01T00:00:01Z","direction":"inbound",
		  "tuple":{"src_ip":"203.0.113.10","src_port":80,"dst_ip":"198.51.100.1","dst_port":30000,"proto":"UDP"}}`,
	}, "\n")
	tr, err := ParseTrace([]byte(body), "inline.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	if tr.RunID != "r-jsonl" || len(tr.Packets) != 2 {
		t.Fatalf("parsed %+v", tr)
	}
	if tr.Packets[0].Seq != 1 || tr.Packets[1].Seq != 2 {
		t.Fatalf("auto seq = %d,%d", tr.Packets[0].Seq, tr.Packets[1].Seq)
	}
}

func TestTraceCheckRejects(t *testing.T) {
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	missing := &Trace{}
	if err := missing.Check(); err == nil {
		t.Fatal("missing run_id must fail")
	}
	dup := &Trace{RunID: "x"}
	dup.Packets = []model.Packet{
		model.NewPacket(5, base, model.Outbound, "10.0.0.2", 40001, "203.0.113.10", 80, model.UDP, model.TCPFlagBits{}),
		model.NewPacket(5, base.Add(time.Second), model.Outbound, "10.0.0.2", 40001, "203.0.113.10", 80, model.UDP, model.TCPFlagBits{}),
	}
	if err := dup.Check(); err == nil || !strings.Contains(err.Error(), "duplicate seq") {
		t.Fatalf("dup seq err=%v", err)
	}
}
