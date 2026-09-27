package nat_test

import (
	"context"
	"testing"
	"time"

	"natlab/internal/config"
	"natlab/internal/model"
	"natlab/internal/nat"
	"natlab/internal/storage"
)

var epoch = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)

// testCfg is a small, fast configuration: a 4-port pool per protocol and short
// but distinct TCP/UDP timeouts so every scenario fits in seconds.
func testCfg() config.Config {
	c := config.Default()
	c.TCPPortMin, c.TCPPortMax = 20000, 20003
	c.UDPPortMin, c.UDPPortMax = 30000, 30003
	c.TCPSynTimeout = config.Duration{Duration: 5 * time.Second}
	c.TCPEstabTimeout = config.Duration{Duration: 30 * time.Second}
	c.TCPFinTimeout = config.Duration{Duration: 10 * time.Second}
	c.TCPTimeWait = config.Duration{Duration: 5 * time.Second}
	c.UDPTimeout = config.Duration{Duration: 10 * time.Second}
	return c
}

func newEngine(t *testing.T, runID string, cfg config.Config, st storage.Store) *nat.Engine {
	t.Helper()
	if st == nil {
		st = storage.NewMemory()
	}
	eng, err := nat.New(runID, cfg, st)
	if err != nil {
		t.Fatalf("engine: %v", err)
	}
	return eng
}

type pktOpt func(*model.Packet)

func at(sec float64) time.Time { return epoch.Add(time.Duration(sec * float64(time.Second))) }

func out(seq int64, sec float64, proto model.Protocol, srcIP string, srcPort uint16,
	dstIP string, dstPort uint16, opts ...pktOpt) model.Packet {
	p := model.Packet{Seq: seq, ObservedAt: at(sec), Direction: model.Outbound,
		Tuple: model.FiveTuple{SrcIP: srcIP, SrcPort: srcPort, DstIP: dstIP,
			DstPort: dstPort, Proto: proto}}
	for _, o := range opts {
		o(&p)
	}
	return p
}

func in(seq int64, sec float64, proto model.Protocol, srcIP string, srcPort uint16,
	dstIP string, dstPort uint16, opts ...pktOpt) model.Packet {
	p := model.Packet{Seq: seq, ObservedAt: at(sec), Direction: model.Inbound,
		Tuple: model.FiveTuple{SrcIP: srcIP, SrcPort: srcPort, DstIP: dstIP,
			DstPort: dstPort, Proto: proto}}
	for _, o := range opts {
		o(&p)
	}
	return p
}

func flags(syn, ack, fin, rst bool) pktOpt {
	return func(p *model.Packet) { p.TCP = model.TCPFlagBits{SYN: syn, ACK: ack, FIN: fin, RST: rst} }
}

func fragmented() pktOpt { return func(p *model.Packet) { p.Fragmented = true } }

func mustProc(t *testing.T, eng *nat.Engine, p model.Packet) model.Decision {
	t.Helper()
	res, err := eng.Process(context.Background(), p)
	if err != nil {
		t.Fatalf("seq=%d unexpected compute error: %v", p.Seq, err)
	}
	return res.Decision
}
