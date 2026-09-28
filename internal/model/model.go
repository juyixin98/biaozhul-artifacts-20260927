// Package model 定义受限容器网络策略离线可达性判定所使用的资源模型。
//
// 模型刻意与具体编排系统解耦，但语义对齐 Kubernetes NetworkPolicy：
//   - 端点（Endpoint）携带命名空间、标签快照与“命名端口”声明；
//   - 策略（Policy）由标签选择器选中端点，并分别给出 Ingress / Egress 规则；
//   - 标签快照（Snapshot）带有 label_version 与 policy_version，
//     策略集合（PolicySet）带 version，判定时必须使用同一版本的快照与策略。
package model

import (
	"encoding/json"
	"fmt"
)

// Direction 是策略作用方向。
type Direction string

const (
	Ingress Direction = "Ingress"
	Egress  Direction = "Egress"
)

// Protocol 是端口协议。零值视为 TCP（与 NetworkPolicy 约定一致）。
type Protocol string

const (
	ProtocolTCP  Protocol = "TCP"
	ProtocolUDP  Protocol = "UDP"
	ProtocolSCTP Protocol = "SCTP"
)

// EffectiveProtocol 返回判定时实际使用的协议：未指定时按 TCP 处理。
func (p Protocol) EffectiveProtocol() Protocol {
	if p == "" {
		return ProtocolTCP
	}
	return p
}

// Labels 是标签键值集合。
type Labels map[string]string

// SelectorOperator 是标签选择器的集合运算符。
type SelectorOperator string

const (
	OpIn           SelectorOperator = "In"
	OpNotIn        SelectorOperator = "NotIn"
	OpExists       SelectorOperator = "Exists"
	OpDoesNotExist SelectorOperator = "DoesNotExist"
)

// SelectorRequirement 是单条标签集合表达式要求。
type SelectorRequirement struct {
	Key      string           `json:"key"`
	Operator SelectorOperator `json:"operator"`
	Values   []string         `json:"values,omitempty"`
}

// LabelSelector 对齐 NetworkPolicy 的标签选择器。
// 零值（无 MatchLabels 且无 MatchExpressions）表示“选中全部”。
type LabelSelector struct {
	MatchLabels      Labels                `json:"match_labels,omitempty"`
	MatchExpressions []SelectorRequirement `json:"match_expressions,omitempty"`
}

// Empty 表示该选择器为空选择器（选中命名空间内全部 Pod）。
func (s LabelSelector) Empty() bool {
	return len(s.MatchLabels) == 0 && len(s.MatchExpressions) == 0
}

// IPBlock 表示基于 IP 段的对端（CIDR + 排除段）。
type IPBlock struct {
	CIDR   string   `json:"cidr"`
	Except []string `json:"except,omitempty"`
}

// Peer 是规则中的一个对端。指针为 nil 的选择器语义与 NetworkPolicy 一致：
//   - PodSelector 与 NamespaceSelector 同时为 nil：任意命名空间的任意 Pod；
//   - 仅 PodSelector：与策略同命名空间、且被该选择器选中的 Pod；
//   - 仅 NamespaceSelector：被选中命名空间内的全部 Pod；
//   - 两者同时存在：被选中命名空间内、再被 Pod 选择器选中的 Pod；
//   - IPBlock 非空：按端点 PodIP 做 CIDR 匹配。
type Peer struct {
	PodSelector       *LabelSelector `json:"pod_selector,omitempty"`
	NamespaceSelector *LabelSelector `json:"namespace_selector,omitempty"`
	IPBlock           *IPBlock       `json:"ip_block,omitempty"`
}

// PortRef 引用一个端口。引用方式有且仅有两种：
//   - 数字端口：Port 非 0（JSON "port": 8080）；
//   - 命名端口：Name 非空（JSON "port": "http"），解析发生在【目标端点】上。
//
// 两者互斥，由 policy.ValidatePortRef 强制。
type PortRef struct {
	Port     int      `json:"-"`
	Name     string   `json:"-"`
	Protocol Protocol `json:"-"`
}

// portRefAlias 用于自定义 JSON 编解码：port 字段既可能是数字也可能是字符串。
type portRefAlias struct {
	Port     json.RawMessage `json:"port,omitempty"`
	Protocol Protocol        `json:"protocol,omitempty"`
}

