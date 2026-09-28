// Package testsupport builds synthetic, fully deterministic TCP captures for
// tests and the golden-fixture generator. It depends only on the protocol
// value types in netmodel (packet representation, pcap serialization) — never
// on the reassembly engine. The expected ("known") streams are authored byte
// constants in the scenario table; slicing those constants is the only way
// expected outputs are derived, so answers are not produced by the code under
// test.
package testsupport

import (
	"fmt"

	"tcpreplay/internal/netmodel"
)

// Canonical endpoints of every synthetic connection. 10.0.0.1 < 10.0.0.2, so
// the client is canonical endpoint A and client->server is direction a_to_b.
const (
	ClientIP   = "10.0.0.1"
	ServerIP   = "10.0.0.2"
	ClientPort = 40001
	ServerPort = 80
)

// FromClient / FromServer select a direction.
const (
	FromClient = "client"
	FromServer = "server"
)

// Builder assembles an arrival-ordered packet list. Sequence arithmetic uses
// the configured ISNs: data byte at stream offset o from the client uses raw
// seq cISN+1+o (the +1 is the SYN), exactly mirroring RFC 9293 rather than
// the engine's bookkeeping.
type Builder struct {
	packets []netmodel.Packet
	cISN    uint32
	sISN    uint32
	cSeqNo  int // record counter for client evidence ids
	sSeqNo  int
}

// NewBuilder creates a builder with explicit ISNs.
func NewBuilder(cISN, sISN uint32) *Builder {
	return &Builder{cISN: cISN, sISN: sISN}
}

// Packets returns the list assembled so far, in arrival order.
func (b *Builder) Packets() []netmodel.Packet { return b.packets }

// ClientSYN emits the opening SYN (seq=cISN, no ACK).
func (b *Builder) ClientSYN() *Builder {
	b.packets = append(b.packets, netmodel.Packet{
		SrcIP: ClientIP, SrcPort: ClientPort, DstIP: ServerIP, DstPort: ServerPort,
		Flags: []string{"SYN"}, Seq: b.cISN, RecordID: b.cid(),
	})
	return b
}

// ServerSYNACK emits SYN+ACK with ack=cISN+1.
func (b *Builder) ServerSYNACK() *Builder {
	b.packets = append(b.packets, netmodel.Packet{
		SrcIP: ServerIP, SrcPort: ServerPort, DstIP: ClientIP, DstPort: ClientPort,
		Flags: []string{"SYN", "ACK"}, Seq: b.sISN, Ack: b.cISN + 1, RecordID: b.sid(),
	})
	return b
}

// ClientHandshakeACK emits the final ACK of the three-way handshake.
func (b *Builder) ClientHandshakeACK() *Builder {
	b.packets = append(b.packets, netmodel.Packet{
		SrcIP: ClientIP, SrcPort: ClientPort, DstIP: ServerIP, DstPort: ServerPort,
		Flags: []string{"ACK"}, Seq: b.cISN + 1, Ack: b.sISN + 1, RecordID: b.cid(),
	})
	return b
}

// Handshake is a convenience for the complete three-way handshake.
func (b *Builder) Handshake() *Builder {
	return b.ClientSYN().ServerSYNACK().ClientHandshakeACK()
}

// ClientData emits a data segment carrying chunk at stream offset off
// (offset 0 = first application byte). The raw seq is cISN+1+off computed with
// 32-bit wrap, authored independently of the engine.
func (b *Builder) ClientData(off int64, chunk []byte) *Builder {
	return b.data(FromClient, off, chunk, b.cid())
}

// ClientDataID is ClientData with a caller-chosen record id (evidence naming).
func (b *Builder) ClientDataID(off int64, chunk []byte, recordID string) *Builder {
	return b.data(FromClient, off, chunk, recordID)
}

// ServerData emits a server data segment at stream offset off.
func (b *Builder) ServerData(off int64, chunk []byte) *Builder {
	return b.data(FromServer, off, chunk, b.sid())
}

// ServerDataID is ServerData with a caller-chosen record id.
func (b *Builder) ServerDataID(off int64, chunk []byte, recordID string) *Builder {
	return b.data(FromServer, off, chunk, recordID)
}

