package server

import (
	"context"
	"fmt"
	"net/netip"
	"time"

	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/storage"
)

// requestKind classifies a REQUEST per RFC 2131 §4.3.6/§4.4.
type requestKind int

const (
	rkSelecting   requestKind = iota // selecting: option 54 present, ciaddr=0
	rkInitReboot                     // INIT-REBOOT: no 54, ciaddr=0, option 50
	rkRenewRebind                    // RENEWING/REBINDING: ciaddr!=0
	rkMalformed
)

func classifyRequest(p *dhcppacket.Packet) (requestKind, string) {
	hasSID := false
	if sid, ok := p.ServerID(); ok {
		hasSID = sid.IsValid() && !sid.IsUnspecified()
	}
	_, hasReq := p.RequestedIP()
	ciSet := p.CIAddr.IsValid() && !p.CIAddr.IsUnspecified()

	switch {
	case hasSID && !ciSet:
		return rkSelecting, "selecting"
	case hasSID && ciSet:
		// RFC 2131: a selecting client MUST set ciaddr=0.
		return rkMalformed, "server_id_and_ciaddr_both_present"
	case ciSet:
		// RENEWING unicast, REBINDING broadcast — both carry ciaddr and no 54.
		return rkRenewRebind, "renew_rebind"
	case hasReq:
		return rkInitReboot, "init_reboot"
	default:
		return rkMalformed, "no_server_id_no_ciaddr_no_requested_ip"
	}
}

func (s *Server) handleRequest(ctx context.Context, p *dhcppacket.Packet,
	ident storage.Identity, fp string, src Source, now time.Time) handled {

	kind, kindLabel := classifyRequest(p)

	switch kind {
	case rkSelecting:
		return s.handleSelecting(ctx, p, ident, fp, kindLabel, now)
	case rkInitReboot:
		return s.handleInitReboot(ctx, p, ident, fp, kindLabel, now)
	case rkRenewRebind:
		return s.handleRenew(ctx, p, ident, fp, kindLabel, now)
	default:
		return s.nakAndPersist(ctx, p, ident, fp, "nak_malformed_request", kindLabel,
			now, netip.Addr{}, "REQUEST framing invalid: %s", kindLabel)
	}
}

func (s *Server) handleSelecting(ctx context.Context, p *dhcppacket.Packet,
	ident storage.Identity, fp, kindLabel string, now time.Time) handled {

	sid, _ := p.ServerID()
	if sid != s.serverID {
		return s.silentDrop("not_selected_server",
			"REQUEST names server-id %s; this server is %s — client selected another server",
			sid, s.serverID)
	}
	reqIP, ok := p.RequestedIP()
	if !ok {
		return s.nakAndPersist(ctx, p, ident, fp, "nak_no_requested_ip", kindLabel,
			now, netip.Addr{}, "selecting REQUEST omitted option 50 (requested IP)")
	}
	if !s.pool.Contains(reqIP) {
		return s.nakAndPersist(ctx, p, ident, fp, "nak_address_outside_pool", kindLabel, now, reqIP,
			"requested %s is outside configured pool %s-%s",
			reqIP, s.cfg.Pool.RangeStart, s.cfg.Pool.RangeEnd)
	}

	lease, err := s.store.ActiveLeaseAtIP(ctx, reqIP.String())
	if err != nil {
		return internalHandled(ident, "lease_lookup_failed", err)
	}
	switch {
	case lease == nil:
		return s.nakAndPersist(ctx, p, ident, fp, "nak_no_valid_offer", kindLabel, now, reqIP,
			"no OFFERED reservation for %s; OFFER may have expired — restart DISCOVER", reqIP)
	case lease.IdentityID != ident.Key:
		return s.nakAndPersist(ctx, p, ident, fp, "nak_address_taken", kindLabel, now, reqIP,
			"address %s is held by another client (%s)", reqIP, lease.IdentityID)
	case lease.State == storage.StateOffered:
		return s.commitLease(ctx, p, ident, fp, kindLabel, reqIP, false, now)
	default: // already BOUND to this client — idempotent re-commit
		return s.commitLease(ctx, p, ident, fp, kindLabel, reqIP, true, now)
	}
}

