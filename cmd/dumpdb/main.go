// Command dumpdb is a read-only audit helper: it prints the persisted
// computation records from a cidrsvc SQLite database as JSON, one object per
// line, so replay evidence can be reviewed without the HTTP server running.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"cidrsvc/internal/store"
)

func main() {
	dbPath := flag.String("db", "data/cidrsvc.db", "SQLite database path")
	limit := flag.Int("limit", 100, "maximum records to print")
	family := flag.String("family", "", "optional family filter")
	status := flag.String("status", "", "optional status filter (ok|error)")
	flag.Parse()

	st, err := store.Open(context.Background(), *dbPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open %s: %v\n", *dbPath, err)
		os.Exit(1)
	}
	defer st.Close()

	recs, err := st.List(context.Background(), *limit, 0, *family, *status)
	if err != nil {
		fmt.Fprintf(os.Stderr, "list: %v\n", err)
		os.Exit(1)
	}
	counts, _ := st.CountByStatus(context.Background())
	fmt.Fprintf(os.Stderr, "records=%d status_counts=%v\n", len(recs), counts)

	enc := json.NewEncoder(os.Stdout)
	for i := range recs {
		r := recs[i]
		out := map[string]any{
			"request_id": r.RequestID, "family": r.Family, "width": r.Width,
			"status": r.Status, "allow": json.RawMessage(orArr(r.AllowJSON)),
			"exclude":    json.RawMessage(orArr(r.ExclJSON)),
			"prefixes":   json.RawMessage(orArr(r.ResultJSON)),
			"client_ref": r.ClientRef, "created_at": r.CreatedAt.Format("2006-01-02T15:04:05.999999999Z07:00"),
		}
		if r.Status == "error" {
			out["error_code"] = r.ErrorCode
			out["error"] = r.ErrorText
		}
		if err := enc.Encode(out); err != nil {
			fmt.Fprintf(os.Stderr, "encode: %v\n", err)
			os.Exit(1)
		}
	}
}

func orArr(s string) string {
	if s == "" {
		return "[]"
	}
	return s
}
