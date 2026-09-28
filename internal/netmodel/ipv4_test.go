package netmodel_test

import (
	"testing"

	"ipfragreasm/internal/fixture"
	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/testlog"

	"net/netip"
)

func TestParseValidFragmentFields(t *testing.T) {
	log := testlog.New(t, "netmodel/parse-fields")
	payload := make([]byte, 40)
	hdr := fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("192.0.2.10"), Dst: netip.MustParseAddr("198.51.100.7"),
		Protocol: netmodel.ProtoTCP, ID: 0xBEEF, More: true, Offset8: 51,
	}
	raw := fixture.BuildIPv4(payload, hdr)
	p, err := netmodel.ParseIPv4(raw)
	if err != nil {
		t.Fatalf("合法分片解析失败: %v", err)
	}
	if p.FragmentOffset != 51 || !p.MoreFragments {
		t.Fatalf("offset/MF 解析错误: off=%d more=%v", p.FragmentOffset, p.MoreFragments)
	}
	if p.Key.ID != 0xBEEF || p.Key.Protocol != netmodel.ProtoTCP {
		t.Fatalf("分组键字段错误: %+v", p.Key)
	}
	if p.Key.Src != hdr.Src || p.Key.Dst != hdr.Dst {
		t.Fatalf("地址解析错误: %s -> %s", p.Key.Src, p.Key.Dst)
	}
	if len(p.Payload) != 40 || p.TotalLength != 60 {
		t.Fatalf("长度字段错误: payload=%d total=%d", len(p.Payload), p.TotalLength)
	}
	if p.FragmentOffset*8 != 408 {
		t.Fatalf("偏移 8 字节单位换算错误")
	}
	log.Pass("parse-fields", "valid", "offset=51(408 字节), MF=1, 分组键四元组正确",
		map[string]any{"offset_bytes": 408, "total": 60})
}

func TestParseErrorsHaveKinds(t *testing.T) {
	log := testlog.New(t, "netmodel/parse-errors")
	cases := []struct {
		name string
		raw  []byte
		want netmodel.ParseErrorKind
	}{
		{"truncated", make([]byte, 10), netmodel.ErrTruncated},
		{"bad-version", append([]byte{0x60}, make([]byte, 39)...), netmodel.ErrBadVersion},
	}
	for _, tc := range cases {
		_, err := netmodel.ParseIPv4(tc.raw)
		pe, ok := err.(*netmodel.ParseError)
		if !ok {
			t.Fatalf("%s: 错误类型不是 *ParseError: %v", tc.name, err)
		}
		if pe.Kind != tc.want {
			t.Fatalf("%s: kind=%s want=%s", tc.name, pe.Kind, tc.want)
		}
		log.Info("parse-errors", tc.name, "返回具体失败类别 %s", pe.Kind)
	}

	// 坏校验和。
	raw := fixture.BuildIPv4(make([]byte, 8), fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("1.1.1.1"), Dst: netip.MustParseAddr("2.2.2.2"),
		Protocol: netmodel.ProtoUDP, ID: 1, BadChecksum: true,
	})
	if _, err := netmodel.ParseIPv4(raw); err == nil || err.(*netmodel.ParseError).Kind != netmodel.ErrBadChecksum {
		t.Fatalf("坏校验和应被识别, err=%v", err)
	}

	// RF 保留位置位。
	raw = fixture.BuildIPv4(make([]byte, 8), fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("1.1.1.1"), Dst: netip.MustParseAddr("2.2.2.2"),
		Protocol: netmodel.ProtoUDP, ID: 1, SetReserved: true,
	})
	if _, err := netmodel.ParseIPv4(raw); err == nil || err.(*netmodel.ParseError).Kind != netmodel.ErrReservedFlag {
		t.Fatalf("RF 置位应被识别, err=%v", err)
	}

	// IHL 非法（版本/IHL 字节给 0x41，IHL=4 字节）。
	badIHL := fixture.BuildIPv4(make([]byte, 8), fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("1.1.1.1"), Dst: netip.MustParseAddr("2.2.2.2"),
		Protocol: netmodel.ProtoUDP, ID: 1,
	})
	badIHL[0] = 0x41
	if _, err := netmodel.ParseIPv4(badIHL); err == nil || err.(*netmodel.ParseError).Kind != netmodel.ErrBadIHL {
		t.Fatalf("IHL=4 应被识别, err=%v", err)
	}
	log.Pass("parse-errors", "all", "每类畸形报文都返回明确 ParseErrorKind", nil)
}

func TestEthernetAndLoopbackExtraction(t *testing.T) {
	ipv4 := fixture.BuildIPv4(make([]byte, 4), fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.0.0.1"), Dst: netip.MustParseAddr("10.0.0.2"),
		Protocol: netmodel.ProtoUDP, ID: 1,
	})
	eth := fixture.EthernetFrame(ipv4)
	got, err := netmodel.ExtractIPv4(netmodel.LinkTypeEthernet, eth)
	if err != nil || len(got) != len(ipv4) {
		t.Fatalf("以太网解封装失败: %v len=%d", err, len(got))
	}

	// 非 IPv4 ethertype（ARP 0x0806）必须被跳过而非崩溃。
	arp := make([]byte, 64)
	arp[12], arp[13] = 0x08, 0x06
	if _, err := netmodel.ExtractIPv4(netmodel.LinkTypeEthernet, arp); err == nil {
		t.Fatalf("ARP 帧应返回 not_ipv4_ethertype")
	} else if pe, ok := err.(*netmodel.ParseError); !ok || pe.Kind != netmodel.ErrNotIPv4Ethertype {
		t.Fatalf("ARP 帧错误类别错误: %v", err)
	}

	loop := fixture.LoopbackFrame(ipv4)
	got, err = netmodel.ExtractIPv4(netmodel.LinkTypeLoop, loop)
	if err != nil || len(got) != len(ipv4) {
		t.Fatalf("loopback 解封装失败: %v", err)
	}

	if _, err := netmodel.ExtractIPv4(netmodel.LinkTypeRaw, ipv4); err != nil {
		t.Fatalf("raw linktype 应直通: %v", err)
	}
}

func TestChecksumKnownVector(t *testing.T) {
	// 标准 RFC 1071 示例：在首部其他字段正确时，含校验和字段求和应得 0xffff。
	raw := fixture.BuildIPv4([]byte{0, 1, 2, 3, 4, 5, 6, 7}, fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.0.0.1"), Dst: netip.MustParseAddr("10.0.0.2"),
		Protocol: 17, ID: 0x1234,
	})
	if !netmodel.VerifyChecksum(raw[:20]) {
		t.Fatalf("由夹具构造的首部校验和应当通过")
	}
}
