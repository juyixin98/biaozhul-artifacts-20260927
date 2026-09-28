package netmodel

import (
	"fmt"
	"net/netip"
)

// ParseErrorKind 区分 IPv4 报文解析阶段的具体失败类别。
// 这些错误属于“单片非法”，不污染同一个分组键下已有的重组组。
type ParseErrorKind string

const (
	ErrTruncated        ParseErrorKind = "truncated_packet"
	ErrBadVersion       ParseErrorKind = "bad_ip_version"
	ErrBadIHL           ParseErrorKind = "bad_ihl"
	ErrBadTotalLength   ParseErrorKind = "bad_total_length"
	ErrReservedFlag     ParseErrorKind = "reserved_flag_set"
	ErrBadChecksum      ParseErrorKind = "bad_header_checksum"
	ErrNotIPv4Ethertype ParseErrorKind = "not_ipv4_ethertype"
)

// ParseError 携带具体失败类别与可记录的判定依据。
type ParseError struct {
	Kind   ParseErrorKind
	Detail string
}

func (e *ParseError) Error() string { return string(e.Kind) + ": " + e.Detail }

// FragKey 是 RFC 791 规定的重组分组键：源地址、目的地址、协议、标识。
// 超时是附着在“组实例”上的策略参数，不进入键本身；组终结并被回收后
// 相同键值可以再次复用（见 reasm 包的 ID 复用处理）。
type FragKey struct {
	Src      netip.Addr `json:"src"`
	Dst      netip.Addr `json:"dst"`
	Protocol Protocol   `json:"protocol"`
	ID       uint16     `json:"id"`
}

// String 返回稳定、可排序、可作为存储键的规范表示。
func (k FragKey) String() string {
	return fmt.Sprintf("%s->%s/proto=%d/id=%d", k.Src, k.Dst, uint8(k.Protocol), k.ID)
}

// Packet 是一个成功解析的 IPv4 分片（或不分片数据报）。
type Packet struct {
	Key FragKey

	// IHL 为首部长度（字节）。
	IHL int
	// TotalLength 为 IPv4 Total Length 字段值。
	TotalLength int

	// MoreFragments 为 MF 标志。
	MoreFragments bool
	// FragmentOffset 为以 8 字节为单位的分片偏移字段原值。
	FragmentOffset uint16
	// Payload 即重组意义上的分片数据。
	Payload []byte
}

// IsFragment 报告该报文是否属于分片流量（MF=1 或 Offset>0）。
func (p *Packet) IsFragment() bool { return p.MoreFragments || p.FragmentOffset != 0 }

// ParseIPv4 严格解析一个裸 IPv4 数据报。
// 不允许尾部填充之外的多余字节：Total Length 之后的字节被忽略（链路层填充），
// 但 Total Length 本身不得超过输入长度。
func ParseIPv4(b []byte) (*Packet, error) {
	if len(b) < 20 {
		return nil, &ParseError{Kind: ErrTruncated,
			Detail: fmt.Sprintf("输入长度 %d 小于 IPv4 最小首部 20", len(b))}
	}
	if version := b[0] >> 4; version != 4 {
		return nil, &ParseError{Kind: ErrBadVersion,
			Detail: fmt.Sprintf("IP version=%d，期望 4", version)}
	}
	ihl := int(b[0]&0x0f) * 4
	if ihl < 20 {
		return nil, &ParseError{Kind: ErrBadIHL,
			Detail: fmt.Sprintf("IHL=%d 字节，小于 20", ihl)}
	}
	if len(b) < ihl {
		return nil, &ParseError{Kind: ErrTruncated,
			Detail: fmt.Sprintf("输入长度 %d 小于 IHL 声明的首部长度 %d", len(b), ihl)}
	}

	totalLen := int(b[2])<<8 | int(b[3])
	if totalLen < ihl {
		return nil, &ParseError{Kind: ErrBadTotalLength,
			Detail: fmt.Sprintf("total length=%d 小于首部长度 %d", totalLen, ihl)}
	}
	if len(b) < totalLen {
		return nil, &ParseError{Kind: ErrTruncated,
			Detail: fmt.Sprintf("输入长度 %d 小于 total length=%d", len(b), totalLen)}
	}

	if !VerifyChecksum(b[:ihl]) {
		return nil, &ParseError{Kind: ErrBadChecksum, Detail: "RFC1071 首部校验和不通过"}
	}

	flagsOffset := uint16(b[6])<<8 | uint16(b[7])
	if flagsOffset&FlagReserved != 0 {
		return nil, &ParseError{Kind: ErrReservedFlag,
			Detail: "Flags+Offset 的保留位(RF)被置位，RFC 791 禁止此类报文"}
	}

	src, _ := netip.AddrFromSlice(b[12:16])
	dst, _ := netip.AddrFromSlice(b[16:20])

	return &Packet{
		Key: FragKey{
			Src:      src,
			Dst:      dst,
			Protocol: Protocol(b[9]),
			ID:       uint16(b[4])<<8 | uint16(b[5]),
		},
		IHL:            ihl,
		TotalLength:    totalLen,
		MoreFragments:  flagsOffset&FlagMoreFragments != 0,
		FragmentOffset: flagsOffset & OffsetMask,
		Payload:        b[ihl:totalLen],
	}, nil
}
