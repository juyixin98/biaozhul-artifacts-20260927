package nat

import (
	"testing"

	"natlab/internal/model"
)

func TestStepTCPReferenceTransitions(t *testing.T) {
	syn := model.TCPFlagBits{SYN: true}
	synack := model.TCPFlagBits{SYN: true, ACK: true}
	ack := model.TCPFlagBits{ACK: true}
	finack := model.TCPFlagBits{FIN: true, ACK: true}
	rst := model.TCPFlagBits{RST: true}

	cases := []struct {
		name       string
		state      string
		dir        model.Direction
		flags      model.TCPFlagBits
		wantState  string
		wantClose  bool
		wantReject bool
	}{
		// SYN_SENT
		{"syn-retransmit", StateSynSent, model.Outbound, syn, StateSynSent, false, false},
		{"synack-opens", StateSynSent, model.Inbound, synack, StateEstablished, false, false},
		{"ack-before-synack", StateSynSent, model.Outbound, ack, StateSynSent, false, true},
		{"fin-in-handshake", StateSynSent, model.Outbound, finack, StateSynSent, false, true},
		{"bare-syn-inbound", StateSynSent, model.Inbound, syn, StateSynSent, false, true},
		{"rst-aborts-handshake", StateSynSent, model.Inbound, rst, StateClosed, true, false},

		// ESTABLISHED
		{"data-out", StateEstablished, model.Outbound, ack, StateEstablished, false, false},
		{"data-in", StateEstablished, model.Inbound, ack, StateEstablished, false, false},
		{"syn-on-established", StateEstablished, model.Inbound, syn, StateEstablished, false, true},
		{"fin-out", StateEstablished, model.Outbound, finack, StateFinWait1, false, false},
		{"fin-in", StateEstablished, model.Inbound, finack, StateFinWait2, false, false},
		{"rst-established", StateEstablished, model.Outbound, rst, StateClosed, true, false},

		// FIN_WAIT_1
		{"fw1-peer-fin", StateFinWait1, model.Inbound, finack, StateTimeWait, false, false},
		{"fw1-ack-wait", StateFinWait1, model.Inbound, ack, StateFinWait1, false, false},
		{"fw1-retransmit-fin", StateFinWait1, model.Outbound, finack, StateFinWait1, false, false},
		{"fw1-rst", StateFinWait1, model.Inbound, rst, StateClosed, true, false},

		// FIN_WAIT_2
		{"fw2-peer-fin", StateFinWait2, model.Inbound, finack, StateTimeWait, false, false},
		{"fw2-ack-stays", StateFinWait2, model.Inbound, ack, StateFinWait2, false, false},

		// TIME_WAIT
		{"tw-final-ack-closes", StateTimeWait, model.Inbound, ack, StateClosed, true, false},
		{"tw-duplicate-fin-stays", StateTimeWait, model.Inbound, finack, StateTimeWait, false, false},
		{"tw-stray-syn-rejected", StateTimeWait, model.Inbound, syn, StateTimeWait, false, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := stepTCP(tc.state, tc.dir, tc.flags)
			if got.stateAfter != tc.wantState {
				t.Errorf("state = %q, want %q", got.stateAfter, tc.wantState)
			}
			if got.close != tc.wantClose {
				t.Errorf("close = %v, want %v", got.close, tc.wantClose)
			}
			if got.reject != tc.wantReject {
				t.Errorf("reject = %v, want %v", got.reject, tc.wantReject)
			}
		})
	}
}

func TestStepTCPRSTValidInEveryState(t *testing.T) {
	for _, st := range []string{StateSynSent, StateEstablished, StateFinWait1,
		StateFinWait2, StateTimeWait} {
		got := stepTCP(st, model.Inbound, model.TCPFlagBits{RST: true})
		if !got.close || got.reject {
			t.Fatalf("RST in %s: close=%v reject=%v, want close=true", st, got.close, got.reject)
		}
	}
}