func (s *Server) handleInitReboot(ctx context.Context, p *dhcppacket.Packet,
	ident storage.Identity, fp, kindLabel string, now time.Time) handled {

	reqIP, ok := p.RequestedIP()
	if !ok {
		return s.nakAndPersist(ctx, p, ident, fp, "nak_no_requested_ip", kindLabel,
			now, netip.Addr{}, "INIT-REBOOT REQUEST omitted option 50")
	}
	if !s.pool.Contains(reqIP) {
		return s.nakAndPersist(ctx, p, ident, fp, "nak_address_outside_pool", kindLabel, now, reqIP,
			"rebooted client requests %s outside pool %s-%s",
			reqIP, s.cfg.Pool.RangeStart, s.cfg.Pool.RangeEnd)
	}

	lease, err := s.store.LeaseAtIP(ctx, reqIP.String())
	if err != nil {
		return internalHandled(ident, "lease_lookup_failed", err)
	}
	if lease == nil || lease.IdentityID != ident.Key {
		return s.nakAndPersist(ctx, p, ident, fp, "nak_unknown_lease_on_reboot", kindLabel, now, reqIP,
			"client rebooted requesting %s but has no recorded lease; restart DISCOVER", reqIP)
	}
	if lease.State != storage.StateBound {
		return s.nakAndPersist(ctx, p, ident, fp, "nak_lease_not_active_on_reboot", kindLabel, now, reqIP,
			"recorded lease for %s is %s, not BOUND; restart DISCOVER", reqIP, lease.State)
	}
	if now.UnixNano() >= lease.Ends {
		return s.nakAndPersist(ctx, p, ident, fp, "nak_expired_on_reboot", kindLabel, now, reqIP,
			"recorded lease for %s ended at %s; restart DISCOVER",
			reqIP, time.Unix(0, lease.Ends).Format(time.RFC3339Nano))
	}
	// Confirmed client, live lease: re-ACK with the ORIGINAL lease boundaries;
	// reboot confirmation does not extend the lease.
	reply, err := s.buildAck(p, reqIP, time.Unix(0, lease.Starts), time.Unix(0, lease.Ends))
	if err != nil {
		return internalHandled(ident, "ack_encode_failed", err)
	}
	o := &Outcome{
		Category: storage.CatOK, Action: "request_ack_reboot", Result: "ok",
		Reason: "reboot_confirmed_existing_lease",
		Detail: "INIT-REBOOT confirmed lease for " + reqIP.String() +
			" with original ends=" + time.Unix(0, lease.Ends).Format(time.RFC3339Nano) + " (not extended)",
		Reply: reply, AssignedIP: reqIP, OutType: dhcppacket.MsgAck,
	}
	return persistReply(ctx, s, p, ident, fp, "REBOOT", reqIP.String(), reply, lease.Ends, now, o)
}

func (s *Server) handleRenew(ctx context.Context, p *dhcppacket.Packet,
	ident storage.Identity, fp, kindLabel string, now time.Time) handled {

	ci := p.CIAddr.Unmap()
	if !s.pool.Contains(ci) {
		return s.nakAndPersist(ctx, p, ident, fp, "nak_address_outside_pool", kindLabel, now, ci,
			"renewal ciaddr %s is outside pool %s-%s",
			ci, s.cfg.Pool.RangeStart, s.cfg.Pool.RangeEnd)
	}
	lease, err := s.store.ActiveLeaseAtIP(ctx, ci.String())
	if err != nil {
		return internalHandled(ident, "lease_lookup_failed", err)
	}
	switch {
	case lease == nil:
		return s.nakAndPersist(ctx, p, ident, fp, "nak_not_renewable", kindLabel, now, ci,
			"no active lease at %s; restart DISCOVER", ci)
	case lease.IdentityID != ident.Key:
		return s.nakAndPersist(ctx, p, ident, fp, "nak_not_owner_renew", kindLabel, now, ci,
			"lease at %s belongs to another client (%s)", ci, lease.IdentityID)
	case lease.State != storage.StateBound:
		return s.nakAndPersist(ctx, p, ident, fp, "nak_offer_not_committed", kindLabel, now, ci,
			"%s is only OFFERED, never committed; SELECT first", ci)
	case now.UnixNano() >= lease.Ends:
		return s.nakAndPersist(ctx, p, ident, fp, "nak_lease_expired", kindLabel, now, ci,
			"lease at %s expired at %s; restart DISCOVER",
			ci, time.Unix(0, lease.Ends).Format(time.RFC3339Nano))
	}
	return s.commitLease(ctx, p, ident, fp, kindLabel, ci, true, now)
}

// commitLease performs the atomic OFFERED->BOUND flip (or renewal extension)
// and builds/stores the ACK. Any lost race becomes a NAK.
func (s *Server) commitLease(ctx context.Context, p *dhcppacket.Packet, ident storage.Identity,
	fp, kindLabel string, ip netip.Addr, renew bool, now time.Time) handled {

	start := now
	end := now.Add(s.cfg.Lease.LeaseTime.Duration)
	in := storage.CommitAckInput{
		Identity: ident, XID: p.XID, IP: ip.String(),
		NowNanos: now.UnixNano(), StartNanos: start.UnixNano(), EndNanos: end.UnixNano(),
		RenewExisting: renew,
	}
	res, err := s.store.CommitAck(ctx, in)
	if err != nil {
		return internalHandled(ident, "commit_failed", err)
	}
	switch res.Status {
	case "lost_to_other":
		return s.nakAndPersist(ctx, p, ident, fp, "nak_address_taken", kindLabel, now, ip,
			"address %s was taken by another client before COMMIT", ip)
	case "not_renewable":
		return s.nakAndPersist(ctx, p, ident, fp, "nak_not_renewable", kindLabel, now, ip,
			"lease for %s is no longer renewable", ip)
	}

	reply, err := s.buildAck(p, ip, start, end)
	if err != nil {
		return internalHandled(ident, "ack_encode_failed", err)
	}
	action, reason, detail := "request_ack", "lease_committed",
		"lease committed for "+ip.String()+" until "+end.Format(time.RFC3339Nano)
	if renew {
		action = "request_ack_renew"
		reason = "lease_renewed"
		detail = "renewal extends " + ip.String() + " to " + end.Format(time.RFC3339Nano) +
			" (renew_count=" + fmt.Sprint(res.Lease.RenewCount) + ")"
	}
	o := &Outcome{
		Category: storage.CatOK, Action: action, Result: "ok", Reason: reason,
		Detail: detail, Reply: reply, AssignedIP: ip, OutType: dhcppacket.MsgAck,
	}
	phase := "COMMIT"
	if renew {
		phase = "RENEW"
	}
	return persistReply(ctx, s, p, ident, fp, phase, ip.String(), reply, end.UnixNano(), now, o)
}

