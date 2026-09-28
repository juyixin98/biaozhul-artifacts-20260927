// Package version 保存构造版本信息，供健康检查、测试日志与 CLI 共用。
package version

// Version 为离线 PCAP IPv4 分片重组后端的语义化版本。
const Version = "0.1.0"

// Algorithm 标识重组策略，便于在日志/报告中关联算法假设。
const Algorithm = "rfc791-rfc815-strict-no-overlap"
