package tcpmodel

import (
	"strings"
	"testing"
)

func TestPacketJSONHexRoundtrip(t *testing.T) {
	src := `{"order":1,"src_ip":"10.0.0.1","src_port":1234,"dst_ip":"10.0.0.2","dst_port":80,"seq":4000000000,"syn":true,"payload_hex":"deadbeef"}`
	p, err := ParseCapture([]byte(src))
	if err != nil {
		t.Fatal(err)
	}
	if len(p) != 1 {
		t.Fatalf("got %d packets", len(p))
	}
	if got := p[0].Payload; len(got) != 4 || got[0] != 0xde || got[3] != 0xef {
		t.Fatalf("payload hex decode wrong: %x", got)
	}
	if p[0].Seq != 4000000000 || !p[0].SYN {
		t.Fatal("flags/seq not decoded")
	}
	f, err := p[0].Flow()
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(f.KeyStr, "10.0.0.1:1234") {
		t.Fatalf("flow key wrong: %q", f.KeyStr)
	}
	if got := f.DirectionOf(p[0]); got != DirC2S {
		t.Fatalf("canonical smaller endpoint must be c2s, got %s", got)
	}
}

func TestParseCaptureCommentsAndBlanks(t *testing.T) {
	doc := `# comment
{"order":1,"src_ip":"::1","src_port":1,"dst_ip":"::2","dst_port":2,"seq":1}

   # indented comment
{"order":2,"src_ip":"::1","src_port":1,"dst_ip":"::2","dst_port":2,"seq":2}`
	pkts, err := ParseCapture([]byte(doc))
	if err != nil {
		t.Fatal(err)
	}
	if len(pkts) != 2 {
		t.Fatalf("want 2 packets, got %d", len(pkts))
	}
	if !pkts[0].SrcIP.Is6() {
		t.Fatal("IPv6 parse failed")
	}
}

func TestSegmentLenAccountsForSynFin(t *testing.T) {
	if (Packet{SYN: true}).SegmentLen() != 1 {
		t.Fatal("SYN must consume one sequence number")
	}
	if (Packet{FIN: true, Payload: []byte("abc")}).SegmentLen() != 4 {
		t.Fatal("FIN + 3 bytes must consume four")
	}
	if (Packet{SYN: true, FIN: true, Payload: []byte("a")}).SegmentLen() != 3 {
		t.Fatal("SYN+FIN+1 byte must consume three")
	}
}

func TestBadHexRejected(t *testing.T) {
	doc := `{"order":1,"src_ip":"1.1.1.1","src_port":1,"dst_ip":"2.2.2.2","dst_port":2,"payload_hex":"zz"}`
	if _, err := ParseCapture([]byte(doc)); err == nil {
		t.Fatal("invalid hex must error")
	}
}
