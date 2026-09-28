// Package oracle 是独立参考判定器：它不导入 internal/engine，
// 仅依赖 model 与最基础的标准库，用最直白的朴素循环重新实现
// NetworkPolicy 离线子集的判定语义，作为被测引擎的交叉验证基准。
//
// 与引擎的差异点是刻意的：
//   - 不建任何索引，按策略/规则/对端线性扫描；
//   - 不共享选择器/端口匹配代码（本包内另写一份）；
//   - 结论只给三态与理由，结构不同于 engine.Decision。
//
// 因此引擎与 oracle 同时犯同一个实现错误的概率显著降低。
package oracle

import (
	"fmt"

	"netpolreach/internal/model"
)

// Verdict 参考结论三态。
type Verdict string

const (
	VAllow   Verdict = "ALLOW"
	VDeny    Verdict = "DENY"
	VUnknown Verdict = "UNKNOWN"
)

// Decision 是参考判定结论。
type Decision struct {
	Verdict       Verdict
	Reason        string
	IngressAllow  bool
	EgressAllow   bool
	IngressRules  []string // "uid#index"
	EgressRules   []string
	ResolvedPort  int
	ResolvedProto model.Protocol
}

// Engine 是朴素参考引擎。
type Engine struct {
	snap    model.Snapshot
	pols    []model.Policy
	consist bool
}

// NewWithVersion 显式传入策略集合版本构造参考引擎。
func NewWithVersion(snap model.Snapshot, ps model.PolicySet) *Engine {
	return &Engine{snap: snap, pols: ps.Policies, consist: snap.PolicyVersion == ps.Version}
}

func (e *Engine) findEp(ref model.EndpointRef) (model.Endpoint, bool) {
	for _, ep := range e.snap.Endpoints {
		if ep.Namespace == ref.Namespace && ep.Name == ref.Name {
			return ep, true
		}
	}
	return model.Endpoint{}, false
}

func (e *Engine) nsLabels(ns string) model.Labels {
	for _, n := range e.snap.Namespaces {
		if n.Name == ns {
			return n.Labels
		}
	}
	return nil
}

// Decide 用朴素语义判定一次探测。
func (e *Engine) Decide(p model.Probe) Decision {
	if !e.consist {
		return Decision{Verdict: VUnknown, Reason: "VERSION_MISMATCH"}
	}
	src, ok1 := e.findEp(p.From)
	dst, ok2 := e.findEp(p.To)
	if !ok1 {
		return Decision{Verdict: VUnknown, Reason: "SOURCE_ENDPOINT_NOT_FOUND"}
	}
	if !ok2 {
		return Decision{Verdict: VUnknown, Reason: "DEST_ENDPOINT_NOT_FOUND"}
	}
	if (p.Port == 0) == (p.NamedPort == "") {
		return Decision{Verdict: VUnknown, Reason: "PROBE_INVALID"}
	}

	port, proto := p.Port, p.Protocol.EffectiveProtocol()
	if p.NamedPort != "" {
		n, pr, found := resolveNamed(dst, p.NamedPort, p.Protocol)
		if !found {
			return Decision{Verdict: VDeny, Reason: "NAMED_PORT_NOT_FOUND"}
		}
		port, proto = n, pr
	}

	inAllow, inRules := e.side("Ingress", dst, src, port, proto)
	egAllow, egRules := e.side("Egress", src, dst, port, proto)
	d := Decision{
		IngressAllow: inAllow,
		EgressAllow:  egAllow,
		IngressRules: inRules,
		EgressRules:  egRules,
		ResolvedPort: port, ResolvedProto: proto,
	}
	switch {
	case inAllow && egAllow:
		d.Verdict, d.Reason = VAllow, "ALLOWED"
	case !inAllow && !egAllow:
		d.Verdict, d.Reason = VDeny, "BOTH_SIDES_DENIED"
	case !inAllow:
		d.Verdict, d.Reason = VDeny, "INGRESS_DENIED"
	default:
		d.Verdict, d.Reason = VDeny, "EGRESS_DENIED"
	}
	return d
}

