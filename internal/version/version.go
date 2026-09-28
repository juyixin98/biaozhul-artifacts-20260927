// Package version holds build identification for the dhcp4d server and its
// tests. Logs, the HTTP diagnostics endpoint and test reports all use these
// values so that a captured run can be tied back to an exact build.
package version

const (
	// Server is the protocol-implementation version reported in logs and
	// via the HTTP diagnostics API. It follows semantic versioning.
	Server = "1.0.0"

	// Protocol is the subset identifier ("dhcp4-subset") plus the RFC
	// document the message subset is derived from (RFC 2131).
	Protocol = "dhcp4-subset/rfc2131"
)