// UnmarshalJSON 支持 {"port":8080} 与 {"port":"http"} 两种写法。
func (p *PortRef) UnmarshalJSON(data []byte) error {
	var a portRefAlias
	if err := json.Unmarshal(data, &a); err != nil {
		return err
	}
	if len(a.Port) > 0 {
		var n int
		if err := json.Unmarshal(a.Port, &n); err == nil {
			p.Port = n
		} else {
			var s string
			if err2 := json.Unmarshal(a.Port, &s); err2 != nil {
				return fmt.Errorf("port 必须是整数或字符串: %w", err)
			}
			p.Name = s
		}
	}
	p.Protocol = a.Protocol
	return nil
}

// MarshalJSON 保证 PortRef 序列化时保留数字/命名端口的原始形态。
func (p PortRef) MarshalJSON() ([]byte, error) {
	a := portRefAlias{Protocol: p.Protocol}
	if p.Name != "" {
		raw, _ := json.Marshal(p.Name)
		a.Port = raw
	} else if p.Port != 0 {
		raw, _ := json.Marshal(p.Port)
		a.Port = raw
	}
	return json.Marshal(a)
}

// IsNamed 表示该引用是否为命名端口引用。
func (p PortRef) IsNamed() bool { return p.Name != "" }

// Rule 是一条入向或出向规则：Ports 为空表示全部端口，Peers 为空表示全部对端。
type Rule struct {
	Ports []PortRef `json:"ports,omitempty"`
	Peers []Peer    `json:"peers,omitempty"`
}

// Policy 是一条网络策略。PodSelector 为值类型；其 Empty() 表示选中命名空间全部 Pod。
type Policy struct {
	UID         string         `json:"uid"`
	Namespace   string         `json:"namespace"`
	Name        string         `json:"name"`
	PodSelector LabelSelector  `json:"pod_selector"`
	Ingress     []Rule         `json:"ingress,omitempty"`
	Egress      []Rule         `json:"egress,omitempty"`
	PolicyTypes []Direction    `json:"policy_types,omitempty"`
}

// AppliesType 判断策略是否对某方向生效（规则存在时对应方向按 NetworkPolicy 隐式生效）。
func (p Policy) AppliesType(d Direction) bool {
	for _, t := range p.PolicyTypes {
		if t == d {
			return true
		}
	}
	switch d {
	case Ingress:
		return len(p.Ingress) > 0
	case Egress:
		return len(p.Egress) > 0
	}
	return false
}

// Namespace 是命名空间标签载体。
type Namespace struct {
	Name   string `json:"name"`
	Labels Labels `json:"labels,omitempty"`
}

// ContainerPort 是端点声明的容器端口：命名端口解析的唯一依据。
type ContainerPort struct {
	Name     string   `json:"name,omitempty"`
	Port     int      `json:"port"`
	Protocol Protocol `json:"protocol,omitempty"`
}

// Endpoint 是受管工作负载端点，含本次标签快照下的标签与端口声明。
type Endpoint struct {
	UID       string          `json:"uid"`
	Namespace string          `json:"namespace"`
	Name      string          `json:"name"`
	PodIP     string          `json:"pod_ip,omitempty"`
	Labels    Labels          `json:"labels,omitempty"`
	Ports     []ContainerPort `json:"ports,omitempty"`
}

// Snapshot 是一次离线快照：标签/命名空间/端口声明都冻结在 LabelVersion，
// PolicyVersion 标注该快照采集时对应的策略集合版本。
type Snapshot struct {
	LabelVersion string      `json:"label_version"`
	PolicyVersion string      `json:"policy_version"`
	Namespaces   []Namespace `json:"namespaces"`
	Endpoints    []Endpoint  `json:"endpoints"`
}

// PolicySet 是带版本的策略集合。
type PolicySet struct {
	Version  string   `json:"version"`
	Policies []Policy `json:"policies"`
}

// EndpointRef 用命名空间+名称定位一个端点。
type EndpointRef struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

func (r EndpointRef) String() string { return r.Namespace + "/" + r.Name }

// Probe 是一次单点探测。数字端口与命名端口二选一：
// NamedPort 非空时，端口号在【目标端点】的端口声明中解析，绝不当作全局数字。
type Probe struct {
	From      EndpointRef `json:"from"`
	To        EndpointRef `json:"to"`
	Port      int         `json:"port,omitempty"`
	NamedPort string      `json:"named_port,omitempty"`
	Protocol  Protocol    `json:"protocol,omitempty"`
}

// Key 返回探测的稳定标识，用于矩阵结果键控与断言。
func (p Probe) Key() string {
	proto := p.Protocol.EffectiveProtocol()
	if p.NamedPort != "" {
		return fmt.Sprintf("%s->%s:named=%s/%s", p.From, p.To, p.NamedPort, proto)
	}
	return fmt.Sprintf("%s->%s:%d/%s", p.From, p.To, p.Port, proto)
}
