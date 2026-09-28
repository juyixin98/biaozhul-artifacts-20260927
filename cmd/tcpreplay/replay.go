package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"tcpreplay/internal/netmodel"
	"tcpreplay/internal/reassembly"
	"tcpreplay/internal/storage"
)

// replayCmd is the one-shot offline pipeline. It deliberately runs the same
// primitives as the service ingest path (fresh Manager, per-record events,
// SaveAnalysis) rather than a parallel implementation.
type replayCmd struct {
	cfgPath   string
	pcapPath  string
	requestID string
	outDir    string
}

func runReplay(args []string) error {
	fs := flag.NewFlagSet("replay", flag.ContinueOnError)
	c := &replayCmd{}
	fs.StringVar(&c.cfgPath, "config", "configs/tcpreplay.json", "path to JSON config")
	fs.StringVar(&c.pcapPath, "pcap", "", "path to classic pcap capture")
	fs.StringVar(&c.requestID, "request-id", "", "explicit request id (default: derived from file name)")
	fs.StringVar(&c.outDir, "out", "", "directory for per-direction raw streams and report.json")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if c.pcapPath == "" {
		return fmt.Errorf("--pcap is required")
	}
	cfg := loadConfigOrExit(c.cfgPath)

	raw, err := os.ReadFile(c.pcapPath)
	if err != nil {
		return fmt.Errorf("read pcap: %w", err)
	}
	packets, err := netmodel.ParsePCap(raw)
	if err != nil {
		return fmt.Errorf("parse pcap: %w", err)
	}
	if c.requestID == "" {
		c.requestID = "req-" + sanitize(filepath.Base(c.pcapPath))
	}
	if c.outDir != "" {
		if err := os.MkdirAll(c.outDir, 0o755); err != nil {
			return fmt.Errorf("create out dir: %w", err)
		}
	}
	if dir := filepath.Dir(cfg.DBPath); dir != "." && dir != ":memory:" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("create db dir: %w", err)
		}
	}
	store, err := storage.Open(sqliteDSN(cfg.DBPath))
	if err != nil {
		return err
	}
	defer store.Close()

	mgr := reassembly.NewManager(reassembly.OverlapPolicy(cfg.OverlapPolicy), cfg.PayloadPreview)
	var events []reassembly.Event
	var metas []storage.PacketMeta
	for i, p := range packets {
		rid := p.RecordID
		if rid == "" {
			rid = fmt.Sprintf("rec-%05d", i+1)
		}
		k, aToB := p.FlowKeyAndDir()
		res := mgr.Process(p, c.requestID, rid)
		events = append(events, res.Events...)
		metas = append(metas, storage.PacketMeta{
			Index: i, RecordID: rid, Flow: k.String(),
			Direction: dirLabel(aToB), RawSeq: p.Seq, PayloadLen: len(p.Payload),
			Flags: strings.Join(p.Flags, ","), Timestamp: p.Timestamp,
		})
	}
	views := mgr.AllViews()
	conflicts := mgr.Conflicts()
	rec := storage.RequestRecord{
		ID: c.requestID, Source: filepath.Base(c.pcapPath), Policy: cfg.OverlapPolicy,
		Preview: cfg.PayloadPreview, PacketCount: len(packets), CreatedAt: time.Now().UTC(),
	}
	if err := store.SaveAnalysis(rec, metas, events, conflicts, views); err != nil {
		return fmt.Errorf("persist: %w", err)
	}

	printSummary(c, len(packets), events, conflicts, views)
	if c.outDir != "" {
		if err := writeArtifacts(store, c, filepath.Base(c.pcapPath), cfg.OverlapPolicy, views); err != nil {
			return err
		}
	}
	return nil
}

func dirLabel(aToB bool) string {
	if aToB {
		return "a_to_b"
	}
	return "b_to_a"
}

