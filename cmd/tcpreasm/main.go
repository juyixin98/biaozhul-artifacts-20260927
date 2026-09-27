// Command tcpreasm runs the offline TCP bidirectional stream reassembly
// service. Two modes:
//
//	tcpreasm serve  -config configs/example.json
//	tcpreasm replay -config configs/example.json testdata/<capture>.jsonl
//
// "replay" ingests one JSONL capture synchronously, prints per-packet
// verdicts and writes every gap/conflict/diagnostic to the configured
// SQLite database (or the -db override).
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"tcpreasm/internal/config"
	"tcpreasm/internal/diag"
	"tcpreasm/internal/httpapi"
	"tcpreasm/internal/reassembly"
	"tcpreasm/internal/store"
	"tcpreasm/internal/tcpmodel"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	switch os.Args[1] {
	case "serve":
		runServe(os.Args[2:])
	case "replay":
		runReplay(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand %q\n", os.Args[1])
		usage()
		os.Exit(2)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `tcpreasm - offline TCP bidirectional stream reassembly

usage:
  tcpreasm serve  [-config path] [-addr host:port] [-db path]
  tcpreasm replay [-config path] [-db path] [-policy first-wins|last-wins|quarantine]
                  [-request-id id] capture.jsonl [capture2.jsonl ...]
`)
}

type commonFlags struct {
	configPath string
	dbOverride string
	policy     string
	requestID  string
	addr       string
}

func parseFlags(fs *flag.FlagSet) *commonFlags {
	c := &commonFlags{}
	fs.StringVar(&c.configPath, "config", "", "path to JSON config (defaults are used when empty)")
	fs.StringVar(&c.dbOverride, "db", "", "override storage.dsn")
	fs.StringVar(&c.policy, "policy", "", "override reassembly.overlap_policy")
	fs.StringVar(&c.requestID, "request-id", "", "request id stamped on diagnostics (replay)")
	return c
}

func loadConfig(c *commonFlags) (config.Config, error) {
	cfg, err := config.Load(c.configPath)
	if err != nil {
		return cfg, err
	}
	if c.dbOverride != "" {
		cfg.Storage.DSN = c.dbOverride
	}
	if c.policy != "" {
		cfg.Reassembly.OverlapPolicy = config.OverlapPolicy(c.policy)
	}
	return cfg, cfg.Validate()
}

func ensureDBDir(dsn string) error {
	if dsn == ":memory:" {
		return nil
	}
	dir := filepath.Dir(dsn)
	if dir == "" || dir == "." {
		return nil
	}
	return os.MkdirAll(dir, 0o755)
}

func openLogWriter(cfg config.Config) (*os.File, error) {
	if cfg.Diagnostics.LogPath == "" {
		return nil, nil
	}
	return os.OpenFile(cfg.Diagnostics.LogPath,
		os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
}

func runServe(args []string) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	c := parseFlags(fs)
	fs.StringVar(&c.addr, "addr", "", "override http.addr")
	_ = fs.Parse(args)

	cfg, err := loadConfig(c)
	fatal(err)
	if c.addr != "" {
		cfg.HTTP.Addr = c.addr
	}
	fatal(ensureDBDir(cfg.Storage.DSN))

	ctx := context.Background()
	st, err := store.Open(ctx, cfg.Storage.DSN)
	fatal(err)
	defer st.Close()

	logFile, err := openLogWriter(cfg)
	fatal(err)
	if logFile != nil {
		defer logFile.Close()
	}
	sink := diag.NewSink(logWriterOr(cfg, logFile, os.Stderr), st, cfg.Diagnostics.MaskIPs)

	eng := reassembly.NewEngine(reassembly.NewOptions(cfg, st, sink))
	srv := &httpapi.Server{Engine: eng, Store: st, Cfg: cfg, Logger: log.Default()}

	httpSrv := &http.Server{
		Addr:              cfg.HTTP.Addr,
		Handler:           srv.NewRouter(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       cfg.HTTP.ReadTimeout(),
		WriteTimeout:      cfg.HTTP.WriteTimeout(),
	}
	log.Printf("tcpreasm listening on %s (db=%s, policy=%s)",
		cfg.HTTP.Addr, cfg.Storage.DSN, cfg.Reassembly.OverlapPolicy)
	if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fatal(err)
	}
}

func logWriterOr(_ config.Config, f *os.File, fallback io.Writer) io.Writer {
	if f != nil {
		return f
	}
	return fallback
}

type replaySummary struct {
	RequestID    string             `json:"request_id"`
	Files        []string           `json:"files"`
	Accepted     int                `json:"accepted"`
	Rejected     int                `json:"rejected"`
	Undecidable  int                `json:"undecidable"`
	TotalPackets int                `json:"total_packets"`
	Connections  []connectionReport `json:"connections"`
}

type connectionReport struct {
	FlowKey     string      `json:"flow_key"`
	Generations []genReport `json:"generations"`
}

