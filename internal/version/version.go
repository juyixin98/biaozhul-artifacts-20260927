// Package version exposes build/protocol identifiers used in /healthz output
// and every structured log line.
package version

import "runtime"

// Values are intentionally fixed defaults; CI/build can override the Version
// with -ldflags "-X lifecycle.local/v1/internal/version.Version=...".
var (
	Version   = "1.0.0"
	Commit    = "unknown"
	BuildDate = "unknown"
)

// APIVersion is the HTTP API contract version.
const APIVersion = "v1"

// SchemaVersion is the SQLite schema revision the storage layer provisions.
const SchemaVersion = 1

// Info is the JSON payload of GET /healthz.
type Info struct {
	Service     string `json:"service"`
	Version     string `json:"version"`
	Commit      string `json:"commit"`
	BuildDate   string `json:"buildDate"`
	APIVersion  string `json:"apiVersion"`
	Schema      int    `json:"schema"`
	GoVersion   string `json:"goVersion"`
	Determinism string `json:"determinism"`
}

// Get returns the current version info. deterministic is either
// "normal" or "deterministic(seed=<seed>)".
func Get(determinism string) Info {
	return Info{
		Service:     "lifecycle-controller",
		Version:     Version,
		Commit:      Commit,
		BuildDate:   BuildDate,
		APIVersion:  APIVersion,
		Schema:      SchemaVersion,
		GoVersion:   runtime.Version(),
		Determinism: determinism,
	}
}
