// Package version holds build-time identity information. It is embedded into
// every log line, health response and error envelope so that a test log can be
// correlated to the exact binary that produced a decision.
package version

// Values may be overridden at link time:
//
//	go build -ldflags "-X opp284/placement/internal/version.Version=v1.2.3"
var (
	Version = "dev"
	Commit  = "unknown"
	BuiltAt = "unknown"
)

// String returns a compact "version (commit, builtAt)" descriptor.
func String() string {
	return Version + " (" + Commit + ", " + BuiltAt + ")"
}
