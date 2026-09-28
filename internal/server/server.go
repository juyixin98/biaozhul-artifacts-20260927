// Package server is the protocol adapter: it classifies parsed DHCPv4
// datagrams, drives the storage state machine and encodes wire replies.
// It contains no socket of its own so the same logic serves the UDP
// listener, the HTTP replay API and unit tests.
package server

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/netip"
	"time"

	"dhcp4lab/internal/config"
	"dhcp4lab/internal/dhcp4"
	"dhcp4lab/internal/storage"
)

// Decision is the adapter result: the exact action, the bytes to put on
// the wire (nil = do not reply), and classification for logging.
type Decision struct {
	Outcome    *storage.Outcome
	ReplyBytes []byte
	// ReplyTo describes delivery intent for the transport.
	ReplyTo ReplyTarget
}

// ReplyTarget records where the reply must go. Loopback fixtures always
// use unicast to the sender; the broadcast flag is preserved for the wire
// header and reported here for diagnostics.
type ReplyTarget struct {
	Broadcast bool
}

// Server adapts packets to storage calls.
type Server struct {
	store  *storage.Store
	log    *slog.Logger
	params ReplyParams
}

// ReplyParams are the configuration values copied into replies.
type ReplyParams struct {
	ServerID  netip.Addr
	Netmask   netip.Addr
	Router    netip.Addr
	DNS       []netip.Addr
	LeaseTime time.Duration
}

// New builds the adapter.
func New(st *storage.Store, p ReplyParams, log *slog.Logger) (*Server, error) {
	if st == nil {
		return nil, errors.New("server: nil store")
	}
	if !p.ServerID.Is4() {
		return nil, errors.New("server: server id must be IPv4")
	}
	if log == nil {
		log = slog.Default()
	}
	return &Server{store: st, log: log, params: p}, nil
}

// Store exposes the state store (transports need it for the admin API).
func (s *Server) Store() *storage.Store { return s.store }

// ParamsFromConfig extracts reply parameters from validated config.
func ParamsFromConfig(c config.Config) (ReplyParams, error) {
	p := ReplyParams{LeaseTime: c.LeaseTime.Duration}
	var err error
	if p.ServerID, err = parse4(c.ServerID); err != nil {
		return p, err
	}
	if p.Netmask, err = parse4(c.Netmask); err != nil {
		return p, err
	}
	if c.Router != "" {
		if p.Router, err = parse4(c.Router); err != nil {
			return p, err
		}
	}
	for _, d := range c.DNS {
		a, err := parse4(d)
		if err != nil {
			return p, err
		}
		p.DNS = append(p.DNS, a)
	}
	return p, nil
}

func parse4(s string) (netip.Addr, error) {
	a, err := netip.ParseAddr(s)
	if err != nil || !a.Is4() {
		return netip.Addr{}, fmt.Errorf("invalid IPv4 %q", s)
	}
	return a, nil
}

// ClassifyRequest decides which RFC 2131 situation a REQUEST represents.
// It is exported so tests can assert the classification independently of
// storage behaviour.
func ClassifyRequest(p *dhcp4.Packet) (storage.RequestKind, error) {
	_, has54 := p.Options[dhcp4.OptServerID]
	_, has50 := p.Options[dhcp4.OptRequestedIP]
	ci := p.CIAddr.IsValid() && !p.CIAddr.IsUnspecified()

	switch {
	case has54:
		// SELECTING. ciaddr must be 0 and option 50 present.
		if !has50 {
			return "", fmt.Errorf("%w: selecting REQUEST without option 50", ErrMalformedRequest)
		}
		if ci {
			return "", fmt.Errorf("%w: selecting REQUEST with non-zero ciaddr %s",
				ErrMalformedRequest, p.CIAddr)
		}
		return storage.ReqSelecting, nil
	case has50:
		// INIT-REBOOT: client verifying its remembered configuration.
		if ci {
			return "", fmt.Errorf("%w: init-reboot REQUEST with non-zero ciaddr %s",
				ErrMalformedRequest, p.CIAddr)
		}
		return storage.ReqInitReboot, nil
	case ci:
		// RENEWING/REBINDING.
		return storage.ReqRenew, nil
	default:
		return "", fmt.Errorf("%w: REQUEST without option 54/50 and with ciaddr=0 "+
			"(no selectable situation)", ErrMalformedRequest)
	}
}

