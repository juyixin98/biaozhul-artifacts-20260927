// Package netmodel 定义路由域内的网络模型：规范化前缀、地址族、
// 路由候选与下一跳。这里不包含任何选路或存储逻辑，只负责把外部
// 输入（字符串形式的 CIDR、地址）变成可比较、可作为映射键的内部值。
package netmodel

import (
	"encoding/json"
	"errors"
	"net/netip"
)

// Family 是地址族。IPv4 与 IPv6 全流程隔离：不同族的前缀永远不会
// 进入同一棵前缀树，也不允许互为递归下一跳。
type Family uint8

const (
	AFUnspecified Family = 0
	AFIPv4        Family = 4
	AFIPv6        Family = 6
)

func (f Family) String() string {
	switch f {
	case AFIPv4:
		return "ipv4"
	case AFIPv6:
		return "ipv6"
	default:
		return "unspecified"
	}
}

// ErrInvalidPrefix 表示输入不是合法 CIDR，或掩码长度超出本族上限。
var ErrInvalidPrefix = errors.New("invalid prefix")

// Prefix 是规范化后的无类别前缀：主机位清零、零压缩、地址族可判别。
// 它包装标准库的 netip.Prefix，天然可比较、可作为 map key，
// 且 String() 输出就是规范化形式（如 2001:db8::/32）。
type Prefix struct {
	p netip.Prefix
}

// ParsePrefix 解析 "addr/bits" 形式的 CIDR 并执行规范化：
// 非法输入返回 ErrInvalidPrefix；主机位非零的输入会被静默清零，
// 调用方可通过 returned Prefix.String() 与原文比对，决定是否记录告警。
func ParsePrefix(s string) (Prefix, error) {
	p, err := netip.ParsePrefix(s)
	if err != nil {
		return Prefix{}, ErrInvalidPrefix
	}
	p = p.Masked()
	return Prefix{p: p}, nil
}

// MustPrefix 在确信输入合法时使用（主要是测试夹具），否则 panic。
func MustPrefix(s string) Prefix {
	p, err := ParsePrefix(s)
	if err != nil {
		panic(err)
	}
	return p
}

func (p Prefix) Family() Family {
	if !p.p.IsValid() {
		return AFUnspecified
	}
	if p.p.Addr().Is4() {
		return AFIPv4
	}
	return AFIPv6
}

// Bits 返回掩码长度（0..32 / 0..128）。
func (p Prefix) Bits() int { return p.p.Bits() }

// MaxBits 返回本族掩码长度上限，用于拒绝过长前缀。
func (p Prefix) MaxBits() int {
	if p.Family() == AFIPv4 {
		return 32
	}
	return 128
}

// Addr 返回网络地址（主机位已清零）。
func (p Prefix) Addr() netip.Addr { return p.p.Addr() }

// String 输出规范化文本：主机位清零、IPv6 零压缩、省略前导零。
func (p Prefix) String() string {
	if !p.p.IsValid() {
		return ""
	}
	return p.p.String()
}

// Netip 返回底层标准库值。
func (p Prefix) Netip() netip.Prefix { return p.p }

// MarshalJSON 以规范化字符串输出前缀。
func (p Prefix) MarshalJSON() ([]byte, error) {
	return json.Marshal(p.String())
}

// UnmarshalJSON 从字符串解析并规范化；非法 CIDR 返回 ErrInvalidPrefix。
func (p *Prefix) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	pp, err := ParsePrefix(s)
	if err != nil {
		return err
	}
	*p = pp
	return nil
}

// Contains 判断地址（必须同族）是否落在前缀内。
// 标准库自动处理 4-in-6 映射，这里显式校验同族以免跨族误判。
func (p Prefix) Contains(addr netip.Addr) bool {
	if !p.p.IsValid() || !addr.IsValid() {
		return false
	}
	if p.Family() != FamilyOfAddr(addr) {
		return false
	}
	return p.p.Contains(addr)
}

// IsDefault 判断是否为默认路由（0.0.0.0/0 或 ::/0）。
func (p Prefix) IsDefault() bool {
	return p.p.IsValid() && p.p.Bits() == 0
}

// FamilyOfAddr 返回地址所属地址族，Is4In6 统一按 IPv4 处理。
func FamilyOfAddr(a netip.Addr) Family {
	if !a.IsValid() {
		return AFUnspecified
	}
	if a.Is4() || a.Is4In6() {
		return AFIPv4
	}
	return AFIPv6
}
