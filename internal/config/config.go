// Package config defines the server configuration, its JSON configuration
// layer and full validation. Nothing here touches a real interface: the
// listener address must be a loopback high port unless explicitly allowed.
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/netip"
	"strings"
	"time"
)

// Duration is a time.Duration that can be configured either as a human
// string ("90s", "2m") or as a JSON number meaning whole seconds (matching
// common DHCP configuration conventions where lease time is numeric).
type Duration struct {
	time.Duration
}

// UnmarshalJSON accepts a JSON string ("90s") or a JSON number of seconds.
func (d *Duration) UnmarshalJSON(raw []byte) error {
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		v, err := time.ParseDuration(s)
		if err != nil {
			return fmt.Errorf("duration %q: %w", s, err)
		}
		if v <= 0 {
			return fmt.Errorf("duration %q must be > 0", s)
		}
		d.Duration = v
		return nil
	}
	var n float64
	if err := json.Unmarshal(raw, &n); err != nil {
		return fmt.Errorf("duration must be a string or number of seconds, got %s", string(raw))
	}
	if n <= 0 {
		return fmt.Errorf("duration seconds must be > 0, got %v", n)
	}
	d.Duration = time.Duration(n * float64(time.Second))
	return nil
}

// MarshalJSON renders the duration in seconds, matching the numeric form
// accepted on input (so the effective config can be round-tripped).
func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.Seconds())
}

// Config is the full server configuration.
type Config struct {
	// ListenUDP is the local address the DHCPv4 listener binds to.
	// Defaults to 127.0.0.1:10067; binding to a non-loopback address or
	// the privileged port 67 requires AllowNonLoopback: true.
	ListenUDP string `json:"listen_udp"`

	// AdminHTTP is the loopback diagnostics/replay HTTP listener.
	AdminHTTP string `json:"admin_http"`

	// AllowNonLoopback must be set to bind anything other than a loopback
	// address. It exists so a typo cannot make the lab server answer real
	// network traffic on a production NIC.
	AllowNonLoopback bool `json:"allow_non_loopback"`

	// ServerID is option 54, the DHCP server identifier (a single IPv4).
	ServerID string `json:"server_id"`

	// Network is the IPv4 subnet served, e.g. "192.0.2.0/24". All offered
	// addresses and every requested/renewed address must belong to it.
	Network string `json:"network"`

	// PoolStart/PoolEnd bound the allocatable address interval (inclusive).
	PoolStart string `json:"pool_start"`
	PoolEnd   string `json:"pool_end"`

	// Netmask is option 1, Router is option 3, DNS is option 6.
	Netmask string   `json:"netmask"`
	Router  string   `json:"router"`
	DNS     []string `json:"dns"`

	// LeaseTime is option 51 in OFFER/ACK replies.
	LeaseTime Duration `json:"lease_time"`

	// OfferTTL bounds how long an OFFER reservation is held.
	OfferTTL Duration `json:"offer_ttl"`

	// SweepInterval is how often the background reaper expires offers
	// and leases.
	SweepInterval Duration `json:"sweep_interval"`

	// SQLite DSN or plain file path. Empty means in-memory ("file::memory:").
	Database string `json:"database"`

	// AckInitRebootUnknown controls the (non-RFC-default) behaviour of
	// answering INIT-REBOOT requests for clients this server has never
	// seen. RFC 2131 says to stay silent; leaving this false preserves
	// that. It exists only to make the boundary explicit and testable.
	AckInitRebootUnknown bool `json:"ack_init_reboot_unknown"`
}

// Default returns a runnable loopback-only lab configuration.
func Default() Config {
	return Config{
		ListenUDP:     "127.0.0.1:10067",
		AdminHTTP:     "127.0.0.1:18067",
		ServerID:      "192.0.2.1",
		Network:       "192.0.2.0/24",
		PoolStart:     "192.0.2.10",
		PoolEnd:       "192.0.2.250",
		Netmask:       "255.255.255.0",
		Router:        "192.0.2.1",
		DNS:           []string{"192.0.2.53"},
		LeaseTime:     Duration{2 * time.Minute},
		OfferTTL:      Duration{30 * time.Second},
		SweepInterval: Duration{time.Second},
	}
}

