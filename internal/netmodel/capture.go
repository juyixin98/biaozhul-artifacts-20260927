package netmodel

import (
	"encoding/binary"
	"net/netip"
)

// WritePCap serializes packets into a classic little-endian, microsecond
// pcap stream with Ethernet link layer. It exists so tests and the CLI can
// build synthetic captures without tcpdump or any third-party library; the
// data and expected streams are authored by the test, not derived from the
// reassembler.
func WritePCap(packets []Packet) []byte {
	var out []byte
	// Global header: magic(4) ver(2+2) thiszone(4) sigfigs(4) snaplen(4) link(4)
	hdr := make([]byte, 24)
	copy(hdr[0:4], pcapMagicMicro[:])
	binary.LittleEndian.PutUint32(hdr[4:8], 2<<16|4) // version 2.4
	binary.LittleEndian.PutUint32(hdr[16:20], 65535) // snaplen
	binary.LittleEndian.PutUint32(hdr[20:24], linkEthernet)
	out = append(out, hdr...)

	baseUsec := int64(1_700_000_000) * 1_000_000
	for i, p := range packets {
		frame := ethernetFrame(p)
		rec := make([]byte, 16)
		usec := baseUsec + int64(i)*1000 // deterministic, 1 ms spacing
		binary.LittleEndian.PutUint32(rec[0:4], uint32(usec/1_000_000))
		binary.LittleEndian.PutUint32(rec[4:8], uint32(usec%1_000_000))
		binary.LittleEndian.PutUint32(rec[8:12], uint32(len(frame)))
		binary.LittleEndian.PutUint32(rec[12:16], uint32(len(frame)))
		out = append(out, rec...)
		out = append(out, frame...)
	}
	return out
}

func ethernetFrame(p Packet) []byte {
	srcIP, _ := netip.ParseAddr(p.SrcIP)
	dstIP, _ := netip.ParseAddr(p.DstIP)
	var ipPkt []byte
	if srcIP.Is4() {
		ipPkt = ipv4Packet(p, srcIP, dstIP)
	} else {
		ipPkt = ipv6Packet(p, srcIP, dstIP)
	}
	eth := make([]byte, 14)
	eth[12], eth[13] = 0x08, 0x00
	if !srcIP.Is4() {
		eth[12], eth[13] = 0x86, 0xdd
	}
	return append(eth, ipPkt...)
}

func ipv4Packet(p Packet, src, dst netip.Addr) []byte {
	tcpSeg := tcpSegment(p)
	total := 20 + len(tcpSeg)
	b := make([]byte, 20+len(tcpSeg))
	b[0] = 0x45
	b[1] = 0 // DSCP
	binary.BigEndian.PutUint16(b[2:4], uint16(total))
	b[8] = 64 // TTL
	b[9] = 6  // TCP
	copy(b[12:16], src.AsSlice())
	copy(b[16:20], dst.AsSlice())
	copy(b[20:], tcpSeg)
	ipv4Checksum(b[:20])
	return b
}

func ipv6Packet(p Packet, src, dst netip.Addr) []byte {
	tcpSeg := tcpSegment(p)
	b := make([]byte, 40+len(tcpSeg))
	b[0] = 0x60
	binary.BigEndian.PutUint16(b[4:6], uint16(len(tcpSeg)))
	b[6] = 6
	b[7] = 64
	copy(b[8:24], src.AsSlice())
	copy(b[24:40], dst.AsSlice())
	copy(b[40:], tcpSeg)
	return b
}

func tcpSegment(p Packet) []byte {
	b := make([]byte, 20+len(p.Payload))
	binary.BigEndian.PutUint16(b[0:2], p.SrcPort)
	binary.BigEndian.PutUint16(b[2:4], p.DstPort)
	binary.BigEndian.PutUint32(b[4:8], p.Seq)
	binary.BigEndian.PutUint32(b[8:12], p.Ack)
	b[12] = 5 << 4 // data offset 20
	b[13] = p.FlagMask()
	binary.BigEndian.PutUint16(b[14:16], 65535)
	copy(b[20:], p.Payload)
	return b
}

// ipv4Checksum computes the IPv4 header checksum into hdr[10:12]. The TCP
// checksum is left zero: fixtures are synthetic and the parser never
// validates checksums.
func ipv4Checksum(hdr []byte) {
	var sum uint32
	for i := 0; i+1 < len(hdr); i += 2 {
		sum += uint32(hdr[i])<<8 | uint32(hdr[i+1])
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	cs := ^uint16(sum)
	hdr[10] = byte(cs >> 8)
	hdr[11] = byte(cs)
}
