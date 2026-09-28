package server

import (
	"context"
	"net/netip"
	"time"

	"dhcpv4lab/internal/dhcppacket"
	"dhcpv4lab/internal/storage"
)

// activePoolIPs returns the set of currently occupied pool addresses.
func (s *Server) activePoolIPs(ctx context.Context) (map[string]*storage.Lease, error) {
	// Pool is bounded by configuration validation (<= 65536); fetch active
	// rows and filter in-process — simple, deterministic, fixture-friendly.
	rows, err := s.store.ListLeases(ctx, int(s.pool.Size()))
	if err != nil {
		return nil, err
	}
	out := make(map[string]*storage.Lease)
	for i := range rows {
		l := rows[i]
		if !l.State.IsActive() {
			continue
		}
		a, perr := netip.ParseAddr(l.IP)
		if perr != nil || !s.pool.Contains(a) {
			continue
		}
		lc := l
		out[l.IP] = &lc
	}
	return out, nil
}

func (s *Server) handleDiscover(ctx context.Context, p *dhcppacket.Packet,
	ident storage.Identity, fp string, src Source, now time.Time) handled {

	active, err := s.activePoolIPs(ctx)
	if err != nil {
		return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
			out: fail(storage.CatInternal, "pool_scan_failed", "%v", err)}
	}

	// Selection preference:
	//  1. client's existing BOUND lease (re-offer same address, no timing
	//     change — OFFER never extends a lease)
	//  2. client's live OFFER (reuse)
	//  3. explicit requested-ip option when free
	//  4. lowest free pool address
	myLease := activeLeaseFor(active, ident.Key)
	var wanted string
	switch {
	case myLease != nil && myLease.State == storage.StateBound:
		wanted = myLease.IP
	case myLease != nil && myLease.State == storage.StateOffered:
		wanted = myLease.IP
	default:
		if rip, ok := p.RequestedIP(); ok && s.pool.Contains(rip) {
			if other, taken := active[rip.String()]; !taken || other.IdentityID == ident.Key {
				wanted = rip.String()
			}
		}
		if wanted == "" {
			wanted = s.lowestFree(active)
		}
	}

	if wanted == "" {
		o := fail(storage.CatPoolFull, "pool_exhausted",
			"no free address in pool %s-%s for xid=%d identity=%s",
			s.cfg.Pool.RangeStart, s.cfg.Pool.RangeEnd, p.XID, ident.Label)
		return handled{ident: ident, msgType: dhcppacket.MsgDiscover, out: o}
	}

	offerExp := now.Add(s.cfg.Lease.OfferTTL.Duration)
	res, err := s.store.ReserveOffer(ctx, storage.ReserveOfferInput{
		Identity:      ident,
		XID:           p.XID,
		IP:            wanted,
		NowNanos:      now.UnixNano(),
		OfferExpNanos: offerExp.UnixNano(),
	})
	if err != nil {
		return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
			out: fail(storage.CatInternal, "reserve_failed", "%v", err)}
	}
	if res.Status == "pool_exhausted" {
		return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
			out: fail(storage.CatPoolFull, "pool_exhausted", "pool empty at commit time")}
	}
	if res.Status == "lost_to_other" {
		// Lost the race for this candidate: pick the next free address once
		// against refreshed occupancy and retry a bounded number of times.
		retryIP := wanted
		for attempt := 0; attempt < 4; attempt++ {
			active2, cerr := s.activePoolIPs(ctx)
			if cerr != nil {
				return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
					out: fail(storage.CatInternal, "pool_rescan_failed", "%v", cerr)}
			}
			cand := s.alternativeFree(active2, ident.Key, retryIP)
			if cand == "" {
				return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
					out: fail(storage.CatPoolFull, "pool_exhausted",
						"address %s taken by another client and no alternative remains", res.ContentionIP)}
			}
			res2, rerr := s.store.ReserveOffer(ctx, storage.ReserveOfferInput{
				Identity: ident, XID: p.XID, IP: cand,
				NowNanos: now.UnixNano(), OfferExpNanos: offerExp.UnixNano(),
			})
			if rerr != nil {
				return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
					out: fail(storage.CatInternal, "reserve_retry_failed", "%v", rerr)}
			}
			if res2.Status == "ok" {
				res = res2
				break
			}
			retryIP = res2.ContentionIP
		}
		if res.Status != "ok" {
			return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
				out: fail(storage.CatContend, "allocation_race_lost",
					"contention for addresses near %s; client should retry DISCOVER", wanted)}
		}
	}

	assigned, _ := netip.ParseAddr(res.IP)
	reply, err := s.buildOffer(p, assigned, offerExp)
	if err != nil {
		return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
			out: fail(storage.CatInternal, "offer_encode_failed", "%v", err)}
	}

	action := "discover_offer"
	result := "ok"
	detail := describeOffer(res, p.XID, ident, offerExp)
	if !res.Created && res.Reused {
		action = "discover_reoffer"
		result = "reused_reservation"
	}
	o := &Outcome{
		Category:   storage.CatOK,
		Action:     action,
		Result:     result,
		Reason:     "offer_reserved",
		Detail:     detail,
		Reply:      reply,
		AssignedIP: assigned,
		OutType:    dhcppacket.MsgOffer,
	}
	// Persist the decision for idempotent replay; on a racing duplicate the
	// loser transparently replays the winner's answer.
	if saved, prior, serr := s.store.SaveTransaction(ctx, storage.TransactionRecord{
		IdentityID: ident.Key, XID: p.XID, Phase: "OFFER", Fingerprint: fp,
		InType: "DISCOVER", OutType: "OFFER", AssignedIP: res.IP,
		Reply: reply, LeaseEndsAt: offerExp.UnixNano(), CreatedAt: now.UnixNano(),
	}); serr != nil {
		return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
			out: fail(storage.CatInternal, "tx_save_failed", "%v", serr)}
	} else if !saved && prior != nil {
		return handled{ident: ident, msgType: dhcppacket.MsgDiscover,
			assignedIP: prior.AssignedIP, out: s.replayOutcome(prior)}
	}
	return handled{ident: ident, msgType: dhcppacket.MsgDiscover, assignedIP: res.IP, out: o}
}

