// Package transport wires the state machine to a raw loopback UDP socket.
//
// The socket is an ordinary UDP server on a high loopback port (default
// 127.0.0.1:10067), NOT ports 67/68 and never a non-loopback interface unless
// the operator explicitly overrides config. This keeps all traffic inside the
// machine and makes concurrent fixtures possible (each test run picks its own
// port).
package transport

import (
	"context"
	"encoding/json"
	"log/slog"
	"net"
	"time"

	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/server"
	"dhcpv4lab/internal/storage"
)

// UDP serves DHCP datagrams from one UDPConn.
type UDP struct {
	conn   *net.UDPConn
	srv    *server.Server
	log    *slog.Logger
	runID  string
	readTO time.Duration
}

// ListenUDP binds addr (must be loopback per config validation) and wraps it.
func ListenUDP(addr string, srv *server.Server, log *slog.Logger, runID string, readTimeout time.Duration) (*UDP, error) {
	a, err := net.ResolveUDPAddr("udp4", addr)
	if err != nil {
		return nil, err
	}
	conn, err := net.ListenUDP("udp4", a)
	if err != nil {
		return nil, err
	}
	return &UDP{conn: conn, srv: srv, log: log, runID: runID, readTO: readTimeout}, nil
}

// LocalAddr reports the bound address (useful for port-zero tests).
func (u *UDP) LocalAddr() *net.UDPAddr { return u.conn.LocalAddr().(*net.UDPAddr) }

// Close shuts the socket.
func (u *UDP) Close() error { return u.conn.Close() }

// Serve reads datagrams until ctx is canceled or a fatal read error occurs.
func (u *UDP) Serve(ctx context.Context) error {
	buf := make([]byte, 1500)
	for {
		if u.readTO > 0 {
			_ = u.conn.SetReadDeadline(time.Now().Add(u.readTO))
		}
		n, src, err := u.conn.ReadFromUDP(buf)
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			// closed connection during shutdown
			if isClosedErr(err) {
				return nil
			}
			u.log.Error("udp read failed", "runId", u.runID, "error", err)
			return err
		}
		raw := make([]byte, n)
		copy(raw, buf[:n])
		go u.dispatch(ctx, raw, src)
	}
}

func isClosedErr(err error) bool {
	return err != nil && (contains(err.Error(), "use of closed network connection"))
}

func contains(s, sub string) bool {
	if len(sub) == 0 {
		return true
	}
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

func (u *UDP) dispatch(ctx context.Context, raw []byte, src *net.UDPAddr) {
	p, derr := dhcppacket.Decode(raw)
	srcDesc := src.String()
	if derr != nil {
		o := &server.Outcome{
			Category: storage.CatMalformed,
			Action:   "decode_failed",
			Result:   string(storage.CatMalformed),
			Reason:   "malformed_packet",
			Detail:   derr.Error(),
		}
		u.logDecision(nil, srcDesc, o, raw)
		// Malformed datagrams get no reply (RFC: silently discard undecodable
		// frames); the failure is recorded, never returned as success.
		return
	}
	o := u.srv.Handle(ctx, p, server.Source{RemoteAddr: srcDesc, RunID: u.runID})
	u.logDecision(p, srcDesc, o, raw)
	if o != nil && len(o.Reply) > 0 {
		// Lab transport: always unicast the reply to the datagram's source
		// address/port. Broadcast flag is retained for protocol fidelity but
		// does not change loopback delivery.
		if _, err := u.conn.WriteToUDP(o.Reply, src); err != nil {
			u.log.Error("reply write failed", "runId", u.runID,
				"xid", p.XID, "src", srcDesc, "error", err)
		}
	}
}

func (u *UDP) logDecision(p *dhcppacket.Packet, src string, o *server.Outcome, raw []byte) {
	args := []any{
		"runId", u.runID,
		"src", src,
		"category", string(o.Category),
		"action", o.Action,
		"result", o.Result,
		"reason", o.Reason,
		"detail", o.Detail,
		"dup", o.Duplicate,
		"inputBytes", len(raw),
	}
	if p != nil {
		args = append(args, "xid", p.XID)
	}
	if o.AssignedIP.IsValid() {
		args = append(args, "assignedIp", o.AssignedIP.String())
	}
	if o.Category == storage.CatOK {
		u.log.Info("dhcp decision", args...)
	} else {
		// NAK / no-reply / malformed: still structured, marked as warn so test
		// logs visibly separate rejections from success.
		u.log.Warn("dhcp decision", args...)
	}
	// Emit a JSON line on a debug channel consumed by the verification scripts
	// when DHCPV4LAB_JSON_LOG=1.
	if jsonLogEnabled() {
		line, _ := json.Marshal(struct {
			RunID      string `json:"runId"`
			Src        string `json:"src"`
			XID        uint32 `json:"xid,omitempty"`
			Category   string `json:"category"`
			Action     string `json:"action"`
			Result     string `json:"result"`
			Reason     string `json:"reason"`
			Detail     string `json:"detail"`
			AssignedIP string `json:"assignedIp,omitempty"`
			Duplicate  bool   `json:"duplicate"`
			InputBytes int    `json:"inputBytes"`
		}{u.runID, src, xidOf(p), string(o.Category), o.Action, o.Result, o.Reason,
			o.Detail, addrOf(o), o.Duplicate, len(raw)})
		u.log.LogAttrs(context.Background(), slog.LevelInfo, string(line))
	}
}

func xidOf(p *dhcppacket.Packet) uint32 {
	if p == nil {
		return 0
	}
	return p.XID
}

func addrOf(o *server.Outcome) string {
	if o != nil && o.AssignedIP.IsValid() {
		return o.AssignedIP.String()
	}
	return ""
}
