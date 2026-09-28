package server

import (
	"fmt"
	"io"
	"log"
	"sort"
	"strconv"
	"strings"
	"time"
)

// coordLogger adapts the standard logger to coordinator.Logger. It emits
// single-line key=value records so each key step of a request is greppable by
// request_id, group or partition.
type coordLogger struct {
	l *log.Logger
}

// NewCoordLogger builds a coordinator.Logger writing to w.
func NewCoordLogger(w io.Writer) coordLogger {
	return coordLogger{l: log.New(w, "", 0)}
}

// Log implements coordinator.Logger.
func (c coordLogger) Log(level, msg string, kv ...any) {
	var b strings.Builder
	fmt.Fprintf(&b, "ts=%s level=%s msg=%q", time.Now().UTC().Format(time.RFC3339Nano), level, msg)
	keys := make([]string, 0, len(kv)/2)
	vals := map[string]any{}
	for i := 0; i+1 < len(kv); i += 2 {
		if k, ok := kv[i].(string); ok {
			keys = append(keys, k)
			vals[k] = kv[i+1]
		}
	}
	sort.Strings(keys)
	for _, k := range keys {
		fmt.Fprintf(&b, " %s=%s", k, logValue(vals[k]))
	}
	c.l.Print(b.String())
}

func logValue(v any) string {
	switch x := v.(type) {
	case string:
		return quote(x)
	case fmt.Stringer:
		return quote(x.String())
	default:
		return quote(fmt.Sprint(v))
	}
}

func quote(s string) string {
	if strings.ContainsAny(s, " \t\"=") {
		return strconv.Quote(s)
	}
	return s
}