func describeOffer(res *storage.ReserveOfferResult, xid uint32, ident storage.Identity, exp time.Time) string {
	switch {
	case res.ReboundFrom != "":
		return "OFFER retargeted from " + res.ReboundFrom + " to " + res.IP +
			"; reservation (not lease) held until " + exp.Format(time.RFC3339Nano)
	case res.Reused:
		return "OFFER reuses client's existing reservation/lease at " + res.IP +
			" for xid=" + uitoa(xid) + "; lease timers untouched"
	default:
		return "OFFER reserves " + res.IP + " for identity " + ident.Label +
			" until " + exp.Format(time.RFC3339Nano) + " (no lease committed)"
	}
}

func activeLeaseFor(m map[string]*storage.Lease, identity string) *storage.Lease {
	for _, l := range m {
		if l.IdentityID == identity {
			return l
		}
	}
	return nil
}

func (s *Server) lowestFree(active map[string]*storage.Lease) string {
	for _, a := range s.pool.Ascending(int(s.pool.Size())) {
		if _, taken := active[a.String()]; !taken {
			return a.String()
		}
	}
	return ""
}

func (s *Server) alternativeFree(active map[string]*storage.Lease, identity, notEqual string) string {
	for _, a := range s.pool.Ascending(int(s.pool.Size())) {
		cand := a.String()
		if cand == notEqual {
			continue
		}
		if _, taken := active[cand]; !taken {
			return cand
		}
	}
	return ""
}

func (s *Server) buildOffer(req *dhcppacket.Packet, ip netip.Addr, exp time.Time) ([]byte, error) {
	rep := dhcppacket.ReplyFor(req, dhcppacket.MsgOffer)
	rep.YIAddr = ip
	rep.SIAddr = s.serverID
	rep.SetIP(dhcppacket.OptSubnetMask, s.netmask)
	rep.SetIP(dhcppacket.OptServerID, s.serverID)
	rep.SetUInt32(dhcppacket.OptLeaseTime, s.cfg.LeaseSeconds())
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

func uitoa(v uint32) string {
	if v == 0 {
		return "0"
	}
	var buf [12]byte
	i := len(buf)
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	return string(buf[i:])
}
