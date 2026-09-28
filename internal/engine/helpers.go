package engine

import (
	"sort"

	"netpolreach/internal/model"
)

// dedupSort 对匹配到的规则来源按 (方向,策略,规则序) 去重并稳定排序，
// 使“策略来源”输出确定，便于离线对比与断言。
func dedupSort(in []Match) []Match {
	if len(in) == 0 {
		return nil
	}
	seen := make(map[Match]struct{}, len(in))
	out := make([]Match, 0, len(in))
	for _, m := range in {
		if _, ok := seen[m]; ok {
			continue
		}
		seen[m] = struct{}{}
		out = append(out, m)
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.Direction != b.Direction {
			return a.Direction < b.Direction
		}
		if a.PolicyUID != b.PolicyUID {
			return a.PolicyUID < b.PolicyUID
		}
		return a.RuleIndex < b.RuleIndex
	})
	return out
}

// EnumerateNumericProbes 针对小端点集合穷举连通矩阵：
// 对每一对 (from,to) 与目标端点声明的每个【数字】容器端口生成探测。
//
// 命名端口与策略中的命名端口通过显式 Probe 列表在测试夹具中提供——
// 命名端口必须逐目标解析，不能用全局数字枚举替代。
// includeSelf 控制是否包含同端点自发自收。
func EnumerateNumericProbes(snap model.Snapshot, includeSelf bool) []model.Probe {
	eps := make([]model.Endpoint, len(snap.Endpoints))
	copy(eps, snap.Endpoints)
	sort.Slice(eps, func(i, j int) bool {
		if eps[i].Namespace != eps[j].Namespace {
			return eps[i].Namespace < eps[j].Namespace
		}
		return eps[i].Name < eps[j].Name
	})

	var probes []model.Probe
	for _, from := range eps {
		for _, to := range eps {
			if !includeSelf && from.Namespace == to.Namespace && from.Name == to.Name {
				continue
			}
			seenPort := make(map[int]model.Protocol)
			var ports []struct {
				p int
				r model.Protocol
			}
			for _, cp := range to.Ports {
				proto := cp.Protocol.EffectiveProtocol()
				// 同一目标多个命名端口可指向同一数字端口；按 (端口,协议) 去重。
				if prev, ok := seenPort[cp.Port]; ok && prev == proto {
					continue
				}
				seenPort[cp.Port] = proto
				ports = append(ports, struct {
					p int
					r model.Protocol
				}{cp.Port, proto})
			}
			for _, pp := range ports {
				probes = append(probes, model.Probe{
					From:     model.EndpointRef{Namespace: from.Namespace, Name: from.Name},
					To:       model.EndpointRef{Namespace: to.Namespace, Name: to.Name},
					Port:     pp.p,
					Protocol: pp.r,
				})
			}
		}
	}
	return probes
}

// Compile 辅助：返回编译快照端点数量（供观测/诊断使用）。
func (c *Compiled) EndpointCount() int { return len(c.endpoint) }

// PolicyCount 返回编译进引擎的策略数量。
func (c *Compiled) PolicyCount() int { return len(c.policies) }
