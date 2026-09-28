// Package diag 提供贯穿一次请求的诊断上下文：请求标识、关键状态记录
// 与脱敏。所有“为什么接受/拒绝/无法判定”的说明都经此输出，保证
// 日志与返回体里的诊断字段一致且不泄漏敏感明细。
package diag

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/netip"
	"os"
	"sync/atomic"
	"time"
)

type ctxKey struct{}

// Context 携带请求标识与顺序记录的诊断条目。
type Context struct {
	RequestID string
	redact    bool
	entries   []Entry
}

// Entry 是一条关键状态记录。
type Entry struct {
	At      time.Time `json:"at"`
	Message string    `json:"message"`
}

// NewContext 创建请求上下文。requestID 为空时生成形如
// req-<8 字节十六进制> 的随机标识。
func NewContext(requestID string, redact bool) *Context {
	if requestID == "" {
		b := make([]byte, 8)
		_, _ = rand.Read(b)
		requestID = "req-" + hex.EncodeToString(b)
	}
	return &Context{RequestID: requestID, redact: redact}
}

// IntoContext / FromContext 在 context.Context 中传递。
func IntoContext(ctx context.Context, d *Context) context.Context {
	return context.WithValue(ctx, ctxKey{}, d)
}

func FromContext(ctx context.Context) *Context {
	if d, ok := ctx.Value(ctxKey{}).(*Context); ok {
		return d
	}
	return nil
}

// Note 记录一条关键状态（如接受/拒绝/无法判定的理由）。
func (c *Context) Note(format string, args ...any) {
	c.entries = append(c.entries, Entry{
		At:      time.Now().UTC(),
		Message: SprintfRedacted(c.redact, format, args...),
	})
}

// Entries 返回已记录条目。
func (c *Context) Entries() []Entry { return c.entries }

// EntriesText 以纯文本切片返回条目消息（供响应体诊断字段使用）。
func (c *Context) EntriesText() []string {
	out := make([]string, 0, len(c.entries))
	for _, e := range c.entries {
		out = append(out, e.Message)
	}
	return out
}

// SprintfRedacted 在格式化前对参数中的地址做脱敏（redact=true 时）。
// 调用方可先经 RedactAddr 得到字符串，也可直接传 netip.Addr。
func SprintfRedacted(redact bool, format string, args ...any) string {
	if redact {
		for i, a := range args {
			args[i] = redactValue(a)
		}
	}
	return fmt.Sprintf(format, args...)
}

// RedactAddr 对地址脱敏：IPv4 保留前两个八位组，IPv6 保留前 16 位，
// 其余以固定占位符替代。默认路由与空地址不脱敏（无识别价值）。
func RedactAddr(a netip.Addr) string {
	if !a.IsValid() {
		return "<addr>"
	}
	if a.Is4() || a.Is4In6() {
		s := a.String()
		parts := splitDots(s)
		if len(parts) != 4 {
			return "<ipv4>"
		}
		return parts[0] + "." + parts[1] + ".x.x"
	}
	// IPv6：保留第一组（16 位），其余压缩占位，保留族可辨识性。
	s := a.String()
	for i := 0; i < len(s); i++ {
		if s[i] == ':' {
			return s[:i] + "::xxxx"
		}
	}
	return "xxxx::xxxx"
}

func redactValue(v any) any {
	switch x := v.(type) {
	case netip.Addr:
		return RedactAddr(x)
	case string:
		// 字符串可能内嵌地址；尝试整体解析为地址，否则保持原样，
		// 避免对自由文本做高误伤的正则替换。
		if a, err := netip.ParseAddr(x); err == nil {
			return RedactAddr(a)
		}
	}
	return v
}

func splitDots(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '.' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	out = append(out, s[start:])
	return out
}

// ---- 结构化访问日志 ----

var (
	loggerPtr atomic.Pointer[slog.Logger]
	seq       atomic.Int64
)

func init() {
	loggerPtr.Store(slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{
		Level: slog.LevelInfo,
	})))
}

// SetLogger 允许 main 按配置替换全局 logger。
func SetLogger(l *slog.Logger) { loggerPtr.Store(l) }

// Logger 返回全局 logger。
func Logger() *slog.Logger { return loggerPtr.Load() }

// NewLogger 按级别名构建文本 logger。
func NewLogger(level string) *slog.Logger {
	var lvl slog.Level
	switch level {
	case "debug":
		lvl = slog.LevelDebug
	case "warn":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	default:
		lvl = slog.LevelInfo
	}
	return slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: lvl}))
}

// EventNo 返回进程内单调递增的事件序号（与请求标识一起出现在日志中）。
func EventNo() int64 { return seq.Add(1) }
