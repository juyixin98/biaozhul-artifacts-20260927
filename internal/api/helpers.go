package api

import "net/netip"

// parseNetipAddr 解析目标地址，4-in-6 统一映射回 IPv4 以维持族隔离。
func parseNetipAddr(s string) (netip.Addr, error) {
	a, err := netip.ParseAddr(s)
	if err != nil {
		return netip.Addr{}, err
	}
	return a.Unmap(), nil
}

// netipAddr 仅为让 server.go 的签名更短，不增加抽象。
type netipAddr = netip.Addr
