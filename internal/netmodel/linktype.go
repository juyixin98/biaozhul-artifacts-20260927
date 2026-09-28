package netmodel

import "fmt"

// 合成夹具与离线 PCAP 回放共同支持的 libpcap 链路类型子集。
// 数值来自 https://www.tcpdump.org/linktypes.html（LINKTYPE_*）。
const (
	LinkTypeEthernet = 1   // LINKTYPE_ETHERNET
	LinkTypeRaw      = 101 // LINKTYPE_RAW（裸 IPv4）
	LinkTypeLoop     = 108 // LINKTYPE_RAW_LOOP / Linux cooked 变体之外的 BSD loopback
	LinkTypeIPv4Raw2 = 228 // LINKTYPE_IPV4（部分工具写出的裸 IPv4）
)

// ExtractIPv4 依据 pcap 链路层类型把一帧剥离到裸 IPv4 数据报。
// 该函数绝不触碰真实网络，只处理夹具/文件中的字节。
func ExtractIPv4(linkType uint32, frame []byte) ([]byte, error) {
	switch linkType {
	case LinkTypeRaw, LinkTypeIPv4Raw2:
		return frame, nil

	case LinkTypeEthernet:
		if len(frame) < 14 {
			return nil, fmt.Errorf("ethernet: 帧长 %d 小于 14", len(frame))
		}
		ethertype := uint16(frame[12])<<8 | uint16(frame[13])
		payload := frame[14:]

		// 支持一层 802.1Q VLAN 标签（0x8100），测试夹具可显式覆盖。
		if ethertype == 0x8100 {
			if len(payload) < 4 {
				return nil, fmt.Errorf("ethernet: 802.1Q 标签被截断")
			}
			ethertype = uint16(payload[2])<<8 | uint16(payload[3])
			payload = payload[4:]
		}
		if ethertype != 0x0800 {
			return nil, &ParseError{Kind: ErrNotIPv4Ethertype,
				Detail: fmt.Sprintf("ethertype=0x%04x，非 IPv4(0x0800)，按跳过处理", ethertype)}
		}
		return payload, nil

	case LinkTypeLoop:
		// BSD loopback：4 字节地址族，IPv4 为 2（主机字节序，夹具统一按 LE 写 02 00 00 00）。
		if len(frame) < 4 {
			return nil, fmt.Errorf("loopback: 帧长 %d 小于 4", len(frame))
		}
		family := uint32(frame[0]) | uint32(frame[1])<<8 | uint32(frame[2])<<16 | uint32(frame[3])<<24
		if family != 2 {
			return nil, &ParseError{Kind: ErrNotIPv4Ethertype,
				Detail: fmt.Sprintf("loopback family=%d，非 IPv4(2)，按跳过处理", family)}
		}
		return frame[4:], nil

	default:
		return nil, fmt.Errorf("不支持的 pcap linktype=%d（夹具仅生成 1/101/108/228）", linkType)
	}
}
