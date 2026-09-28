// Package engine 是离线可达性判定核心。
//
// 判定模型（与 Kubernetes NetworkPolicy 语义对齐的离线子集）：
//
//	probe = (源端点, 目标端点, 端口, 协议)
//	入口允许集 = 在目标端点上求值其命名空间内选中它的 Ingress 策略；
//	出口允许集 = 在源端点上求值其命名空间内选中它的 Egress 策略；
//	连接被允许 当且仅当【两侧都允许】。
//
// 关键不变量：
//   - 未被任何对应方向策略选中的端点默认允许（隔离只来自“被选中”）；
//   - 命名端口在【目标端点】解析为数字端口，绝不当作全局数字；
//   - 标签快照的 policy_version 与策略集合 version 不一致时一律 UNKNOWN；
//   - 每个判定都带理由码与策略来源，便于离线诊断。
package engine

import (
	"errors"
	"fmt"
	"sort"

	"netpolreach/internal/model"
	"netpolreach/internal/policy"
)

// Verdict 三态结论。
type Verdict string

const (
	VerdictAllow   Verdict = "ALLOW"
	VerdictDeny    Verdict = "DENY"
	VerdictUnknown Verdict = "UNKNOWN"
)

// 理由码：UNKNOWN
const (
	ReasonVersionMismatch       = "VERSION_MISMATCH"
	ReasonSourceEndpointMissing = "SOURCE_ENDPOINT_NOT_FOUND"
	ReasonDestEndpointMissing   = "DEST_ENDPOINT_NOT_FOUND"
)

// 理由码：DENY
const (
	ReasonIngressDenied       = "INGRESS_DENIED"
	ReasonEgressDenied        = "EGRESS_DENIED"
	ReasonBothSidesDenied     = "BOTH_SIDES_DENIED"
	ReasonNamedPortNotFound   = "NAMED_PORT_NOT_FOUND"
)

// 理由码：ALLOW（侧理由）
const (
	ReasonAllowedByRules       = "ALLOWED_BY_RULE"
	ReasonNoIngressSelects     = "NO_INGRESS_POLICY_SELECTS_DEFAULT_ALLOW"
	ReasonNoEgressSelects      = "NO_EGRESS_POLICY_SELECTS_DEFAULT_ALLOW"
)

// 侧阻断理由
const (
	ReasonIngressNoRule = "INGRESS_POLICY_SELECTS_POD_BUT_NO_RULE_MATCHES"
	ReasonEgressNoRule  = "EGRESS_POLICY_SELECTS_POD_BUT_NO_RULE_MATCHES"
)

// 最终允许理由
const (
	ReasonAllowed = "ALLOWED"
)

// Match 指向一条具体的允许规则及其所属策略。
type Match struct {
	PolicyUID  string            `json:"policy_uid"`
	PolicyName string            `json:"policy_name"`
	Namespace  string            `json:"namespace"`
	RuleIndex  int               `json:"rule_index"`
	Direction  model.Direction   `json:"direction"`
}

// SideDecision 是单侧（入口或出口）独立合成的允许集合结论。
type SideDecision struct {
	Allowed           bool     `json:"allowed"`
	Reason            string   `json:"reason"`
	SelectingPolicies []string `json:"selecting_policies,omitempty"`
	Matches           []Match  `json:"matches,omitempty"`
}

// Decision 是一次探测的完整判定：结论、理由、两侧依据与版本戳。
type Decision struct {
	Probe         model.Probe   `json:"probe"`
	Verdict       Verdict       `json:"verdict"`
	Reason        string        `json:"reason"`
	AllowedBy     []Match       `json:"allowed_by,omitempty"`
	DeniedBy      []string      `json:"denied_by,omitempty"`
	Ingress       *SideDecision `json:"ingress,omitempty"`
	Egress        *SideDecision `json:"egress,omitempty"`
	ResolvedPort  int           `json:"resolved_port,omitempty"`
	ResolvedProto model.Protocol `json:"resolved_protocol,omitempty"`
	LabelVersion  string        `json:"label_version"`
	PolicyVersion string        `json:"policy_version"`
	UnknownDetail string        `json:"unknown_detail,omitempty"`
}

// Compiled 是快照 + 策略集合的不可变编译产物（带索引）。
type Compiled struct {
	snapshot model.Snapshot
	policies []model.Policy
	endpoint map[string]*model.Endpoint // ns/name
	nsByKey  map[string]model.Namespace
}

// Engine 基于固定的快照版本做判定；换版本需重新构造。
type Engine struct {
	c *Compiled
	// versions 记录判定依据的版本；不一致时 Decide 一律 UNKNOWN。
	snapshotPolicyVersion string
	policySetVersion      string
	labelVersion          string
	versionConsistent     bool
}