// ErrMalformedRequest marks a structurally invalid REQUEST.
var ErrMalformedRequest = errors.New("malformed_request")

// Handle processes one parsed request packet and returns the decision.
// A Decision with nil ReplyBytes means "do not answer"; transport errors
// are the only error return.
func (s *Server) Handle(ctx context.Context, p *dhcp4.Packet) (*Decision, error) {
	msgType, ok := p.Type()
	if !ok {
		// Unmarshal already rejects this, but defend the boundary.
		return &Decision{Outcome: &storage.Outcome{Action: storage.ActDrop,
			Reason: storage.ReasonMalformed}}, nil
	}

	dec := &Decision{ReplyTo: ReplyTarget{Broadcast: p.Broadcast}}
	switch msgType {
	case dhcp4.MsgDiscover:
		out, err := s.store.Discover(ctx, storage.DiscoverInput{
			XID: p.XID, Identity: dhcp4.IdentityOf(p),
			CHAddr: p.CHAddr, ClientID: p.ClientID(),
		})
		if err != nil {
			return nil, err
		}
		dec.Outcome = out
		if out.Reply != nil {
			dec.ReplyBytes = s.encodeOffer(out.Reply, p)
		}
		return dec, nil

	case dhcp4.MsgRequest:
		kind, err := ClassifyRequest(p)
		if err != nil {
			// Malformed REQUEST: NAK so the client returns to INIT rather
			// than retrying the same bad shape forever.
			out := &storage.Outcome{
				Action: storage.ActNAK,
				Reply: &storage.ReplySpec{
					Type: dhcp4.MsgNAK, XID: p.XID, CHAddr: p.CHAddr,
					ClientIDOpt: append([]byte(nil), p.ClientID()...),
				},
				Reason: storage.ReasonMalformed,
			}
			dec.Outcome = out
			dec.ReplyBytes = s.encodeNAK(out.Reply, p)
			return dec, nil
		}
		in := storage.RequestInput{
			XID: p.XID, Kind: kind, Identity: dhcp4.IdentityOf(p),
			CHAddr: p.CHAddr, ClientID: p.ClientID(),
			CIAddr: p.CIAddr,
		}
		if a, ok := p.OptionIPv4(dhcp4.OptServerID); ok {
			in.ServerID = a
		}
		if a, ok := p.OptionIPv4(dhcp4.OptRequestedIP); ok {
			in.RequestedIP = a
		}
		out, err := s.store.Request(ctx, in)
		if err != nil {
			return nil, err
		}
		dec.Outcome = out
		if out.Reply == nil {
			return dec, nil
		}
		switch out.Reply.Type {
		case dhcp4.MsgACK:
			dec.ReplyBytes = s.encodeACK(out.Reply, p)
		case dhcp4.MsgNAK:
			dec.ReplyBytes = s.encodeNAK(out.Reply, p)
		}
		return dec, nil

	case dhcp4.MsgRelease:
		// RFC 2131: a RELEASE names the address being relinquished in
		// ciaddr. A zero ciaddr is malformed; drop it silently with a
		// classified reason rather than touching the store.
		if !p.CIAddr.IsValid() || p.CIAddr.IsUnspecified() {
			dec.Outcome = &storage.Outcome{
				Action: storage.ActDrop,
				Reason: storage.ReasonMalformed + ": release_without_ciaddr",
			}
			return dec, nil
		}
		out, err := s.store.Release(ctx, storage.ReleaseInput{
			XID: p.XID, Identity: dhcp4.IdentityOf(p),
			CHAddr: p.CHAddr, ClientID: p.ClientID(), CIAddr: p.CIAddr,
		})
		if err != nil {
			return nil, err
		}
		dec.Outcome = out
		return dec, nil

	default:
		// Unreachable for parsed messages (Unmarshal restricts the
		// subset); never answer unknown types.
		return &Decision{Outcome: &storage.Outcome{
			Action: storage.ActDrop, Reason: storage.ReasonMalformed}}, nil
	}
}

