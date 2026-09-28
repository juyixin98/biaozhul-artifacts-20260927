package nat

import (
	"testing"

	"natlab/internal/model"
)

func TestFSMOutbound(t *testing.T) {
	cases := []struct {
		name      string
		state     string
		flags     tcpFlags
		wantState string
		wantCode  string
		closeNow  bool
	}{
		{"retransmit SYN in syn_sent", model.StateSynSent, tcpFlags{SYN: true}, model.StateSynSent, "", false},
		{"bare ACK rejected in syn_sent", model.StateSynSent, tcpFlags{ACK: true}, "", model.CodeTCPBadState, false},
		{"FIN rejected in syn_sent", model.StateSynSent, tcpFlags{FIN: true, ACK: true}, "", model.CodeTCPBadState, false},
		{"ACK completes handshake", model.StateSynAckRcvd, tcpFlags{ACK: true}, model.StateEstablished, "", false},
		{"SYN rejected in syn_ack_rcvd", model.StateSynAckRcvd, tcpFlags{SYN: true}, "", model.CodeTCPBadState, false},
		{"data ACK stays established", model.StateEstablished, tcpFlags{ACK: true}, model.StateEstablished, "", false},
		{"FIN+ACK enters fin_wait", model.StateEstablished, tcpFlags{FIN: true, ACK: true}, model.StateFinWait, "", false},
		{"final ACK closes", model.StateFinWait, tcpFlags{ACK: true}, model.StateClosed, "", true},
		{"simultaneous FIN stays fin_wait", model.StateFinWait, tcpFlags{FIN: true, ACK: true}, model.StateFinWait, "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			next, closeNow, code, _ := fsmOutbound(tc.state, tc.flags)
			if code != tc.wantCode {
				t.Fatalf("code = %q, want %q", code, tc.wantCode)
			}
			if tc.wantCode == "" && next != tc.wantState {
				t.Fatalf("next = %q, want %q", next, tc.wantState)
			}
			if closeNow != tc.closeNow {
				t.Fatalf("closeNow = %v, want %v", closeNow, tc.closeNow)
			}
		})
	}
}

func TestFSMInbound(t *testing.T) {
	cases := []struct {
		name      string
		state     string
		flags     tcpFlags
		wantState string
		wantCode  string
		closeNow  bool
	}{
		{"SYN+ACK answers handshake", model.StateSynSent, tcpFlags{SYN: true, ACK: true}, model.StateSynAckRcvd, "", false},
		{"bare SYN not an answer", model.StateSynSent, tcpFlags{SYN: true}, "", model.CodeTCPBadState, false},
		{"bare ACK rejected in syn_sent", model.StateSynSent, tcpFlags{ACK: true}, "", model.CodeTCPBadState, false},
		{"retransmit SYN+ACK idempotent", model.StateSynAckRcvd, tcpFlags{SYN: true, ACK: true}, model.StateSynAckRcvd, "", false},
		{"data ACK stays established", model.StateEstablished, tcpFlags{ACK: true}, model.StateEstablished, "", false},
		{"FIN+ACK enters fin_wait", model.StateEstablished, tcpFlags{FIN: true, ACK: true}, model.StateFinWait, "", false},
		{"ACK of FIN closes", model.StateFinWait, tcpFlags{ACK: true}, model.StateClosed, "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			next, closeNow, code, _ := fsmInbound(tc.state, tc.flags)
			if code != tc.wantCode {
				t.Fatalf("code = %q, want %q", code, tc.wantCode)
			}
			if tc.wantCode == "" && next != tc.wantState {
				t.Fatalf("next = %q, want %q", next, tc.wantState)
			}
			if closeNow != tc.closeNow {
				t.Fatalf("closeNow = %v, want %v", closeNow, tc.closeNow)
			}
		})
	}
}

func TestParseTCPFlags(t *testing.T) {
	if f, bad := parseTCPFlags("syn+ack"); bad != "" || !f.SYN || !f.ACK {
		t.Fatalf("syn+ack parse: %+v bad=%q", f, bad)
	}
	if _, bad := parseTCPFlags("SYN+FIN"); bad == "" {
		t.Fatalf("SYN+FIN must be rejected")
	}
	if _, bad := parseTCPFlags("PSH"); bad == "" {
		t.Fatalf("PSH must be rejected (unmodeled flag)")
	}
	if _, bad := parseTCPFlags("RST+FIN"); bad == "" {
		t.Fatalf("RST+FIN must be rejected")
	}
	if _, bad := parseTCPFlags(""); bad == "" {
		t.Fatalf("empty flags must be rejected")
	}
}
