// Package version holds build identity embedded in logs and the HTTP API.
package version

// Values may be overridden at link time:
//
//	go build -ldflags "-X placer/internal/version.Version=v1.0.0"
var (
	Version   = "v0.1.0-dev"
	Commit    = "unknown"
	BuildTime = "unknown"
)

// String returns a single human-readable build identifier.
func String() string {
	return Version + " commit=" + Commit + " built=" + BuildTime
}