func (s *Server) baseReply(spec *storage.ReplySpec, req *dhcp4.Packet, mt dhcp4.MessageType) *dhcp4.Packet {
	p := &dhcp4.Packet{
		Op:        dhcp4.OpBootReply,
		HType:     1,
		HLen:      6,
		XID:       spec.XID,
		Broadcast: req.Broadcast,
		YIAddr:    spec.YIAddr,
		SIAddr:    s.params.ServerID,
		GIAddr:    req.GIAddr,
		CHAddr:    spec.CHAddr,
		Options:   map[dhcp4.OptionCode][]byte{},
	}
	p.Options[dhcp4.OptMessageType] = []byte{byte(mt)}
	sid := dhcp4.Addr4(s.params.ServerID)
	p.Options[dhcp4.OptServerID] = sid[:]
	if cid := spec.ClientIDOpt; len(cid) >= 2 {
		p.Options[dhcp4.OptClientID] = append([]byte(nil), cid...)
	}
	return p
}

func (s *Server) encodeOffer(spec *storage.ReplySpec, req *dhcp4.Packet) []byte {
	p := s.baseReply(spec, req, dhcp4.MsgOffer)
	s.addConfigOptions(p, spec)
	b, err := p.Marshal()
	if err != nil {
		s.log.Error("offer marshal failed", "err", err)
		return nil
	}
	return b
}

func (s *Server) encodeACK(spec *storage.ReplySpec, req *dhcp4.Packet) []byte {
	p := s.baseReply(spec, req, dhcp4.MsgACK)
	// Unicasted renew/rebind replies echo ciaddr per RFC 2131 §4.3.2.
	if req.CIAddr.IsValid() && req.CIAddr.Is4() {
		p.CIAddr = req.CIAddr
	}
	s.addConfigOptions(p, spec)
	b, err := p.Marshal()
	if err != nil {
		s.log.Error("ack marshal failed", "err", err)
		return nil
	}
	return b
}

func (s *Server) encodeNAK(spec *storage.ReplySpec, req *dhcp4.Packet) []byte {
	// NAK carries no parameters and yiaddr=0 (RFC 2131 §4.3.2).
	spec.YIAddr = netip.Addr{}
	p := s.baseReply(spec, req, dhcp4.MsgNAK)
	b, err := p.Marshal()
	if err != nil {
		s.log.Error("nak marshal failed", "err", err)
		return nil
	}
	return b
}

func (s *Server) addConfigOptions(p *dhcp4.Packet, spec *storage.ReplySpec) {
	lease := spec.LeaseSeconds
	if lease == 0 {
		lease = uint32(s.params.LeaseTime / time.Second)
	}
	p.Options[dhcp4.OptLeaseTime] = []byte{
		byte(lease >> 24), byte(lease >> 16), byte(lease >> 8), byte(lease)}
	if s.params.Netmask.IsValid() {
		b := dhcp4.Addr4(s.params.Netmask)
		p.Options[dhcp4.OptSubnetMask] = b[:]
	}
	if s.params.Router.IsValid() {
		b := dhcp4.Addr4(s.params.Router)
		p.Options[dhcp4.OptRouter] = b[:]
	}
	if len(s.params.DNS) > 0 {
		v := make([]byte, 0, 4*len(s.params.DNS))
		for _, d := range s.params.DNS {
			b := dhcp4.Addr4(d)
			v = append(v, b[:]...)
		}
		p.Options[dhcp4.OptDNSServer] = v
	}
}