func (b *Builder) data(from string, off int64, chunk []byte, recordID string) *Builder {
	isn := b.cISN
	srcIP := ClientIP
	var srcPort uint16 = ClientPort
	dstIP := ServerIP
	var dstPort uint16 = ServerPort
	if from == FromServer {
		isn = b.sISN
		srcIP = ServerIP
		srcPort = ServerPort
		dstIP = ClientIP
		dstPort = ClientPort
	}
	// SYN consumes one sequence number; uint32 arithmetic performs the wrap.
	seq := netmodel.SeqAdd(isn, 1+off)
	b.packets = append(b.packets, netmodel.Packet{
		SrcIP: srcIP, SrcPort: srcPort, DstIP: dstIP, DstPort: dstPort,
		Flags: []string{"ACK", "PSH"}, Seq: seq, Payload: append([]byte(nil), chunk...),
		RecordID: recordID,
	})
	return b
}

// ClientFIN emits the client FIN at stream offset off; data (possibly empty)
// precedes the FIN in the same segment, and the FIN consumes one more seq.
func (b *Builder) ClientFIN(off int64, data []byte) *Builder {
	return b.fin(FromClient, off, data)
}

// ServerFIN emits the server FIN analogously.
func (b *Builder) ServerFIN(off int64, data []byte) *Builder {
	return b.fin(FromServer, off, data)
}

func (b *Builder) fin(from string, off int64, data []byte) *Builder {
	isn := b.cISN
	srcIP := ClientIP
	var srcPort uint16 = ClientPort
	dstIP := ServerIP
	var dstPort uint16 = ServerPort
	rid := b.cid()
	if from == FromServer {
		isn = b.sISN
		srcIP = ServerIP
		srcPort = ServerPort
		dstIP = ClientIP
		dstPort = ClientPort
		rid = b.sid()
	}
	seq := netmodel.SeqAdd(isn, 1+off)
	b.packets = append(b.packets, netmodel.Packet{
		SrcIP: srcIP, SrcPort: srcPort, DstIP: dstIP, DstPort: dstPort,
		Flags: []string{"ACK", "FIN"}, Seq: seq, Payload: append([]byte(nil), data...),
		RecordID: rid,
	})
	return b
}

// RawSegment appends an arbitrary segment without touching builder state. It
// is used for retransmissions, conflicting overlaps, anchorless captures and
// post-RST probes. data may be nil.
func (b *Builder) RawSegment(from string, rawSeq uint32, flags []string, data []byte, recordID string) *Builder {
	srcIP := ClientIP
	var srcPort uint16 = ClientPort
	dstIP := ServerIP
	var dstPort uint16 = ServerPort
	if from == FromServer {
		srcIP = ServerIP
		srcPort = ServerPort
		dstIP = ClientIP
		dstPort = ClientPort
	}
	if recordID == "" {
		recordID = b.cid()
		if from == FromServer {
			recordID = b.sid()
		}
	}
	b.packets = append(b.packets, netmodel.Packet{
		SrcIP: srcIP, SrcPort: srcPort, DstIP: dstIP, DstPort: dstPort,
		Flags: append([]string(nil), flags...), Seq: rawSeq,
		Payload: append([]byte(nil), data...), RecordID: recordID,
	})
	return b
}

// Reorder swaps two arrival positions (0-based).
func (b *Builder) Reorder(i, j int) *Builder {
	b.packets[i], b.packets[j] = b.packets[j], b.packets[i]
	return b
}

// PCap serializes the assembled packets to a classic pcap stream.
func (b *Builder) PCap() []byte { return netmodel.WritePCap(b.packets) }

func (b *Builder) cid() string {
	b.cSeqNo++
	return fmt.Sprintf("c-%03d", b.cSeqNo)
}

func (b *Builder) sid() string {
	b.sSeqNo++
	return fmt.Sprintf("s-%03d", b.sSeqNo)
}

// ClientISN / ServerISN expose the authored initial sequence numbers.
func (b *Builder) ClientISN() uint32 { return b.cISN }
func (b *Builder) ServerISN() uint32 { return b.sISN }
