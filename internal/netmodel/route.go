package netmodel

import (
	"errors"
	"net/netip"
)

// NHKind 是下一跳类型。
type NHKind string

const (
	// NHAddress 普通递归下一跳：必须经同表解析到出接口才能转发。
	NHAddress NHKind = "address"
	// NHConnected 直连下一跳：终止解析，报文从指定出接口发出。
	NHConnected NHKind = "connected"
	// NHBlackhole 黑洞：终止解析，丢弃报文（管理性拒绝）。
	NHBlackhole NHKind = "blackhole"
	// NHUnreachable 不可达：终止解析，ICMP unreachable 语义。
	NHUnreachable NHKind = "unreachable"
)

// Nexthop 是一条路由的下一跳。Kind 决定 Address/Iface 哪些字段有效。
// Address 用指针：黑洞等终局路由没有地址，nil 才能在 JSON 中省略，
// 也避免零值 netip.Addr 无法 MarshalText 的问题。
type Nexthop struct {
	Kind    NHKind      `json:"kind"`
	Address *netip.Addr `json:"address,omitempty"`
	Iface   string      `json:"interface,omitempty"`
}

// HasAddress 报告是否携带有效地址。
func (n Nexthop) HasAddress() bool { return n.Address != nil && n.Address.IsValid() }

// Route 是一条路由候选。同一前缀下可以有多条候选，
// 由选择策略（掩码长度优先，其次管理距离……）决定哪条胜出。
type Route struct {
	ID      string  `json:"id"`
	Prefix  Prefix  `json:"prefix"`
	Nexthop Nexthop `json:"nexthop"`
	// AdminDistance 管理距离：越小越优先（直连通常 0，静态 1，BGP 20…）。
	AdminDistance int `json:"admin_distance"`
	// Metric 协议内度量：同管理距离时越小越优先。
	Metric int `json:"metric"`
	// Protocol 来源协议名，仅在其余键全部相等时参与决胜（固定先后）。
	Protocol string `json:"protocol,omitempty"`
}

// 校验阶段的可分类错误。HTTP 层据此映射状态码与失败类别。
var (
	ErrEmptyID          = errors.New("route id must be non-empty")
	ErrBadNexthopKind   = errors.New("unsupported nexthop kind")
	ErrNHAddrRequired   = nexthopError("address nexthop requires a valid address")
	ErrNHIfaceRequired  = nexthopError("connected nexthop requires an interface")
	ErrNHAddrNotAllowed = nexthopError("terminating nexthop must not carry an address")
	ErrAFMismatch       = nexthopError("nexthop address family does not match prefix family")
	ErrBadDistance      = errors.New("admin_distance must be in [0,255]")
	ErrBadMetric        = errors.New("metric must be in [0,65535]")
)

type nexthopError string

func (e nexthopError) Error() string { return string(e) }

// Validate 检查一条路由在写入前是否自洽。跨族下一跳在这里被拒绝，
// 保证“地址族隔离”在入口处就成立。
func (r Route) Validate() error {
	if r.ID == "" {
		return ErrEmptyID
	}
	if !r.Prefix.Netip().IsValid() {
		return ErrInvalidPrefix
	}
	if r.AdminDistance < 0 || r.AdminDistance > 255 {
		return ErrBadDistance
	}
	if r.Metric < 0 || r.Metric > 65535 {
		return ErrBadMetric
	}
	nh := r.Nexthop
	switch nh.Kind {
	case NHAddress:
		if !nh.HasAddress() {
			return ErrNHAddrRequired
		}
		if FamilyOfAddr(*nh.Address) != r.Prefix.Family() {
			return ErrAFMismatch
		}
	case NHConnected:
		if nh.Iface == "" {
			return ErrNHIfaceRequired
		}
		if nh.HasAddress() && FamilyOfAddr(*nh.Address) != r.Prefix.Family() {
			return ErrAFMismatch
		}
	case NHBlackhole, NHUnreachable:
		// 黑洞/不可达不携带地址；若携带则属配置歧义，拒绝。
		if nh.HasAddress() {
			return ErrNHAddrNotAllowed
		}
	default:
		return ErrBadNexthopKind
	}
	return nil
}
