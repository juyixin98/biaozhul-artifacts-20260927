package adapter

import "infraplanner/internal/errorsx"

func errLost() error {
	return errorsx.UnknownCommit("COMMIT_AMBIGUOUS",
		"provider response lost; create or delete may have committed",
		map[string]any{"replay": "Read before retry"})
}
func errTransient() error {
	return errorsx.Transient("PROVIDER_TRANSIENT", "transient provider fault", nil)
}
func errQuoted() error {
	return errorsx.Exhausted("QUOTA_EXCEEDED", "environment capacity exhausted", nil)
}
func errNotFound() error {
	return errorsx.State("RESOURCE_NOT_FOUND", "resource does not exist", nil)
}
func errState(st string) error {
	return errorsx.State("BAD_RESOURCE_STATE", "resource not in a writable state",
		map[string]any{"state": st})
}

// itoa is a tiny allocation-free-ish int formatter avoiding strconv import in
// hot lock; strconv is fine, keep package self-contained though.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}