// side 返回 (是否允许, 命中的规则来源)。朴素实现，刻意不与引擎共享代码。
func (e *Engine) side(dir string, isolated, peer model.Endpoint, port int, proto model.Protocol) (bool, []string) {
	selected := false
	var hits []string
	for _, pol := range e.pols {
		if pol.Namespace != isolated.Namespace {
			continue
		}
		if !applies(pol, dir) {
			continue
		}
		if !labelsMatch(pol.PodSelector, isolated.Labels) {
			continue
		}
		selected = true
		rules := pol.Ingress
		if dir == "Egress" {
			rules = pol.Egress
		}
		for i, rule := range rules {
			if !portMatch(rule, peer, port, proto) {
				continue
			}
			if !peerMatch(rule, isolated, peer, e) {
				continue
			}
			hits = append(hits, fmt.Sprintf("%s#%d", pol.UID, i))
		}
	}
	// 未被选中 => 默认允许。
	if !selected {
		return true, nil
	}
	return len(hits) > 0, hits
}

func applies(p model.Policy, dir string) bool {
	want := model.Direction(dir)
	for _, t := range p.PolicyTypes {
		if t == want {
			return true
		}
	}
	if want == model.Ingress && len(p.Ingress) > 0 {
		return true
	}
	if want == model.Egress && len(p.Egress) > 0 {
		return true
	}
	return false
}

func portMatch(rule model.Rule, dst model.Endpoint, port int, proto model.Protocol) bool {
	if len(rule.Ports) == 0 {
		return true
	}
	for _, pr := range rule.Ports {
		if pr.Name != "" {
			if n, pproto, ok := resolveNamed(dst, pr.Name, pr.Protocol); ok && n == port && pproto == proto {
				return true
			}
			continue
		}
		if pr.Port == port && pr.Protocol.EffectiveProtocol() == proto {
			return true
		}
	}
	return false
}

func peerMatch(rule model.Rule, isolated model.Endpoint, peer model.Endpoint, e *Engine) bool {
	if len(rule.Peers) == 0 {
		return true
	}
	for _, pr := range rule.Peers {
		if pr.IPBlock != nil {
			// 本参考实现的夹具未使用 ipBlock；显式不支持以免给出错误基准。
			continue
		}
		nsSel := pr.NamespaceSelector
		podSel := pr.PodSelector
		switch {
		case nsSel == nil && podSel == nil:
			return true
		case podSel != nil && nsSel == nil:
			if peer.Namespace != isolated.Namespace {
				continue
			}
			if labelsMatchDeref(podSel, peer.Labels) {
				return true
			}
		case podSel == nil && nsSel != nil:
			if labelsMatchDeref(nsSel, e.nsLabels(peer.Namespace)) {
				return true
			}
		default:
			if labelsMatchDeref(nsSel, e.nsLabels(peer.Namespace)) &&
				labelsMatchDeref(podSel, peer.Labels) {
				return true
			}
		}
	}
	return false
}

func labelsMatchDeref(s *model.LabelSelector, ls model.Labels) bool {
	return labelsMatch(*s, ls)
}

// labelsMatch 独立实现的选择器求值。
func labelsMatch(s model.LabelSelector, ls model.Labels) bool {
	for k, v := range s.MatchLabels {
		if ls[k] != v {
			return false
		}
	}
	for _, ex := range s.MatchExpressions {
		v, has := ls[ex.Key]
		switch ex.Operator {
		case model.OpExists:
			if !has {
				return false
			}
		case model.OpDoesNotExist:
			if has {
				return false
			}
		case model.OpIn:
			if !has || !containsStr(ex.Values, v) {
				return false
			}
		case model.OpNotIn:
			if has && containsStr(ex.Values, v) {
				return false
			}
		}
	}
	return true
}

func resolveNamed(ep model.Endpoint, name string, hint model.Protocol) (int, model.Protocol, bool) {
	for _, cp := range ep.Ports {
		if cp.Name == name {
			pr := cp.Protocol.EffectiveProtocol()
			if hint != "" {
				pr = hint
			}
			return cp.Port, pr, true
		}
	}
	return 0, "", false
}

func containsStr(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}
