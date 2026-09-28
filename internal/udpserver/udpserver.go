// Package udpserver is the loopback-only datagram transport. It binds a
// UDP socket (never a raw socket, never a production NIC by default),
// decodes nothing itself and hands each datagram to the adapter. Every
// received datagram is logged with the run id, peer and xid so a packet
// capture can be correlated with the state-machine journal.
package udpserver

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"runtime/debug"
	"sync"
	"sync/atomic"

	"dhcp4lab/internal/dhcp4"
)

// Handler processes one raw datagram and returns the raw reply (nil =
// drop/no reply).
type Handler func(ctx context.Context, datagram []byte) []byte

// Listener is the UDP front end.
type Listener struct {
	address string
	handler Handler
	log     *slog.Logger

	runID string
	conn  *net.UDPConn

	datagramsIn  atomic.Uint64
	datagramsOut atomic.Uint64
	parseFails   atomic.Uint64

	closeOnce sync.Once
	closed    chan struct{}
}

// New constructs (but does not bind) a listener.
func New(address string, h Handler, log *slog.Logger) *Listener {
	if log == nil {
		log = slog.Default()
	}
	return &Listener{
		address: address,
		handler: h,
		log:     log,
		runID:   "run-" + randHex(4),
		closed:  make(chan struct{}),
	}
}

func randHex(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// RunID is the per-process correlation identifier used in every log line.
func (l *Listener) RunID() string { return l.runID }

// LocalAddr reports the bound address ("" before Listen).
func (l *Listener) LocalAddr() string {
	if l.conn == nil {
		return ""
	}
	return l.conn.LocalAddr().String()
}

// Stats is a transport-level counter snapshot.
type Stats struct {
	DatagramsIn  uint64
	DatagramsOut uint64
	ParseFails   uint64
}

// Stats returns the current counters.
func (l *Listener) Stats() Stats {
	return Stats{
		DatagramsIn:  l.datagramsIn.Load(),
		DatagramsOut: l.datagramsOut.Load(),
		ParseFails:   l.parseFails.Load(),
	}
}

// Listen binds the socket. It refuses non-loopback binds (the caller
// must have already enforced this in config; this is the second gate).
func (l *Listener) Listen() error {
	ap, err := net.ResolveUDPAddr("udp4", l.address)
	if err != nil {
		return fmt.Errorf("udpserver: resolve %q: %w", l.address, err)
	}
	if !ap.IP.IsLoopback() {
		return fmt.Errorf("udpserver: refusing non-loopback bind %s (lab safety gate)", l.address)
	}
	conn, err := net.ListenUDP("udp4", ap)
	if err != nil {
		return fmt.Errorf("udpserver: bind %s: %w", l.address, err)
	}
	l.conn = conn
	return nil
}

// Serve reads datagrams until ctx is cancelled or the socket errors.
func (l *Listener) Serve(ctx context.Context) error {
	if l.conn == nil {
		return errors.New("udpserver: Listen not called")
	}
	l.log.LogAttrs(ctx, slog.LevelInfo, "udp listener bound",
		slog.String("component", "udpserver"),
		slog.String("run_id", l.runID),
		slog.String("addr", l.LocalAddr()))

	buf := make([]byte, 1500)
	for {
		if ctx.Err() != nil {
			return nil
		}
		n, peer, err := l.conn.ReadFromUDP(buf)
		if err != nil {
			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return fmt.Errorf("udpserver: read: %w", err)
		}
		dg := make([]byte, n)
		copy(dg, buf[:n])
		l.datagramsIn.Add(1)
		go l.process(ctx, dg, peer)
	}
}

func (l *Listener) process(ctx context.Context, dg []byte, peer *net.UDPAddr) {
	// Crash isolation: a bug handling one datagram must never take down
	// the listener process for all other clients. The packet is logged
	// with a stable category and dropped; it is never answered.
	defer func() {
		if r := recover(); r != nil {
			l.parseFails.Add(1)
			l.log.LogAttrs(ctx, slog.LevelError, "handler panic; datagram dropped",
				slog.String("component", "udpserver"),
				slog.String("run_id", l.runID),
				slog.String("peer", peer.String()),
				slog.Int("bytes", len(dg)),
				slog.String("fail_category", "handler_panic"),
				slog.Any("panic", r),
				slog.String("stack", string(debug.Stack())))
		}
	}()
	seq := l.datagramsIn.Load()
	xidPreview := ""
	if len(dg) >= 8 {
		xidPreview = hex.EncodeToString(dg[4:8])
	}

	// Pre-parse purely for log classification; the adapter parses again
	// defensively. Parse failures are dropped with an explicit category,
	// never answered and never reported as success.
	pkt, perr := dhcp4.Unmarshal(dg)
	if perr != nil {
		l.parseFails.Add(1)
		cat, detail := classifyParseError(perr)
		l.log.LogAttrs(ctx, slog.LevelWarn, "malformed datagram dropped",
			slog.String("component", "udpserver"),
			slog.String("run_id", l.runID),
			slog.Uint64("seq", seq),
			slog.String("peer", peer.String()),
			slog.String("xid", xidPreview),
			slog.Int("bytes", len(dg)),
			slog.String("fail_category", cat),
			slog.String("detail", detail))
		return
	}
	mtype, _ := pkt.Type()

	reply := l.handler(ctx, dg)

	attrs := []slog.Attr{
		slog.String("component", "udpserver"),
		slog.String("run_id", l.runID),
		slog.Uint64("seq", seq),
		slog.String("peer", peer.String()),
		slog.String("xid", xidPreview),
		slog.String("recv_type", mtype.String()),
		slog.String("chaddr", macString(pkt.CHAddr)),
	}
	if reply == nil {
		attrs = append(attrs, slog.String("result", "no_reply"))
	} else {
		attrs = append(attrs, slog.String("result", "reply"),
			slog.Int("reply_bytes", len(reply)))
	}
	l.log.LogAttrs(ctx, slog.LevelInfo, "datagram processed", attrs...)

	if reply != nil {
		// Lab transport always unicasts the reply straight back to the
		// peer socket, regardless of the broadcast flag; the flag remains
		// visible in the encoded header and in the decision logs.
		if _, err := l.conn.WriteToUDP(reply, peer); err != nil {
			if ctx.Err() == nil {
				l.log.LogAttrs(ctx, slog.LevelWarn, "reply write failed",
					slog.String("component", "udpserver"),
					slog.String("run_id", l.runID),
					slog.String("peer", peer.String()),
					slog.Any("err", err))
			}
			return
		}
		l.datagramsOut.Add(1)
	}
}

// Close stops the listener.
func (l *Listener) Close() error {
	var err error
	l.closeOnce.Do(func() {
		if l.conn != nil {
			err = l.conn.Close()
		}
		close(l.closed)
	})
	return err
}

// Closed reports whether Close has run.
func (l *Listener) Closed() <-chan struct{} { return l.closed }

func macString(m [6]byte) string {
	return fmt.Sprintf("%02x:%02x:%02x:%02x:%02x:%02x",
		m[0], m[1], m[2], m[3], m[4], m[5])
}

// classifyParseError maps a typed parse error to its stable category.
func classifyParseError(err error) (string, string) {
	var pe *dhcp4.ParseError
	if errors.As(err, &pe) {
		return pe.Code, pe.Reason
	}
	return "parse_error", err.Error()
}
