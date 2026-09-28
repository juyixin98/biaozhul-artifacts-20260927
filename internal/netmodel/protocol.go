package netmodel

// IPFlagBits 对应 IPv4 首部 Flags 字段（16 位 Flags+Fragment Offset 高 3 位）。
const (
	// FlagReserved 为保留位（RF/bit 0）。正常报文必须为 0。
	FlagReserved = 0x8000
	// FlagDontFragment 为 DF（bit 1）。
	FlagDontFragment = 0x4000
	// FlagMoreFragments 为 MF（bit 2），置位表示还有后续分片。
	FlagMoreFragments = 0x2000
	// OffsetMask 提取 13 位分片偏移。
	OffsetMask = 0x1FFF
)

// Protocol 是 IPv4 Protocol 字段的命名子集（重组逻辑对任意协议号一视同仁）。
type Protocol uint8

const (
	ProtoICMP Protocol = 1
	ProtoTCP  Protocol = 6
	ProtoUDP  Protocol = 17
)

// String 返回协议名；未知协议号返回数字。
func (p Protocol) String() string {
	switch p {
	case ProtoICMP:
		return "ICMP"
	case ProtoTCP:
		return "TCP"
	case ProtoUDP:
		return "UDP"
	default:
		return "PROTO-" + itoa(int(p))
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [8]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}
