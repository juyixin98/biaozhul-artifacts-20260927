package pcap_test

import (
	"bytes"
	"encoding/binary"
	"net/netip"
	"testing"
	"time"

	"ipreasm/internal/ipv4"
	"ipreasm/internal/pcap"
)

// TestRoundTrip: records written through Writer parse back byte-identical
// with their timestamps preserved.
func TestRoundTrip(t *testing.T) {
	var buf bytes.Buffer
	w, err := pcap.NewWriter(&buf, pcap.LinkEthernet)
	if err != nil {
		t.Fatal(err)
	}
	ts := time.Date(2026, 9, 27, 12, 0, 0, 123456000, time.UTC)
	ip, err := ipv4.MarshalFragment(
		netip.MustParseAddr("10.0.0.1"), netip.MustParseAddr("10.0.0.2"),
		17, 0x1234, 2, true, 64, bytes.Repeat([]byte{0xAB}, 16))
	if err != nil {
		t.Fatal(err)
	}
	eth := make([]byte, 14+len(ip))
	binary.BigEndian.PutUint16(eth[12:14], 0x0800)
	copy(eth[14:], ip)
	if err := w.WriteRecord(ts, eth); err != nil {
		t.Fatal(err)
	}

	r, err := pcap.NewReader(&buf)
	if err != nil {
		t.Fatal(err)
	}
	if r.LinkType != pcap.LinkEthernet {
		t.Fatalf("link type=%d", r.LinkType)
	}
	rec, err := r.Next()
	if err != nil {
		t.Fatal(err)
	}
	if !rec.TS.Equal(ts) {
		t.Fatalf("ts=%s want %s", rec.TS, ts)
	}
	pkt, err := pcap.ExtractIPv4(r.LinkType, rec.Data)
	if err != nil {
		t.Fatal(err)
	}
	if pkt.ID != 0x1234 || pkt.FragOffsetUnits != 2 || !pkt.MoreFragments {
		t.Fatalf("parsed flags/id wrong: id=0x%04x off=%d more=%v", pkt.ID, pkt.FragOffsetUnits, pkt.MoreFragments)
	}
	if !bytes.Equal(pkt.Payload, bytes.Repeat([]byte{0xAB}, 16)) {
		t.Fatalf("payload mismatch")
	}
	if _, err := r.Next(); err == nil {
		t.Fatal("expected EOF after one record")
	}
	t.Logf("pcap round-trip ok: ts preserved, id=0x1234, off_units=2, mf=true, payload=16B")
}

// TestRawLinkType: raw-IP captures parse without an ethernet header.
func TestRawLinkType(t *testing.T) {
	var buf bytes.Buffer
	w, err := pcap.NewWriter(&buf, pcap.LinkRaw)
	if err != nil {
		t.Fatal(err)
	}
	ip, _ := ipv4.MarshalFragment(
		netip.MustParseAddr("192.168.1.1"), netip.MustParseAddr("192.168.1.2"),
		6, 0x0001, 0, false, 64, []byte("hello"))
	if err := w.WriteRecord(time.Unix(100, 0), ip); err != nil {
		t.Fatal(err)
	}
	r, err := pcap.NewReader(&buf)
	if err != nil {
		t.Fatal(err)
	}
	rec, err := r.Next()
	if err != nil {
		t.Fatal(err)
	}
	pkt, err := pcap.ExtractIPv4(r.LinkType, rec.Data)
	if err != nil {
		t.Fatal(err)
	}
	if pkt.Fragmented() {
		t.Fatal("unfragmented packet reported as fragmented")
	}
	if string(pkt.Payload) != "hello" {
		t.Fatalf("payload=%q", pkt.Payload)
	}
	t.Logf("raw-ip parse ok: fragmented=false, payload=%q", pkt.Payload)
}

// TestBadMagic rejects a non-pcap stream with an explicit error rather
// than success.
func TestBadMagic(t *testing.T) {
	if _, err := pcap.NewReader(bytes.NewReader(make([]byte, 24))); err == nil {
		t.Fatal("expected ErrBadMagic for zero bytes")
	} else {
		t.Logf("non-pcap input correctly rejected: %v", err)
	}
}

// TestNonIPFrame: an ARP-like ethertype must be reported as no-IPv4.
func TestNonIPFrame(t *testing.T) {
	frame := make([]byte, 64)
	binary.BigEndian.PutUint16(frame[12:14], 0x0806) // ARP
	if _, err := pcap.ExtractIPv4(pcap.LinkEthernet, frame); err == nil {
		t.Fatal("expected non-IPv4 error")
	} else {
		t.Logf("non-IPv4 ethertype correctly reported: %v", err)
	}
}

// TestChecksumValid: generated headers pass an independent checksum
// verification, proving the model layer is not emitting garbage.
func TestChecksumValid(t *testing.T) {
	b, err := ipv4.MarshalFragment(
		netip.MustParseAddr("1.2.3.4"), netip.MustParseAddr("5.6.7.8"),
		17, 42, 0, true, 64, make([]byte, 32))
	if err != nil {
		t.Fatal(err)
	}
	var sum uint32
	for i := 0; i < 20; i += 2 {
		sum += uint32(binary.BigEndian.Uint16(b[i : i+2]))
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	if uint16(sum) != 0xffff {
		t.Fatalf("header checksum invalid: 0x%04x", sum)
	}
}
