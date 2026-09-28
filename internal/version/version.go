// Package version exposes build/release metadata. The values may be overridden
// with -ldflags, e.g.:
//
//	go build -ldflags "-X dhcpv4lab/internal/version.Version=v1.0.1" ./cmd/dhcpd
package version

import (
	"fmt"
	"runtime"
)

var (
	// Version is the fixed release tag of this delivery.
	Version = "v1.0.0"
	// Commit is filled in at build time; "dev" denotes an unversioned build.
	Commit = "dev"
)

// Banner returns a single human-readable build descriptor. It is logged at
// startup and exposed over HTTP so test runs can correlate to code revision.
func Banner() string {
	return fmt.Sprintf("dhcpv4lab %s commit=%s go=%s", Version, Commit, runtime.Version())
}
