// Package version holds build/proto/schema identity used by logs, the HTTP
// /version endpoint and persisted events, so a failure trace can always be
// tied back to the code and schema revision that produced it.
package version

// ProtocolVersion is the wire/domain protocol revision.
const ProtocolVersion = "1.0.0"

// SchemaVersion is the PostgreSQL migration revision applied at startup.
const SchemaVersion = 1

// Service is the human-readable service name printed at startup.
const Service = "workbroker"
