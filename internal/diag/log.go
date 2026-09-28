// Package diag 提供结构化诊断日志与敏感信息脱敏。
//
// 诊断原则：
//   - 每条日志都可带 request_id（记录/请求标识）与关键状态（版本、结论、理由）；
//   - Pod IP 属于敏感拓扑信息，只打印脱敏形式（IPv4 保留前两段）；
//   - 判定体落库/出日志前调用 RedactDecision，去除原始 IP。
package diag

import (
	"encoding/json"
	"log/slog"
	"net/netip"
	"os"
	"strings"

	"netpolreach/internal/engine"
)

// MaskIP 对 IP 做脱敏：IPv4 保留前两段（如 10.0.x.x），IPv6 保留首段。
// 非法输入原样返回（通常已经是空串），避免误导调用方。
func MaskIP(ip string) string {
	if ip == "" {
		return ""
	}
	addr, err := netip.ParseAddr(ip)
	if err != nil {
		return "***"
	}
	if addr.Is4() {
		p := addr.As4()
		return itoa(int(p[0])) + "." + itoa(int(p[1])) + ".x.x"
	}
	// IPv6：保留第一个 hextet，其余折叠。
	s := addr.String()
	if i := strings.Index(s, ":"); i >= 0 {
		return s[:i] + "::****"
	}
	return "****"
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [3]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}

// redactedDecision 是落库/出日志的判定投影：不带 PodIP。
type redactedDecision struct {
	Probe         string             `json:"probe"`
	Verdict       engine.Verdict     `json:"verdict"`
	Reason        string             `json:"reason"`
	AllowedBy     []engine.Match     `json:"allowed_by,omitempty"`
	DeniedBy      []string           `json:"denied_by,omitempty"`
	ResolvedPort  int                `json:"resolved_port,omitempty"`
	ResolvedProto string             `json:"resolved_protocol,omitempty"`
	Ingress       *engine.SideDecision `json:"ingress,omitempty"`
	Egress        *engine.SideDecision `json:"egress,omitempty"`
	LabelVersion  string             `json:"label_version"`
	PolicyVersion string             `json:"policy_version"`
	UnknownDetail string             `json:"unknown_detail,omitempty"`
}

// RedactDecisionJSON 把判定序列化为脱敏 JSON（不含任何端点 PodIP）。
func RedactDecisionJSON(d engine.Decision) ([]byte, error) {
	rd := redactedDecision{
		Probe:         d.Probe.Key(),
		Verdict:       d.Verdict,
		Reason:        d.Reason,
		AllowedBy:     d.AllowedBy,
		DeniedBy:      d.DeniedBy,
		ResolvedPort:  d.ResolvedPort,
		ResolvedProto: string(d.ResolvedProto),
		Ingress:       d.Ingress,
		Egress:        d.Egress,
		LabelVersion:  d.LabelVersion,
		PolicyVersion: d.PolicyVersion,
		UnknownDetail: d.UnknownDetail,
	}
	return json.Marshal(rd)
}

// Logger 包装 slog，强制所有诊断走 request_id + 关键字段。
type Logger struct {
	l *slog.Logger
}

// NewLogger 创建 JSON 结构化 logger。debug=true 时输出 DEBUG 级别（含两侧细节）。
func NewLogger(debug bool) *Logger {
	level := slog.LevelInfo
	if debug {
		level = slog.LevelDebug
	}
	h := slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: level})
	return &Logger{l: slog.New(h)}
}

// Slog 暴露底层 logger 给中间件取带 request_id 的子 logger。
func (g *Logger) Slog() *slog.Logger { return g.l }

// Decision 记录一次判定的诊断（脱敏）。
func (g *Logger) Decision(requestID string, d engine.Decision) {
	attrs := []any{
		"request_id", requestID,
		"probe", d.Probe.Key(),
		"verdict", string(d.Verdict),
		"reason", d.Reason,
		"label_version", d.LabelVersion,
		"policy_version", d.PolicyVersion,
	}
	if d.ResolvedPort != 0 {
		attrs = append(attrs, "resolved_port", d.ResolvedPort, "resolved_proto", string(d.ResolvedProto))
	}
	if len(d.AllowedBy) > 0 {
		attrs = append(attrs, "allowed_by", matchesSummary(d.AllowedBy))
	}
	if len(d.DeniedBy) > 0 {
		attrs = append(attrs, "denied_by", d.DeniedBy)
	}
	switch d.Verdict {
	case engine.VerdictAllow:
		g.l.Info("decision allow", attrs...)
	case engine.VerdictDeny:
		g.l.Info("decision deny", attrs...)
		if d.Ingress != nil && !d.Ingress.Allowed {
			g.l.Debug("deny ingress detail",
				"request_id", requestID, "ingress_reason", d.Ingress.Reason,
				"selecting", d.Ingress.SelectingPolicies)
		}
		if d.Egress != nil && !d.Egress.Allowed {
			g.l.Debug("deny egress detail",
				"request_id", requestID, "egress_reason", d.Egress.Reason,
				"selecting", d.Egress.SelectingPolicies)
		}
	default:
		g.l.Warn("decision unknown", append(attrs, "detail", d.UnknownDetail)...)
	}
}

func matchesSummary(ms []engine.Match) []string {
	out := make([]string, 0, len(ms))
	for _, m := range ms {
		out = append(out, string(m.Direction)+":"+m.PolicyUID+"/r"+itoa(m.RuleIndex))
	}
	return out
}

// Reconcile 记录协调结果。
func (g *Logger) Reconcile(ok bool, msg string, attrs ...any) {
	base := []any{"event", "reconcile", "ok", ok}
	base = append(base, attrs...)
	if ok {
		g.l.Info(msg, base...)
	} else {
		g.l.Error(msg, base...)
	}
}
