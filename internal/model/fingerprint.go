package model

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// SpecFingerprint returns a stable hash of a desired spec. It lets the
// controller compare the external resource against the desired state without
// relying on pointer equality or field-by-field comparisons. Struct field
// order is fixed by the type, so encoding/json output is deterministic.
func SpecFingerprint(spec WidgetSpec) string {
	b, err := json.Marshal(spec)
	if err != nil {
		// WidgetSpec contains only JSON-safe scalar types; marshal cannot fail.
		panic(err)
	}
	sum := sha256.Sum256(b)
	return "sha256:" + hex.EncodeToString(sum[:])
}