// Compile 对已通过 Validate 的快照与策略集合建索引。
func Compile(snap model.Snapshot, ps model.PolicySet) (*Compiled, error) {
	c := &Compiled{
		snapshot: snap,
		policies: ps.Policies,
		endpoint: make(map[string]*model.Endpoint, len(snap.Endpoints)),
		nsByKey:  make(map[string]model.Namespace, len(snap.Namespaces)),
	}
	for i := range snap.Namespaces {
		n := snap.Namespaces[i]
		c.nsByKey[n.Name] = n
	}
	for i := range snap.Endpoints {
		ep := &snap.Endpoints[i]
		c.endpoint[ep.Namespace+"/"+ep.Name] = ep
	}
	return c, nil
}

// NewEngine 编译并校验快照与策略集合。版本不一致不报错，
// 但该引擎对所有探测返回 VERSION_MISMATCH 的 UNKNOWN（无法判定，而非猜测）。
func NewEngine(snap model.Snapshot, ps model.PolicySet) (*Engine, error) {
	if err := policy.ValidateSnapshot(snap); err != nil {
		return nil, fmt.Errorf("snapshot 校验失败: %w", err)
	}
	if err := policy.ValidatePolicySet(ps); err != nil {
		return nil, fmt.Errorf("policy set 校验失败: %w", err)
	}
	c, err := Compile(snap, ps)
	if err != nil {
		return nil, err
	}
	return &Engine{
		c:                     c,
		snapshotPolicyVersion: snap.PolicyVersion,
		policySetVersion:      ps.Version,
		labelVersion:          snap.LabelVersion,
		versionConsistent:     snap.PolicyVersion == ps.Version,
	}, nil
}

// ErrProbeInvalid 在探测本身不合法时返回。
var ErrProbeInvalid = errors.New("probe invalid")

// ValidateProbe 校验单次探测：数字端口与命名端口互斥。
func ValidateProbe(p model.Probe) error {
	if p.From.Namespace == "" || p.From.Name == "" || p.To.Namespace == "" || p.To.Name == "" {
		return fmt.Errorf("%w: from/to 必须完整", ErrProbeInvalid)
	}
	if (p.Port == 0) == (p.NamedPort == "") {
		return fmt.Errorf("%w: 数字端口与命名端口必须二选一", ErrProbeInvalid)
	}
	if p.Port != 0 && (p.Port < 1 || p.Port > 65535) {
		return fmt.Errorf("%w: 端口 %d 越界", ErrProbeInvalid, p.Port)
	}
	if p.NamedPort != "" && (len(p.NamedPort) > 15) {
		return fmt.Errorf("%w: 命名端口过长", ErrProbeInvalid)
	}
	switch p.Protocol {
	case "", model.ProtocolTCP, model.ProtocolUDP, model.ProtocolSCTP:
	default:
		return fmt.Errorf("%w: 协议 %q 非法", ErrProbeInvalid, p.Protocol)
	}
	return nil
}

// Decide 对一次探测给出三态判定。
func (e *Engine) Decide(p model.Probe) Decision {
	base := Decision{
		Probe:         p,
		LabelVersion:  e.labelVersion,
		PolicyVersion: e.policySetVersion,
	}

	if !e.versionConsistent {
		base.Verdict = VerdictUnknown
		base.Reason = ReasonVersionMismatch
		base.UnknownDetail = fmt.Sprintf(
			"snapshot.policy_version=%q 与 policy_set.version=%q 不一致；标签快照与策略版本必须一致才能判定",
			e.snapshotPolicyVersion, e.policySetVersion)
		return base
	}
	if err := ValidateProbe(p); err != nil {
		base.Verdict = VerdictUnknown
		base.Reason = "PROBE_INVALID"
		base.UnknownDetail = err.Error()
		return base
	}

	src, okSrc := e.c.endpoint[p.From.Namespace+"/"+p.From.Name]
	dst, okDst := e.c.endpoint[p.To.Namespace+"/"+p.To.Name]
	if !okSrc {
		base.Verdict = VerdictUnknown
		base.Reason = ReasonSourceEndpointMissing
		base.UnknownDetail = "快照中不存在源端点 " + p.From.String()
		return base
	}
	if !okDst {
		base.Verdict = VerdictUnknown
		base.Reason = ReasonDestEndpointMissing
		base.UnknownDetail = "快照中不存在目标端点 " + p.To.String()
		return base
	}

	// 命名端口只在目标端解析。数字端口直接使用。
	var port int
	var proto model.Protocol
	if p.NamedPort != "" {
		n, pr, found := policy.ResolveNamedPort(*dst, p.NamedPort, p.Protocol)
		if !found {
			base.Verdict = VerdictDeny
			base.Reason = ReasonNamedPortNotFound
			base.DeniedBy = []string{"NAMED_PORT_RESOLUTION"}
			base.UnknownDetail = fmt.Sprintf(
				"目标端点 %s 的端口声明中没有命名端口 %q", p.To.String(), p.NamedPort)
			return base
		}
		port, proto = n, pr
	} else {
		port, proto = p.Port, p.Protocol.EffectiveProtocol()
	}
	base.ResolvedPort = port
	base.ResolvedProto = proto

	// 分别独立合成入口允许集（目标端视角）与出口允许集（源端视角）。
	ing := e.evaluateSide(model.Ingress, dst, src, port, proto)
	egr := e.evaluateSide(model.Egress, src, dst, port, proto)
	base.Ingress = ing
	base.Egress = egr

	allowed := append([]Match{}, ing.Matches...)
	allowed = append(allowed, egr.Matches...)

	switch {
	case ing.Allowed && egr.Allowed:
		base.Verdict = VerdictAllow
		base.Reason = ReasonAllowed
		base.AllowedBy = dedupSort(allowed)
	case !ing.Allowed && !egr.Allowed:
		base.Verdict = VerdictDeny
		base.Reason = ReasonBothSidesDenied
		base.DeniedBy = []string{"INGRESS", "EGRESS"}
	case !ing.Allowed:
		base.Verdict = VerdictDeny
		base.Reason = ReasonIngressDenied
		base.DeniedBy = []string{"INGRESS"}
	default:
		base.Verdict = VerdictDeny
		base.Reason = ReasonEgressDenied
		base.DeniedBy = []string{"EGRESS"}
	}
	return base
}

