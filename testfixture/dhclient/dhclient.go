// Package dhclient is a synthetic DHCPv4 client for the lab tests. It
// speaks raw UDP on loopback using the independent wirekit reference
// codec (never the server's own codec), keeps per-xid correlation and
// exposes explicit failure categories instead of generic errors.
package dhclient

import (
	"errors"
	"fmt"
	"net"
	"sync"
	"time"

	"dhcp4lab/testfixture/wirekit"
)

// Failure is a classified client-side outcome.
type Failure struct {
	Category string
	Detail   string
}

func (f *Failure) Error() string { return f.Category + ": " + f.Detail }

// Stable failure categories.
const (
	FailTimeout      = "client_timeout"
	FailParse        = "client_bad_reply"
	FailXIDMismatch  = "client_xid_mismatch"
	FailTypeMismatch = "client_reply_type_mismatch"
	FailSocket       = "client_socket_error"
)

// Client is one synthetic client with a stable identity.
type Client struct {
	// ID is a human correlation label (e.g. "alpha").
	ID     string
	HWAddr [6]byte
	// ClientIDOpt is raw option 61 (type+value); nil uses chaddr identity.
	ClientIDOpt []byte

	server *net.UDPAddr
	conn   *net.UDPConn

	mu sync.Mutex
}

// New binds a client ephemeral socket aimed at the loopback server.
func New(id, serverAddr, mac string, clientIDOpt []byte) (*Client, error) {
	srv, err := net.ResolveUDPAddr("udp4", serverAddr)
	if err != nil {
		return nil, &Failure{FailSocket, err.Error()}
	}
	conn, err := net.DialUDP("udp4", nil, srv)
	if err != nil {
		return nil, &Failure{FailSocket, err.Error()}
	}
	c := &Client{
		ID: id, HWAddr: wirekit.MAC(mac), ClientIDOpt: clientIDOpt,
		server: srv, conn: conn,
	}
	return c, nil
}

// Close releases the socket.
func (c *Client) Close() error { return c.conn.Close() }

// LocalPort is the client's source UDP port.
func (c *Client) LocalPort() int { return c.conn.LocalAddr().(*net.UDPAddr).Port }

// XID derives a deterministic transaction id from a seed (keeps logs
// reproducible while differing across logical transactions).
func XID(seed uint32) [4]byte {
	return [4]byte{byte(seed >> 24), byte(seed >> 16), byte(seed >> 8), byte(seed)}
}

// Send transmits one prebuilt datagram.
func (c *Client) Send(b []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := c.conn.Write(b); err != nil {
		return &Failure{FailSocket, err.Error()}
	}
	return nil
}

// SendRaw sends arbitrary bytes (malformed-input tests).
func (c *Client) SendRaw(b []byte) error { return c.Send(b) }

// Exchange sends a datagram and waits for a reply with matching xid.
// wantType=0 accepts any reply; noReplyWait>0 means "expect silence" and
// returns (nil, nil) if nothing arrives in that window.
func (c *Client) Exchange(b []byte, wantType byte, wait time.Duration) (*wirekit.Packet, []byte, error) {
	if err := c.Send(b); err != nil {
		return nil, nil, err
	}
	return c.Recv(wantType, b[wirekit.OffXID:wirekit.OffXID+4], wait)
}

// Recv waits for one reply matching the given xid.
func (c *Client) Recv(wantType byte, xid []byte, wait time.Duration) (*wirekit.Packet, []byte, error) {
	_ = c.conn.SetReadDeadline(time.Now().Add(wait))
	buf := make([]byte, 1500)
	for {
		n, err := c.conn.Read(buf)
		if err != nil {
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() {
				return nil, nil, &Failure{FailTimeout,
					fmt.Sprintf("no reply within %s for xid %x", wait, xid)}
			}
			return nil, nil, &Failure{FailSocket, err.Error()}
		}
		dg := make([]byte, n)
		copy(dg, buf[:n])
		pkt, perr := wirekit.Parse(dg)
		if perr != nil {
			return nil, dg, &Failure{FailParse, perr.Error()}
		}
		if string(pkt.XID[:]) != string(xid) {
			// Not ours; keep waiting inside the deadline.
			continue
		}
		if wantType != 0 {
			if mt, ok := pkt.MsgType(); !ok || mt != wantType {
				got := "?"
				if ok {
					got = wirekit.MsgTypeName(mt)
				}
				return pkt, dg, &Failure{FailTypeMismatch,
					fmt.Sprintf("want %s, got %s", wirekit.MsgTypeName(wantType), got)}
			}
		}
		return pkt, dg, nil
	}
}

