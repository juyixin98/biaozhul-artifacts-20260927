// Package diag provides cross-cutting diagnostics: structured logging,
// request correlation IDs and redaction of sensitive label values.
package diag

import (
	"io"
	"log/slog"
	"os"
	"strings"
)

// NewLogger builds a JSON structured logger at the given level ("debug",
// "info", "warn", "error").
func NewLogger(level string, w io.Writer) *slog.Logger {
	if w == nil {
		w = os.Stdout
	}
	var lvl slog.Level
	switch strings.ToLower(strings.TrimSpace(level)) {
	case "debug":
		lvl = slog.LevelDebug
	case "warn", "warning":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	default:
		lvl = slog.LevelInfo
	}
	return slog.New(slog.NewJSONHandler(w, &slog.HandlerOptions{Level: lvl}))
}
