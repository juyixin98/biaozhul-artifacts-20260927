package diag

import "strings"

// sensitiveSubstrings mark label KEYS whose VALUES must never be logged in
// clear text. Matching is case-insensitive substring matching so that e.g.
// "api-token", "db_password" and "secretKey" are all caught.
var sensitiveSubstrings = []string{
	"secret", "password", "passwd", "token", "credential",
	"authorization", "auth", "private", "apikey", "api-key",
}

// Redacted is the fixed placeholder used for masked values.
const Redacted = "***REDACTED***"

// IsSensitiveKey reports whether a label key carries sensitive data.
func IsSensitiveKey(key string) bool {
	k := strings.ToLower(key)
	for _, s := range sensitiveSubstrings {
		if strings.Contains(k, s) {
			return true
		}
	}
	return false
}

// RedactLabels returns a copy of labels with sensitive values masked. The
// returned map is safe to log even if the input came straight off a request.
// Keys and the set of keys are preserved (they describe the policy model);
// only values are masked.
func RedactLabels(labels map[string]string) map[string]string {
	if labels == nil {
		return nil
	}
	out := make(map[string]string, len(labels))
	for k, v := range labels {
		if IsSensitiveKey(k) {
			out[k] = Redacted
		} else {
			out[k] = v
		}
	}
	return out
}
