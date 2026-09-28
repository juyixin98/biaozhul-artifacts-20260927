// Command netsemd is the runnable HTTP service for offline first-match
// network-rule analysis.
//
// Endpoints (all local, JSON only):
//
//	POST /configs            submit a ruleset JSON; stores version + report
//	GET  /reports/latest     latest analysis report (diagnostics, witnesses)
//	GET  /reports/{version}  report for a version
//	POST /evaluate           evaluate one packet against the latest config
//	GET  /requests/{id}      correlated, explainable log of a request
//	GET  /healthz
package main

import (
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"time"

	"netsem/internal/httpapi"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "listen address")
	dbPath := flag.String("db", "netsem.db", "SQLite database path")
	instance := flag.String("instance", hostnameOr("local-dev"), "server instance id recorded in logs")
	flag.Parse()

	app, cleanup, err := httpapi.New(*dbPath, *instance)
	if err != nil {
		log.Fatalf("startup: %v", err)
	}
	defer cleanup()

	log.Printf("netsemd listening on %s (instance=%s, db=%s)", *addr, *instance, *dbPath)
	httpSrv := &http.Server{Addr: *addr, Handler: app.Handler(), ReadHeaderTimeout: 5 * time.Second}
	if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func hostnameOr(fallback string) string {
	if h, err := os.Hostname(); err == nil && h != "" {
		return h
	}
	return fallback
}
