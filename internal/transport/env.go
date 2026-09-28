package transport

import "os"

// jsonLogEnabled turns on one-JSON-line-per-decision logging for the
// verification scripts.
func jsonLogEnabled() bool {
	v := os.Getenv("DHCPV4LAB_JSON_LOG")
	return v == "1" || v == "true"
}