// Matrix 对一批探测逐一判定，保持输入顺序（调用方通常以稳定序生成）。
func (e *Engine) Matrix(probes []model.Probe) []Decision {
	out := make([]Decision, len(probes))
	for i, p := range probes {
		out[i] = e.Decide(p)
	}
	return out
}

// evaluateSide 合成单侧允许集。
//
// sideEP 是策略作用的端点（Ingress=目标，Egress=源）；
// otherEP 是对端（Ingress=源，Egress=目标）；
// 命名端口规则引用统一在目标端点（dst）解析。
func (e *Engine) evaluateSide(d model.Direction, sideEP, otherEP *model.Endpoint, port int, proto model.Protocol) *SideDecision {
	var selecting []string
	var matches []Match

	sideNS := e.c.nsByKey[sideEP.Namespace]

	for i := range e.c.policies {
		pol := &e.c.policies[i]
		if pol.Namespace != sideEP.Namespace {
			continue
		}
		if !pol.AppliesType(d) {
			continue
		}
		if !policy.SelectorMatches(pol.PodSelector, sideEP.Labels) {
			continue
		}
		selecting = append(selecting, pol.UID)

		rules := pol.Ingress
		if d == model.Egress {
			rules = pol.Egress
		}
		for ri, rule := range rules {
			if !rulePortMatches(rule, otherEP, port, proto) {
				continue
			}
			if !rulePeerMatches(rule, sideNS, otherEP, e.c.nsByKey) {
				continue
			}
			matches = append(matches, Match{
				PolicyUID:  pol.UID,
				PolicyName: pol.Name,
				Namespace:  pol.Namespace,
				RuleIndex:  ri,
				Direction:  d,
			})
		}
	}

	sort.Strings(selecting)
	matches = dedupSort(matches)

	// 默认行为：未被任何该方向策略选中 => 默认允许。
	if len(selecting) == 0 {
		if d == model.Ingress {
			return &SideDecision{Allowed: true, Reason: ReasonNoIngressSelects}
		}
		return &SideDecision{Allowed: true, Reason: ReasonNoEgressSelects}
	}
	if len(matches) > 0 {
		return &SideDecision{
			Allowed:           true,
			Reason:            ReasonAllowedByRules,
			SelectingPolicies: selecting,
			Matches:           matches,
		}
	}
	if d == model.Ingress {
		return &SideDecision{Allowed: false, Reason: ReasonIngressNoRule, SelectingPolicies: selecting}
	}
	return &SideDecision{Allowed: false, Reason: ReasonEgressNoRule, SelectingPolicies: selecting}
}

// rulePortMatches 判定规则的端口集合是否覆盖探测端口。
// 空端口集合表示全部端口；命名端口引用在目标端点 otherEP 上解析。
func rulePortMatches(rule model.Rule, dstEP *model.Endpoint, port int, proto model.Protocol) bool {
	if len(rule.Ports) == 0 {
		return true
	}
	for _, pr := range rule.Ports {
		if pr.IsNamed() {
			n, p, found := policy.ResolveNamedPort(*dstEP, pr.Name, pr.Protocol)
			if !found {
				continue
			}
			if n == port && p == proto {
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

// rulePeerMatches 判定规则的对端集合是否包含 otherEP。空对端集合表示全部对端。
func rulePeerMatches(rule model.Rule, sideNS model.Namespace, otherEP *model.Endpoint, allNS map[string]model.Namespace) bool {
	if len(rule.Peers) == 0 {
		return true
	}
	for _, peer := range rule.Peers {
		if policy.PeerMatchesPod(peer, sideNS, *otherEP, allNS) {
			return true
		}
	}
	return false
}
