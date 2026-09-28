// Package fixture 生成本地合成测试数据：手工拼 IPv4 首部与分片、写 PCAP 文件。
//
// 该包刻意独立于 netmodel.ParseIPv4 / reasm 重组器，只使用自己的字节级
// 构造逻辑，避免“参考答案由被测实现自身生成”。
package fixture

import (
	"encoding/binary"
	"net/netip"

	"ipfragreasm/internal/netmodel"
)

// IPHeaderOptions 控制一个合成 IPv4 数据报/分片的首部字段。
type IPHeaderOptions struct {
	Src         netip.Addr
	Dst         netip.Addr
	Protocol    netmodel.Protocol
	ID          uint16
	More        bool   // MF
	Offset8     uint16 // 以 8 字节为单位的偏移
	BadChecksum bool   // 为 true 时故意写坏首部校验和
	SetReserved bool   // 为 true 时置 RF 保留位
	IHLBytes    int    // <=0 时取 20（无选项）
}

// BuildIPv4 以原始字节方式构造一个 IPv4 报文。
func BuildIPv4(payload []byte, o IPHeaderOptions) []byte {
	ihl := o.IHLBytes
	if ihl < 20 {
		ihl = 20
	}
	total := ihl + len(payload)
	var flagsOffset uint16
	if o.More {
		flagsOffset |= netmodel.FlagMoreFragments
	}
	if o.SetReserved {
		flagsOffset |= netmodel.FlagReserved
	}
	flagsOffset |= o.Offset8 & netmodel.OffsetMask

	b := make([]byte, total)
	b[0] = 0x40 | byte(ihl/4)
	b[1] = 0 // DSCP/ECN
	binary.BigEndian.PutUint16(b[2:4], uint16(total))
	binary.BigEndian.PutUint16(b[4:6], o.ID)
	binary.BigEndian.PutUint16(b[6:8], flagsOffset)
	b[8] = 64 // TTL
	b[9] = byte(o.Protocol)
	// b[10:12] 校验和稍后计算
	src := o.Src.As4()
	dst := o.Dst.As4()
	copy(b[12:16], src[:])
	copy(b[16:20], dst[:])
	copy(b[ihl:], payload)

	csum := netmodel.Checksum(b[:ihl])
	binary.BigEndian.PutUint16(b[10:12], csum)
	if o.BadChecksum {
		b[10] ^= 0xFF
	}
	return b
}

// SplitPayload 按给定的 8 字节对齐分段大小切分载荷，返回每片的
// (offset8, payload, more)。除末片外每段长度都是 8 的倍数。
func SplitPayload(data []byte, sizes []int) []FragmentSpec {
	var specs []FragmentSpec
	offset := 0
	pos := 0
	for i, size := range sizes {
		last := i == len(sizes)-1
		seg := data[pos : pos+size]
		specs = append(specs, FragmentSpec{
			Offset8: uint16(offset / 8),
			Payload: append([]byte(nil), seg...),
			More:    !last,
		})
		pos += size
		offset += size
	}
	return specs
}

// FragmentSpec 是一个合成分片的构造规格。
type FragmentSpec struct {
	Offset8 uint16
	Payload []byte
	More    bool
}

// BuildFragments 把若干 FragmentSpec 编成 IPv4 分片字节序列。
func BuildFragments(o IPHeaderOptions, specs []FragmentSpec, mutate func(idx int, base IPHeaderOptions) IPHeaderOptions) [][]byte {
	out := make([][]byte, len(specs))
	for i, s := range specs {
		hdr := o
		hdr.Offset8 = s.Offset8
		hdr.More = s.More
		if mutate != nil {
			hdr = mutate(i, hdr)
		}
		out[i] = BuildIPv4(s.Payload, hdr)
	}
	return out
}
