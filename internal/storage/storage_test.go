package storage

import (
	"path/filepath"
	"testing"
	"time"

	"tcpreplay/internal/reassembly"
)

func TestSaveAndLoadRoundtrip(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "test.db")
	s, err := Open("file:" + dbPath + "?_pragma=busy_timeout(2000)")
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	t.Cleanup(func() { s.Close() })

	views := []reassembly.GenerationView{{
		Flow: "10.0.0.1:40001<->10.0.0.2:80", Generation: 0, Closed: true,
		AtoB: reassembly.DirectionView{
			Direction: "a_to_b", HandshakeKnown: true, Stream: []byte("HELLO"),
			Gaps: []reassembly.Gap{{Start: 5, End: 7}}, FINSeen: true, FINPos: 9, LengthProved: 9,
		},
	}}
	events := []reassembly.Event{{
		Seq: 1, RequestID: "r1", Code: reassembly.EvSegmentAccepted, Level: reassembly.LevelInfo,
		Flow: views[0].Flow, RawSeq: 0xfffffff0, AbsStart: 1, AbsEnd: 6, FINPos: -1,
	}}
	conflicts := []reassembly.Conflict{{
		ID: "conf-000001", RequestID: "r1", RecordID: "evil", Flow: views[0].Flow,
		Direction: "a_to_b", ByteOffset: 5, RawSeq: 0x10, Accepted: 'A', Offered: 'Z',
		AcceptedBy: "good", Policy: reassembly.PolicyFirstWins, Disposition: reassembly.DispRejected,
	}}
	rec := RequestRecord{
		ID: "r1", Source: "unit", Policy: "first_wins", Preview: false,
		PacketCount: 1, CreatedAt: time.Date(2026, 9, 28, 10, 0, 0, 0, time.UTC),
	}
	metas := []PacketMeta{{Index: 0, RecordID: "rec-00001", Flow: views[0].Flow,
		Direction: "a_to_b", RawSeq: 0xfffffff0, PayloadLen: 5, Flags: "ACK,PSH"}}

	if err := s.SaveAnalysis(rec, metas, events, conflicts, views); err != nil {
		t.Fatalf("save: %v", err)
	}
	// Same request id must be rejectable by the service via Exists.
	if ok, err := s.Exists("r1"); err != nil || !ok {
		t.Fatalf("Exists: ok=%v err=%v", ok, err)
	}

	h, err := s.GetRequest("r1")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if h.Policy != "first_wins" || h.PacketCount != 1 || h.Preview {
		t.Errorf("header mismatch: %+v", h)
	}

	gotViews, err := s.ListViews("r1")
	if err != nil || len(gotViews) != 1 {
		t.Fatalf("views: %v %v", gotViews, err)
	}
	if string(gotViews[0].AtoB.Stream) != "HELLO" ||
		len(gotViews[0].AtoB.Gaps) != 1 || gotViews[0].AtoB.Gaps[0].End != 7 ||
		gotViews[0].AtoB.FINPos != 9 {
		t.Errorf("view mismatch: %+v", gotViews[0].AtoB)
	}

	gotEvents, err := s.ListEvents("r1", EventFilter{Code: "SEGMENT_ACCEPTED"})
	if err != nil || len(gotEvents) != 1 {
		t.Fatalf("events filter: %v %v", gotEvents, err)
	}
	if gotEvents[0].RawSeq != 0xfffffff0 {
		t.Errorf("raw seq roundtrip: %08x", gotEvents[0].RawSeq)
	}

	gotConfs, err := s.ListConflicts("r1", "", "", -1)
	if err != nil || len(gotConfs) != 1 {
		t.Fatalf("conflicts: %v %v", gotConfs, err)
	}
	c := gotConfs[0]
	if c.Accepted != 'A' || c.Offered != 'Z' || c.ByteOffset != 5 ||
		c.Disposition != reassembly.DispRejected {
		t.Errorf("conflict roundtrip mismatch: %+v", c)
	}

	gotPackets, err := s.ListPackets("r1")
	if err != nil || len(gotPackets) != 1 || gotPackets[0].PayloadLen != 5 {
		t.Fatalf("packets: %v %v", gotPackets, err)
	}

	if _, err := s.GetRequest("missing"); err != ErrNotFound {
		t.Errorf("want ErrNotFound, got %v", err)
	}
}

func TestReopenPersistsAcrossHandles(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "persist.db")
	s1, err := Open("file:" + dbPath + "?_pragma=busy_timeout(2000)")
	if err != nil {
		t.Fatal(err)
	}
	rec := RequestRecord{ID: "r9", Source: "x", Policy: "quarantine", PacketCount: 0}
	if err := s1.SaveAnalysis(rec, nil, nil, nil, nil); err != nil {
		t.Fatal(err)
	}
	if err := s1.Close(); err != nil {
		t.Fatal(err)
	}
	s2, err := Open("file:" + dbPath + "?_pragma=busy_timeout(2000)")
	if err != nil {
		t.Fatal(err)
	}
	defer s2.Close()
	if ok, _ := s2.Exists("r9"); !ok {
		t.Fatal("row did not survive close/reopen")
	}
}