type genReport struct {
	Index     int                 `json:"gen_index"`
	Inferred  bool                `json:"inferred"`
	State     string              `json:"state"`
	C2S       directionReport     `json:"c2s"`
	S2C       directionReport     `json:"s2c"`
	Gaps      []store.GapRow      `json:"gaps"`
	Conflicts []store.ConflictOut `json:"conflicts"`
}

type directionReport struct {
	State          string  `json:"state"`
	DeliveredBytes uint64  `json:"delivered_bytes"`
	ISN            *uint32 `json:"isn,omitempty"`
	OpenGaps       int     `json:"open_gaps"`
}

func runReplay(args []string) {
	fs := flag.NewFlagSet("replay", flag.ExitOnError)
	c := parseFlags(fs)
	quiet := fs.Bool("quiet", false, "suppress per-packet verdict lines on stderr")
	_ = fs.Parse(args)
	files := fs.Args()
	if len(files) == 0 {
		usage()
		os.Exit(2)
	}
	cfg, err := loadConfig(c)
	fatal(err)
	fatal(ensureDBDir(cfg.Storage.DSN))

	ctx := context.Background()
	st, err := store.Open(ctx, cfg.Storage.DSN)
	fatal(err)
	defer st.Close()

	logFile, err := openLogWriter(cfg)
	fatal(err)
	if logFile != nil {
		defer logFile.Close()
	}
	var sink *diag.Sink
	if *quiet {
		sink = diag.NewSink(io.Discard, st, cfg.Diagnostics.MaskIPs)
	} else {
		sink = diag.NewSink(logWriterOr(cfg, logFile, os.Stderr), st, cfg.Diagnostics.MaskIPs)
	}

	eng := reassembly.NewEngine(reassembly.NewOptions(cfg, st, sink))
	reqID := c.requestID
	if reqID == "" {
		reqID = fmt.Sprintf("replay-%d", time.Now().UnixNano())
	}

	summary := replaySummary{RequestID: reqID, Files: files}
	var order int64
	for _, path := range files {
		raw, err := os.ReadFile(path)
		fatal(err)
		pkts, err := tcpmodel.ParseCapture(raw)
		fatal(err)
		for i := range pkts {
			if pkts[i].Order == 0 {
				pkts[i].Order = order + int64(i) + 1
			} else {
				pkts[i].Order = order + pkts[i].Order
			}
			if pkts[i].RecordID == "" {
				pkts[i].RecordID = fmt.Sprintf("%s#%d", filepath.Base(path), i+1)
			}
		}
		order += int64(len(pkts))
		for _, p := range pkts {
			summary.TotalPackets++
			res, err := eng.Process(ctx, p, reqID)
			fatal(err)
			switch res.Decision {
			case diag.Accepted:
				summary.Accepted++
			case diag.Rejected:
				summary.Rejected++
			case diag.Undecidable:
				summary.Undecidable++
			}
			if !*quiet {
				fmt.Fprintf(os.Stderr, "%-11s %-45s pkt=%s flow=%s gen=%d dir=%s %s\n",
					res.Decision, res.Category, p.RecordID, res.FlowKey,
					res.GenIndex, res.Direction, res.Reason)
			}
		}
	}

	conns, err := st.ListConnections(ctx)
	fatal(err)
	for _, co := range conns {
		cr := connectionReport{FlowKey: co.FlowKey}
		for _, g := range co.Gens {
			gr := genReport{Index: g.GenIndex, Inferred: g.Inferred, State: stateSummary(g.C2SState, g.S2CState)}
			gr.C2S = directionReport{State: g.C2SState, DeliveredBytes: g.C2SDelivered, ISN: g.C2SISN}
			gr.S2C = directionReport{State: g.S2CState, DeliveredBytes: g.S2CDelivered, ISN: g.S2CISN}
			gaps, err := st.ListGaps(ctx, co.FlowKey, g.GenIndex, "", "")
			fatal(err)
			for _, gap := range gaps {
				if gap.Status == "open" {
					if gap.Direction == "c2s" {
						gr.C2S.OpenGaps++
					} else {
						gr.S2C.OpenGaps++
					}
				}
			}
			gr.Gaps = gaps
			if gr.Gaps == nil {
				gr.Gaps = []store.GapRow{}
			}
			gr.Conflicts, err = st.ListConflicts(ctx, co.FlowKey, g.GenIndex, "")
			fatal(err)
			if gr.Conflicts == nil {
				gr.Conflicts = []store.ConflictOut{}
			}
			cr.Generations = append(cr.Generations, gr)
		}
		summary.Connections = append(summary.Connections, cr)
	}

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	fatal(enc.Encode(summary))
}

func stateSummary(a, b string) string {
	if a == b {
		return a
	}
	return a + "/" + b
}

func fatal(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "tcpreasm:", err)
		os.Exit(1)
	}
}