func printSummary(c *replayCmd, packetCount int, events []reassembly.Event,
	conflicts []reassembly.Conflict, views []reassembly.GenerationView) {
	fmt.Printf("request_id:   %s\n", c.requestID)
	fmt.Printf("packets:      %d (TCP segments parsed)\n", packetCount)
	byLevel := map[string]int{}
	for _, e := range events {
		byLevel[string(e.Level)]++
	}
	fmt.Printf("events:       info=%d warn=%d reject=%d undecided=%d\n",
		byLevel["info"], byLevel["warn"], byLevel["reject"], byLevel["undecided"])
	fmt.Printf("conflicts:    %d byte-level overlap disagreement(s)\n", len(conflicts))
	for _, v := range views {
		fmt.Printf("generation %d %s: a_to_b contiguous=%d gaps=%d held=%d fin=%t | b_to_a contiguous=%d gaps=%d held=%d fin=%t closed=%t\n",
			v.Generation, v.Flow,
			len(v.AtoB.Stream), len(v.AtoB.Gaps), len(v.AtoB.HeldRuns), v.AtoB.FINSeen,
			len(v.BtoA.Stream), len(v.BtoA.Gaps), len(v.BtoA.HeldRuns), v.BtoA.FINSeen,
			v.Closed)
		for _, g := range v.AtoB.Gaps {
			fmt.Printf("  GAP a_to_b [%d,%d)\n", g.Start, g.End)
		}
		for _, g := range v.BtoA.Gaps {
			fmt.Printf("  GAP b_to_a [%d,%d)\n", g.Start, g.End)
		}
	}
	for _, cf := range conflicts {
		fmt.Printf("  CONFLICT gen=%d %s offset=%d raw_seq=0x%08x accepted=0x%02x(from %s) offered=0x%02x(from %s) disposition=%s\n",
			cf.Generation, cf.Direction, cf.ByteOffset, cf.RawSeq, cf.Accepted,
			cf.AcceptedBy, cf.Offered, cf.RecordID, cf.Disposition)
	}
	if c.outDir != "" {
		fmt.Printf("artifacts:    %s (raw streams + report.json)\n", c.outDir)
	}
}

// streamArtifact is the JSON report written to --out. It is re-read from
// SQLite, so the file is exactly the evidence auditors can query back.
type streamArtifact struct {
	RequestID string                      `json:"request_id"`
	Source    string                      `json:"source"`
	Policy    string                      `json:"policy"`
	Views     []reassembly.GenerationView `json:"generation_views"`
	Events    []reassembly.Event          `json:"events"`
	Conflicts []reassembly.Conflict       `json:"conflicts"`
}

func writeArtifacts(store *storage.Store, c *replayCmd, source, policy string,
	views []reassembly.GenerationView) error {
	events, err := store.ListEvents(c.requestID, storage.EventFilter{})
	if err != nil {
		return err
	}
	conflicts, err := store.ListConflicts(c.requestID, "", "", -1)
	if err != nil {
		return err
	}
	art := streamArtifact{
		RequestID: c.requestID, Source: source, Policy: policy,
		Views: views, Events: events, Conflicts: conflicts,
	}
	buf, err := json.MarshalIndent(art, "", "  ")
	if err != nil {
		return err
	}
	if err := os.WriteFile(filepath.Join(c.outDir, "report.json"), append(buf, '\n'), 0o644); err != nil {
		return err
	}
	for _, v := range views {
		if err := writeStreamFile(c.outDir, v.Flow, v.Generation, "a_to_b", v.AtoB.Stream); err != nil {
			return err
		}
		if err := writeStreamFile(c.outDir, v.Flow, v.Generation, "b_to_a", v.BtoA.Stream); err != nil {
			return err
		}
	}
	return nil
}

func writeStreamFile(dir, flow string, gen int, direction string, data []byte) error {
	name := fmt.Sprintf("stream_%s_g%d_%s.bin", sanitize(flow), gen, direction)
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return err
	}
	fmt.Printf("  wrote %s (%d contiguous, evidenced bytes)\n", name, len(data))
	return nil
}

func sanitize(s string) string {
	r := strings.NewReplacer(":", "_", "<", "", ">", "", "/", "_", " ", "_")
	out := r.Replace(s)
	return strings.Trim(out, "_.")
}