// Parse decodes JSON configuration, applies defaults for zero-valued
// fields and validates the result.
func Parse(raw []byte) (Config, error) {
	cfg := Default()
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		return Config{}, fmt.Errorf("config json: %w", err)
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// Validate checks every field and returns the first problem found.
func (c Config) Validate() error {
	var errs []string
	add := func(format string, a ...any) { errs = append(errs, fmt.Sprintf(format, a...)) }

	sid, err := parseIPv4(c.ServerID)
	if err != nil {
		add("server_id: %v", err)
	}

	prefix, err := netip.ParsePrefix(c.Network)
	if err != nil {
		add("network: %v", err)
	} else if prefix.Addr().BitLen() != 32 || !prefix.Addr().Is4() {
		add("network: must be IPv4: %q", c.Network)
	}

	mask, err := parseIPv4(c.Netmask)
	if err != nil {
		add("netmask: %v", err)
	} else if prefix.IsValid() {
		maskBytes := mask.As4()
		for i := range maskBytes {
			if maskBytes[i]&^cidrMaskByte(prefix.Bits(), i) != 0 {
				add("netmask %s is wider than network %s", c.Netmask, c.Network)
				break
			}
		}
	}

	start, err1 := parseIPv4(c.PoolStart)
	end, err2 := parseIPv4(c.PoolEnd)
	switch {
	case err1 != nil:
		add("pool_start: %v", err1)
	case err2 != nil:
		add("pool_end: %v", err2)
	default:
		if v4Greater(end, start) == false {
			add("pool_end %s must be greater than pool_start %s", c.PoolEnd, c.PoolStart)
		}
		if prefix.IsValid() {
			if !prefix.Contains(start) || !prefix.Contains(end) {
				add("pool %s-%s must be inside network %s", c.PoolStart, c.PoolEnd, c.Network)
			}
		}
		if sid.IsValid() && start.IsValid() {
			// server id may be inside the subnet but must never be handed out
			if inRange(sid, start, end) {
				add("server_id %s must not be inside the allocatable pool", c.ServerID)
			}
		}
	}

	if c.Router != "" {
		if r, err := parseIPv4(c.Router); err != nil {
			add("router: %v", err)
		} else if prefix.IsValid() && !prefix.Contains(r) {
			add("router %s must be inside network %s", c.Router, c.Network)
		}
	}
	for i, d := range c.DNS {
		if a, err := parseIPv4(d); err != nil {
			add("dns[%d]: %v", i, err)
		} else if prefix.IsValid() && !prefix.Contains(a) {
			add("dns[%d] %s must be inside network %s", i, d, c.Network)
		}
	}

	if c.LeaseTime.Duration <= 0 {
		add("lease_time must be > 0")
	}
	if c.OfferTTL.Duration <= 0 {
		add("offer_ttl must be > 0")
	}
	if c.SweepInterval.Duration <= 0 {
		add("sweep_interval must be > 0")
	}
	if c.OfferTTL.Duration >= c.LeaseTime.Duration {
		add("offer_ttl (%s) must be shorter than lease_time (%s)", c.OfferTTL, c.LeaseTime)
	}

	if err := checkListen("listen_udp", c.ListenUDP, c.AllowNonLoopback); err != nil {
		add("%v", err)
	}
	if err := checkListen("admin_http", c.AdminHTTP, c.AllowNonLoopback); err != nil {
		add("%v", err)
	}

	if len(errs) > 0 {
		return errors.New(strings.Join(errs, "; "))
	}
	return nil
}

func parseIPv4(s string) (netip.Addr, error) {
	a, err := netip.ParseAddr(s)
	if err != nil {
		return netip.Addr{}, fmt.Errorf("invalid IPv4 %q: %w", s, err)
	}
	if !a.Is4() {
		return netip.Addr{}, fmt.Errorf("not an IPv4 address %q", s)
	}
	return a, nil
}

func inRange(a, start, end netip.Addr) bool {
	return !v4Greater(start, a) && !v4Greater(a, end)
}

// v4Greater reports a > b for IPv4 addresses interpreted as 32-bit ints.
func v4Greater(a, b netip.Addr) bool {
	x := a.As4()
	y := b.As4()
	for i := range x {
		if x[i] != y[i] {
			return x[i] > y[i]
		}
	}
	return false
}

func cidrMaskByte(bits, byteIndex int) byte {
	// mask byte: leading (bits - i*8) bits set
	n := bits - byteIndex*8
	if n <= 0 {
		return 0
	}
	if n >= 8 {
		return 0xff
	}
	return byte(0xff << (8 - n))
}

func checkListen(name, addrPort string, allowNonLoopback bool) error {
	ap, err := netip.ParseAddrPort(addrPort)
	if err != nil {
		return fmt.Errorf("%s: invalid addr:port %q: %w", name, addrPort, err)
	}
	if ap.Port() == 0 {
		return fmt.Errorf("%s: port must not be zero", name)
	}
	if !ap.Addr().IsLoopback() {
		if !allowNonLoopback {
			return fmt.Errorf("%s: %s is non-loopback; set allow_non_loopback to permit", name, addrPort)
		}
	}
	if ap.Port() < 1024 && !allowNonLoopback {
		return fmt.Errorf("%s: privileged port %d refused without allow_non_loopback", name, ap.Port())
	}
	return nil
}
