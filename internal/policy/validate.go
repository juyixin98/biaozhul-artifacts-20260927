// Package policy 提供资源模型的校验、标签选择器求值，以及命名端口解析。
// 这些函数被判定引擎与加载器共用，保证“入口/出口两侧”使用完全一致的匹配语义。
package policy

import (
	"fmt"
	"net/netip"
	"regexp"
	"strings"

	"netpolreach/internal/model"
)

// 名称规则（对齐 DNS-1123 label 的常用子集，错误信息用中文说明）。
var (
	nameRegexp   = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)
	portNameRe   = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`)
	labelKeyRe   = regexp.MustCompile(`^[a-z0-9A-Z]([-._a-z0-9A-Z]*[a-z0-9A-Z])?$`)
)

// ValidatePolicySet 校验策略集合整体，返回首个问题。
func ValidatePolicySet(ps model.PolicySet) error {
	if strings.TrimSpace(ps.Version) == "" {
		return fmt.Errorf("policy set 缺少 version")
	}
	uids := make(map[string]struct{}, len(ps.Policies))
	for i := range ps.Policies {
		p := &ps.Policies[i]
		if err := ValidatePolicy(*p); err != nil {
			return fmt.Errorf("policy[%d] %s 非法: %w", i, p.UID, err)
		}
		if _, dup := uids[p.UID]; dup {
			return fmt.Errorf("policy uid 重复: %s", p.UID)
		}
		uids[p.UID] = struct{}{}
	}
	return nil
}

// ValidatePolicy 校验单条策略。
func ValidatePolicy(p model.Policy) error {
	if strings.TrimSpace(p.UID) == "" {
		return fmt.Errorf("uid 为空")
	}
	if !nameRegexp.MatchString(p.Namespace) {
		return fmt.Errorf("namespace %q 非法", p.Namespace)
	}
	if !nameRegexp.MatchString(p.Name) {
		return fmt.Errorf("name %q 非法", p.Name)
	}
	if err := ValidateSelector(p.PodSelector); err != nil {
		return fmt.Errorf("pod_selector 非法: %w", err)
	}
	for _, t := range p.PolicyTypes {
		if t != model.Ingress && t != model.Egress {
			return fmt.Errorf("policy_type %q 非法（仅支持 Ingress/Egress）", t)
		}
	}
	for ri, r := range p.Ingress {
		if err := validateRule(r); err != nil {
			return fmt.Errorf("ingress 规则 %d: %w", ri, err)
		}
	}
	for ri, r := range p.Egress {
		if err := validateRule(r); err != nil {
			return fmt.Errorf("egress 规则 %d: %w", ri, err)
		}
	}
	return nil
}

func validateRule(r model.Rule) error {
	for pi := range r.Ports {
		if err := ValidatePortRef(r.Ports[pi]); err != nil {
			return fmt.Errorf("ports[%d]: %w", pi, err)
		}
	}
	for pi, peer := range r.Peers {
		if peer.IPBlock != nil && (peer.PodSelector != nil || peer.NamespaceSelector != nil) {
			return fmt.Errorf("peers[%d]: ipBlock 不能与 selector 混用", pi)
		}
		if peer.IPBlock != nil {
			if _, err := netip.ParsePrefix(peer.IPBlock.CIDR); err != nil {
				return fmt.Errorf("peers[%d]: cidr %q 非法: %w", pi, peer.IPBlock.CIDR, err)
			}
			for _, ex := range peer.IPBlock.Except {
				if _, err := netip.ParsePrefix(ex); err != nil {
					return fmt.Errorf("peers[%d]: except %q 非法: %w", pi, ex, err)
				}
			}
		}
		if peer.PodSelector != nil {
			if err := ValidateSelector(*peer.PodSelector); err != nil {
				return fmt.Errorf("peers[%d]: pod_selector: %w", pi, err)
			}
		}
		if peer.NamespaceSelector != nil {
			if err := ValidateSelector(*peer.NamespaceSelector); err != nil {
				return fmt.Errorf("peers[%d]: namespace_selector: %w", pi, err)
			}
		}
	}
	return nil
}

// ValidatePortRef 强制数字端口与命名端口互斥、取值合法。
func ValidatePortRef(pr model.PortRef) error {
	if pr.IsNamed() && pr.Port != 0 {
		return fmt.Errorf("数字端口与命名端口不能同时设置")
	}
	if pr.IsNamed() {
		if len(pr.Name) > 15 || !portNameRe.MatchString(pr.Name) {
			return fmt.Errorf("命名端口 %q 非法（小写字母数字及 '-'，最长 15）", pr.Name)
		}
	} else {
		if pr.Port < 1 || pr.Port > 65535 {
			return fmt.Errorf("数字端口 %d 超出 1..65535", pr.Port)
		}
	}
	switch pr.Protocol {
	case "", model.ProtocolTCP, model.ProtocolUDP, model.ProtocolSCTP:
	default:
		return fmt.Errorf("协议 %q 非法", pr.Protocol)
	}
	return nil
}

// ValidateSelector 校验选择器表达式可求值。
func ValidateSelector(s model.LabelSelector) error {
	for k := range s.MatchLabels {
		if !labelKeyRe.MatchString(k) {
			return fmt.Errorf("match_labels 键 %q 非法", k)
		}
	}
	for i, e := range s.MatchExpressions {
		if !labelKeyRe.MatchString(e.Key) {
			return fmt.Errorf("match_expressions[%d] 键 %q 非法", i, e.Key)
		}
		switch e.Operator {
		case model.OpIn, model.OpNotIn:
			if len(e.Values) == 0 {
				return fmt.Errorf("match_expressions[%d] 运算符 %s 至少需要一个 value", i, e.Operator)
			}
		case model.OpExists, model.OpDoesNotExist:
			if len(e.Values) != 0 {
				return fmt.Errorf("match_expressions[%d] 运算符 %s 不应带 values", i, e.Operator)
			}
		default:
			return fmt.Errorf("match_expressions[%d] 运算符 %q 非法", i, e.Operator)
		}
	}
	return nil
}

// ValidateSnapshot 校验标签快照：版本、命名空间、端点、命名端口声明。
func ValidateSnapshot(s model.Snapshot) error {
	if strings.TrimSpace(s.LabelVersion) == "" {
		return fmt.Errorf("snapshot 缺少 label_version")
	}
	if strings.TrimSpace(s.PolicyVersion) == "" {
		return fmt.Errorf("snapshot 缺少 policy_version")
	}
	ns := make(map[string]model.Labels, len(s.Namespaces))
	for i, n := range s.Namespaces {
		if !nameRegexp.MatchString(n.Name) {
			return fmt.Errorf("namespaces[%d] 名称 %q 非法", i, n.Name)
		}
		if _, dup := ns[n.Name]; dup {
			return fmt.Errorf("namespace 重复: %s", n.Name)
		}
		ns[n.Name] = n.Labels
	}
	uids := make(map[string]struct{}, len(s.Endpoints))
	keys := make(map[string]struct{}, len(s.Endpoints))
	for i, ep := range s.Endpoints {
		if !nameRegexp.MatchString(ep.Namespace) {
			return fmt.Errorf("endpoints[%d] namespace %q 非法", i, ep.Namespace)
		}
		if _, ok := ns[ep.Namespace]; !ok {
			return fmt.Errorf("endpoints[%d] 引用了未知 namespace %q", i, ep.Namespace)
		}
		if !nameRegexp.MatchString(ep.Name) {
			return fmt.Errorf("endpoints[%d] name %q 非法", i, ep.Name)
		}
		if strings.TrimSpace(ep.UID) == "" {
			return fmt.Errorf("endpoints[%d] uid 为空", i)
		}
		if _, dup := uids[ep.UID]; dup {
			return fmt.Errorf("endpoint uid 重复: %s", ep.UID)
		}
		uids[ep.UID] = struct{}{}
		k := ep.Namespace + "/" + ep.Name
		if _, dup := keys[k]; dup {
			return fmt.Errorf("endpoint 在命名空间内重名: %s", k)
		}
		keys[k] = struct{}{}
		for lk, v := range ep.Labels {
			if !labelKeyRe.MatchString(lk) {
				return fmt.Errorf("endpoint %s 标签键 %q 非法", k, lk)
			}
			_ = v
		}
		// 同一端点内命名端口不得重名（目标端解析必须确定）。
		portNames := make(map[string]struct{})
		for _, cp := range ep.Ports {
			if cp.Name != "" {
				if len(cp.Name) > 15 || !portNameRe.MatchString(cp.Name) {
					return fmt.Errorf("endpoint %s 命名端口 %q 非法", k, cp.Name)
				}
				if _, dup := portNames[cp.Name]; dup {
					return fmt.Errorf("endpoint %s 命名端口重名: %s", k, cp.Name)
				}
				portNames[cp.Name] = struct{}{}
			}
			if cp.Port < 1 || cp.Port > 65535 {
				return fmt.Errorf("endpoint %s 容器端口 %d 非法", k, cp.Port)
			}
			switch cp.Protocol {
			case "", model.ProtocolTCP, model.ProtocolUDP, model.ProtocolSCTP:
			default:
				return fmt.Errorf("endpoint %s 端口协议 %q 非法", k, cp.Protocol)
			}
		}
		if ep.PodIP != "" {
			if _, err := netip.ParseAddr(ep.PodIP); err != nil {
				return fmt.Errorf("endpoint %s pod_ip %q 非法: %w", k, ep.PodIP, err)
			}
		}
	}
	return nil
}

// SelectorMatches 在给定标签集合上求值选择器。
func SelectorMatches(sel model.LabelSelector, labels model.Labels) bool {
	for k, v := range sel.MatchLabels {
		if labels[k] != v {
			return false
		}
	}
	for _, e := range sel.MatchExpressions {
		v, present := labels[e.Key]
		switch e.Operator {
		case model.OpExists:
			if !present {
				return false
			}
		case model.OpDoesNotExist:
			if present {
				return false
			}
		case model.OpIn:
			if !present || !contains(e.Values, v) {
				return false
			}
		case model.OpNotIn:
			if present && contains(e.Values, v) {
				return false
			}
		}
	}
	return true
}

// ResolveNamedPort 在【目标端点】上把命名端口解析为数字端口与协议。
// 这是命名端口解析的唯一位置：命名端口绝不是全局数字，且不同目标可解析为不同端口。
// 未找到时 found=false（调用方据此给出确定性的 NAMED_PORT_NOT_FOUND 阻断）。
func ResolveNamedPort(ep model.Endpoint, named string, probeProto model.Protocol) (port int, proto model.Protocol, found bool) {
	for _, cp := range ep.Ports {
		if cp.Name == named {
			proto = cp.Protocol.EffectiveProtocol()
			// 探测显式给出的协议优先于声明协议。
			if probeProto != "" {
				proto = probeProto
			}
			return cp.Port, proto, true
		}
	}
	return 0, "", false
}

// PeerMatchesPod 判断对端 peer 是否选中 (nsLabels, ep)。
func PeerMatchesPod(peer model.Peer, nsOfPod model.Namespace, ep model.Endpoint, allNS map[string]model.Namespace) bool {
	if peer.IPBlock != nil {
		return ipBlockContains(peer.IPBlock, ep.PodIP)
	}
	switch {
	case peer.PodSelector == nil && peer.NamespaceSelector == nil:
		return true // 任意命名空间的任意 Pod
	case peer.PodSelector != nil && peer.NamespaceSelector == nil:
		// 同命名空间选择器；跨命名空间直接不匹配。
		if ep.Namespace != nsOfPod.Name {
			return false
		}
		return SelectorMatches(*peer.PodSelector, ep.Labels)
	case peer.PodSelector == nil && peer.NamespaceSelector != nil:
		return SelectorMatches(*peer.NamespaceSelector, allNS[ep.Namespace].Labels)
	default:
		// 命名空间选择器 + Pod 选择器组合。
		if !SelectorMatches(*peer.NamespaceSelector, allNS[ep.Namespace].Labels) {
			return false
		}
		return SelectorMatches(*peer.PodSelector, ep.Labels)
	}
}

func ipBlockContains(b *model.IPBlock, ip string) bool {
	if ip == "" {
		return false
	}
	addr, err := netip.ParseAddr(ip)
	if err != nil {
		return false
	}
	prefix, err := netip.ParsePrefix(b.CIDR)
	if err != nil || !prefix.Contains(addr) {
		return false
	}
	for _, ex := range b.Except {
		if epf, err := netip.ParsePrefix(ex); err == nil && epf.Contains(addr) {
			return false
		}
	}
	return true
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}