// AssertSilence sends and verifies nothing arrives within wait.
func (c *Client) AssertSilence(b []byte, wait time.Duration) error {
	if err := c.Send(b); err != nil {
		return err
	}
	_ = c.conn.SetReadDeadline(time.Now().Add(wait))
	buf := make([]byte, 1500)
	n, err := c.conn.Read(buf)
	if err == nil {
		pkt, _ := wirekit.Parse(buf[:n])
		got := "unparseable"
		if pkt != nil {
			if mt, ok := pkt.MsgType(); ok {
				got = wirekit.MsgTypeName(mt)
			}
		}
		return &Failure{Category: "client_unexpected_reply",
			Detail: fmt.Sprintf("expected silence, got %d bytes (%s)", n, got)}
	}
	var ne net.Error
	if errors.As(err, &ne) && ne.Timeout() {
		return nil
	}
	return &Failure{FailSocket, err.Error()}
}

// --- High-level message constructors (independent reference behavior) ---

// Discover builds a DISCOVER.
func (c *Client) Discover(xid [4]byte) []byte {
	b := wirekit.NewRequest(xid, c.HWAddr).MsgType(wirekit.MTDiscover)
	if c.ClientIDOpt != nil {
		b.ClientID(c.ClientIDOpt)
	}
	return b.ParamRequest(wirekit.OptSubnetMask, wirekit.OptRouter, wirekit.OptDNS, wirekit.OptLeaseTime).Build()
}

// RequestSelect builds a SELECTING REQUEST (options 50 + 54).
func (c *Client) RequestSelect(xid [4]byte, requestedIP, serverID [4]byte) []byte {
	b := wirekit.NewRequest(xid, c.HWAddr).MsgType(wirekit.MTRequest).
		RequestedIP(requestedIP).ServerID(serverID)
	if c.ClientIDOpt != nil {
		b.ClientID(c.ClientIDOpt)
	}
	return b.Build()
}

// RequestReboot builds an INIT-REBOOT REQUEST (option 50, no 54, ciaddr 0).
func (c *Client) RequestReboot(xid [4]byte, requestedIP [4]byte) []byte {
	b := wirekit.NewRequest(xid, c.HWAddr).MsgType(wirekit.MTRequest).
		RequestedIP(requestedIP)
	if c.ClientIDOpt != nil {
		b.ClientID(c.ClientIDOpt)
	}
	return b.Build()
}

// RequestRenew builds a RENEWING REQUEST (ciaddr set, no 50/54).
func (c *Client) RequestRenew(xid [4]byte, ciaddr [4]byte) []byte {
	b := wirekit.NewRequest(xid, c.HWAddr).MsgType(wirekit.MTRequest).CIAddr(ciaddr)
	if c.ClientIDOpt != nil {
		b.ClientID(c.ClientIDOpt)
	}
	return b.Build()
}

// Release builds a RELEASE (ciaddr set, type 7).
func (c *Client) Release(xid [4]byte, ciaddr [4]byte) []byte {
	b := wirekit.NewRequest(xid, c.HWAddr).MsgType(wirekit.MTRelease).CIAddr(ciaddr)
	if c.ClientIDOpt != nil {
		b.ClientID(c.ClientIDOpt)
	}
	return b.Build()
}

// RawBuilder exposes the underlying builder for adversarial tests.
func (c *Client) RawBuilder(xid [4]byte) *wirekit.Builder {
	b := wirekit.NewRequest(xid, c.HWAddr)
	if c.ClientIDOpt != nil {
		b.ClientID(c.ClientIDOpt)
	}
	return b
}
