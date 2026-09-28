// Command genfixtures writes the synthetic pcap captures used for offline
// review into a directory, together with sidecar .expected.json files that
// contain the hand-authored known streams and expected gaps/conflicts.
//
// These files are the same scenarios covered by the Go tests; the generator
// exists so a reviewer can reproduce the CLI workflow from captures on disk
// without writing Go. Expected answers come from the string constants and
// arithmetic in this file, never from the reassembly engine.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"

	"tcpreplay/internal/netmodel"
	"tcpreplay/internal/testsupport"
)

type expectedSidecar struct {
	Scenario       string   `json:"scenario"`
	ClientISN      string   `json:"client_isn"`
	ServerISN      string   `json:"server_isn"`
	ClientOriginal string   `json:"client_original"`
	ServerOriginal string   `json:"server_original,omitempty"`
	Notes          []string `json:"notes"`
	// Expectations as data-byte offsets (first app byte = 0).
	Expectations map[string]any `json:"expectations"`
}

func main() {
	dir := flag.String("out", "test/fixtures", "output directory")
	flag.Parse()
	if err := os.MkdirAll(*dir, 0o755); err != nil {
		fatal(err)
	}

	type fixture struct {
		name    string
		build   func() *testsupport.Builder
		sidecar expectedSidecar
	}

	clientOrig := "HELLO-TCP-REASSEMBLY-WORLD!!"
	serverOrig := "ACK-DATA-FROM-SERVER-BYE"

	fixtures := []fixture{
		{
			name: "01-outoforder-gap",
			build: func() *testsupport.Builder {
				b := testsupport.NewBuilder(1000, 5000).Handshake()
				// Arrival order is deliberately not sequence order; middle
				// segment [6,12) is withheld forever.
				b.ClientData(12, []byte(clientOrig[12:20]))
				b.ClientData(20, []byte(clientOrig[20:28]))
				b.ClientData(0, []byte(clientOrig[0:6]))
				b.ClientFIN(28, nil)
				b.ServerData(0, []byte(serverOrig))
				b.ServerFIN(24, nil)
				return b
			},
			sidecar: expectedSidecar{
				Scenario:  "out-of-order delivery with a permanent 6-byte gap",
				ClientISN: "0x000003e8", ServerISN: "0x00001388",
				ClientOriginal: clientOrig, ServerOriginal: serverOrig,
				Notes: []string{
					"bytes at offsets 6..11 ('TCP-RE') are never sent",
					"client replay outputs only the 6-byte contiguous prefix 'HELLO-'",
					"gap [6,12) is proved by the FIN at offset 28",
				},
				Expectations: map[string]any{
					"client_contiguous_prefix": "HELLO-",
					"client_gaps":              []map[string]int64{{"start": 6, "end": 12}},
					"client_fin_position":      28,
					"client_length_proved":     28,
					"server_stream":            serverOrig,
				},
			},
		},
		{
			name: "02-retransmit",
			build: func() *testsupport.Builder {
				b := testsupport.NewBuilder(1000, 5000).Handshake()
				b.ClientData(0, []byte(clientOrig[0:6]))
				b.ClientData(0, []byte(clientOrig[0:6])) // exact duplicate
				b.ClientData(6, []byte(clientOrig[6:12]))
				b.ClientData(3, []byte(clientOrig[3:11])) // identical overlap
				b.ClientData(12, []byte(clientOrig[12:20]))
				b.ClientData(20, []byte(clientOrig[20:28]))
				b.ClientFIN(28, nil)
				b.ServerFIN(0, nil)
				return b
			},
			sidecar: expectedSidecar{
				Scenario:  "byte-identical retransmissions",
				ClientISN: "0x000003e8", ServerISN: "0x00001388",
				ClientOriginal: clientOrig,
				Notes: []string{
					"retransmitted bytes must not duplicate output",
					"no overlap conflicts may be reported",
				},
				Expectations: map[string]any{
					"client_stream":  clientOrig,
					"conflict_count": 0,
				},
			},
		},
		{
			name: "03-conflict-overlap",
			build: func() *testsupport.Builder {
				b := testsupport.NewBuilder(1000, 5000).Handshake()
				b.ClientData(0, []byte(clientOrig))
				b.ClientDataID(10, []byte("zzzzzzzz"), "evil-seg")
				b.ClientFIN(28, nil)
				b.ServerFIN(0, nil)
				return b
			},
			sidecar: expectedSidecar{
				Scenario:  "8 conflicting overlap bytes ('REASSEMB' vs 'zzzzzzzz')",
				ClientISN: "0x000003e8", ServerISN: "0x00001388",
				ClientOriginal: clientOrig,
				Notes: []string{
					"offered bytes 0x7a at offsets 10..17 disagree with established bytes",
					"policy first_wins: 8 rejected conflicts, stream stays the original",
					"policy last_wins: stream becomes HELLO-TCP-zzzzzzzzLY-WORLD!!",
					"policy quarantine: 8 bytes held, stream stays the original",
				},
				Expectations: map[string]any{
					"conflict_offsets": []int{10, 11, 12, 13, 14, 15, 16, 17},
					"accepted_hex":     "5245415353454d42", // "REASSEMB"
					"offered_hex":      "7a7a7a7a7a7a7a7a", // z*8
					"offered_record":   "evil-seg",
				},
			},
		},
		{
			name: "04-seq-wrap",
			build: func() *testsupport.Builder {
				const isn uint32 = 0xFFFFFFF6
				b := testsupport.NewBuilder(isn, 7000).Handshake()
				b.ClientData(6, []byte(clientOrig[6:18]))
				b.ClientData(18, []byte(clientOrig[18:28]))
				b.ClientData(0, []byte(clientOrig[0:6]))
				b.ServerData(0, []byte(serverOrig))
				b.ClientFIN(28, nil)
				b.ServerFIN(24, nil)
				return b
			},
			sidecar: expectedSidecar{
				Scenario:  "data straddles the 32-bit sequence wrap",
				ClientISN: "0xfffffff6", ServerISN: "0x00001b58",
				ClientOriginal: clientOrig, ServerOriginal: serverOrig,
				Notes: []string{
					"first client data byte raw seq 0xfffffff7; offsets 9+ wrap to 0x00000000+",
					"client FIN raw seq 0x00000013, at data offset 28",
				},
				Expectations: map[string]any{
					"client_stream":          clientOrig,
					"server_stream":          serverOrig,
					"client_fin_raw_seq":     "0x00000013",
					"client_fin_data_offset": 28,
				},
			},
		},
		{
			name: "05-half-close",
			build: func() *testsupport.Builder {
				b := testsupport.NewBuilder(1000, 5000).Handshake()
				b.ClientData(0, []byte(clientOrig[0:14]))
				b.ClientFIN(14, []byte(clientOrig[14:28]))
				b.ServerData(0, []byte(serverOrig))
				// stray byte after FIN: seq is isn+1+28
				b.RawSegment(testsupport.FromClient, uint32(1000+1+28),
					[]string{"ACK", "PSH"}, []byte("X"), "stray-after-fin")
				b.ServerFIN(24, nil)
				return b
			},
			sidecar: expectedSidecar{
				Scenario:  "half-close then a rejected post-FIN byte",
				ClientISN: "0x000003e8", ServerISN: "0x00001388",
				ClientOriginal: clientOrig, ServerOriginal: serverOrig,
				Notes: []string{
					"FIN carries the last 14 client bytes and consumes one more seq",
					"the stray 'X' at/after FIN is rejected, not output",
				},
				Expectations: map[string]any{
					"client_stream":                      clientOrig,
					"server_stream":                      serverOrig,
					"rejected_event":                     "DATA_AFTER_FIN_REJECTED",
					"rejected_record":                    "stray-after-fin",
					"rejected_payload_not_in_any_stream": true,
				},
			},
		},
		{
			name: "06-missing-handshake",
			build: func() *testsupport.Builder {
				// No SYN/SYN-ACK/ACK. Engine sees data first.
				b := testsupport.NewBuilder(4999, 7999)
				b.ClientData(6, []byte(clientOrig[6:18]))
				b.ServerData(6, []byte(serverOrig[6:18]))
				b.ClientData(0, []byte(clientOrig[0:6]))
				b.ServerData(0, []byte(serverOrig[0:6]))
				b.ClientData(18, []byte(clientOrig[18:28]))
				b.ServerData(18, []byte(serverOrig[18:24]))
				b.ClientFIN(28, nil)
				b.ServerFIN(24, nil)
				return b
			},
			sidecar: expectedSidecar{
				Scenario:  "no handshake observed (anchorless)",
				ClientISN: "unknown", ServerISN: "unknown",
				ClientOriginal: clientOrig, ServerOriginal: serverOrig,
				Notes: []string{
					"offsets are relative to the first observed byte, not the true ISN",
					"handshake_known=false and an undecided HANDSHAKE_ABSENT event is emitted",
				},
				Expectations: map[string]any{
					"client_stream":   clientOrig,
					"server_stream":   serverOrig,
					"handshake_known": false,
					"undecided_event": "HANDSHAKE_ABSENT",
				},
			},
		},
		{
			name: "07-connection-reuse",
			build: func() *testsupport.Builder {
				b := testsupport.NewBuilder(1000, 5000)
				b.ClientSYN()
				b.ServerSYNACK()
				b.ClientHandshakeACK()
				b.ClientData(0, []byte("GEN0-CLIENT"))
				b.ServerData(0, []byte("GEN0-SERVER"))
				b.ClientFIN(11, nil)
				b.ServerFIN(11, nil)
				b.RawSegment(testsupport.FromClient, 9000, []string{"SYN"}, nil, "g1-syn")
				b.RawSegment(testsupport.FromServer, 9500, []string{"SYN", "ACK"}, nil, "g1-synack").
					RawSegment(testsupport.FromClient, 9001, []string{"ACK"}, nil, "g1-ack")
				b.RawSegment(testsupport.FromClient, 9001, []string{"ACK", "PSH"}, []byte("GEN1-CLIENT"), "g1-cdata")
				b.RawSegment(testsupport.FromServer, 9501, []string{"ACK", "PSH"}, []byte("GEN1-SERVER"), "g1-sdata")
				b.RawSegment(testsupport.FromClient, 9001+11, []string{"ACK", "FIN"}, nil, "g1-cfin")
				b.RawSegment(testsupport.FromServer, 9501+11, []string{"ACK", "FIN"}, nil, "g1-sfin")
				return b
			},
			sidecar: expectedSidecar{
				Scenario:  "same 4-tuple reused; two handshake generations",
				ClientISN: "gen0=0x3e8 gen1=0x2328", ServerISN: "gen0=0x1388 gen1=0x251c",
				ClientOriginal: "GEN0-CLIENT | GEN1-CLIENT",
				ServerOriginal: "GEN0-SERVER | GEN1-SERVER",
				Notes: []string{
					"generations must be indexed 0 and 1 with isolated streams",
				},
				Expectations: map[string]any{
					"generation_count": 2,
					"gen0_client":      "GEN0-CLIENT",
					"gen1_client":      "GEN1-CLIENT",
					"gen0_server":      "GEN0-SERVER",
					"gen1_server":      "GEN1-SERVER",
				},
			},
		},
	}

	for _, f := range fixtures {
		pkts := f.build().Packets()
		pcapPath := filepath.Join(*dir, f.name+".pcap")
		if err := os.WriteFile(pcapPath, netmodel.WritePCap(pkts), 0o644); err != nil {
			fatal(err)
		}
		f.sidecar.Expectations["packet_count"] = len(pkts)
		expPath := filepath.Join(*dir, f.name+".expected.json")
		raw, _ := json.MarshalIndent(f.sidecar, "", "  ")
		if err := os.WriteFile(expPath, append(raw, '\n'), 0o644); err != nil {
			fatal(err)
		}
		fmt.Printf("wrote %s (%d packets) + sidecar\n", pcapPath, len(pkts))
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "genfixtures:", err)
	os.Exit(1)
}