func (s *Server) buildAck(req *dhcppacket.Packet, ip netip.Addr, start, end time.Time) ([]byte, error) {
	rep := dhcppacket.ReplyFor(req, dhcppacket.MsgAck)
	rep.YIAddr = ip
	rep.SIAddr = s.serverID
	rep.SetIP(dhcppacket.OptSubnetMask, s.netmask)
	rep.SetIP(dhcppacket.OptServerID, s.serverID)
	leaseSecs := uint32(end.Sub(start).Seconds())
	if leaseSecs == 0 {
		leaseSecs = s.cfg.LeaseSeconds()
	}
	rep.SetUInt32(dhcppacket.OptLeaseTime, leaseSecs)
	rep.SetUInt32(dhcppacket.OptRenewalTime, s.cfg.T1Seconds())
	rep.SetUInt32(dhcppacket.OptRebindingTime, s.cfg.T2Seconds())
	if s.router.IsValid() {
		rep.SetIP(dhcppacket.OptRouter, s.router)
	}
	if len(s.dns) > 0 {
		rep.SetIPList(dhcppacket.OptDNSServer, s.dns)
	}
	return rep.Encode()
}

func (s *Server) buildNak(req *dhcppacket.Packet, message string) ([]byte, error) {
	rep := dhcppacket.ReplyFor(req, dhcppacket.MsgNak)
	// NAK carries yiaddr=0 and only server-id plus a text explanation (RFC
	// 2131 §4.3.6: non-config parameters should not be included).
	rep.SetIP(dhcppacket.OptServerID, s.serverID)
	rep.Options[dhcppacket.OptMessage] = []byte(message)
	return rep.Encode()
}

func (s *Server) nakAndPersist(ctx context.Context, p *dhcppacket.Packet, ident storage.Identity,
	fp, reason, kindLabel string, now time.Time, ip netip.Addr, detailFmt string, args ...any) handled {

	rep, err := s.buildNak(p, reason)
	if err != nil {
		return internalHandled(ident, "nak_encode_failed", err)
	}
	o := &Outcome{
		Category: storage.CatNAK, Action: "request_nak", Result: "nak",
		Reason: reason,
		Detail: "NAK (" + kindLabel + "): " + fmt.Sprintf(detailFmt, args...),
		Reply:  rep, OutType: dhcppacket.MsgNak, AssignedIP: ip,
	}
	return persistReply(ctx, s, p, ident, fp, "NAK", ip.String(), rep, 0, now, o)
}

func (s *Server) silentDrop(reason, detailFmt string, args ...any) handled {
	return handled{
		msgType: dhcppacket.MsgRequest,
		out: &Outcome{
			Category: storage.CatNoReply, Action: "silent_ignore", Result: "no_reply",
			Reason: reason, Detail: fmt.Sprintf(detailFmt, args...),
		},
	}
}

func internalHandled(ident storage.Identity, reason string, err error) handled {
	return handled{
		msgType: dhcppacket.MsgRequest,
		out:     fail(storage.CatInternal, reason, "%v", err),
	}
}

// persistReply stores the decision row and returns the handled result. A
// racing duplicate transparently becomes the stored replay.
func persistReply(ctx context.Context, s *Server, p *dhcppacket.Packet, ident storage.Identity,
	fp, phase, assignedIP string, reply []byte, endsAt int64, now time.Time, o *Outcome) handled {

	outName := "ACK"
	if o.OutType == dhcppacket.MsgNak {
		outName = "NAK"
	}
	saved, prior, err := s.store.SaveTransaction(ctx, storage.TransactionRecord{
		IdentityID: ident.Key, XID: p.XID, Phase: phase, Fingerprint: fp,
		InType: "REQUEST", OutType: outName, AssignedIP: assignedIP,
		Reply: reply, LeaseEndsAt: endsAt, CreatedAt: now.UnixNano(),
	})
	if err != nil {
		return handled{msgType: dhcppacket.MsgRequest,
			out: fail(storage.CatInternal, "tx_save_failed", "%v", err)}
	}
	if !saved && prior != nil {
		return handled{msgType: dhcppacket.MsgRequest,
			assignedIP: prior.AssignedIP, out: s.replayOutcome(prior)}
	}
	return handled{msgType: dhcppacket.MsgRequest, assignedIP: assignedIP, out: o}
}
