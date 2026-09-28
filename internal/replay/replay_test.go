package replay_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"testing"
	"time"

	"ipfragreasm/internal/fixture"
	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/reasm"
	"ipfragreasm/internal/replay"
	"ipfragreasm/internal/store"
	"ipfragreasm/internal/testlog"
	"ipfragreasm/internal/testutil"

	"net/netip"
)

// buildPCAP 把若干分片按给定排列与逐帧时间偏移写进内存 pcap。
func buildPCAP(t *testing.T, specs []fixture.FragmentSpec, order []int,
	linkType uint32, base time.Time, frame func([]byte) []byte) (*bytes.Buffer, netmodel.FragKey) {
	t.Helper()
	hdr := fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.0.0.1"), Dst: netip.MustParseAddr("10.0.0.2"),
		Protocol: netmodel.ProtoUDP, ID: 0x7777,
	}
	pkts := fixture.BuildFragments(hdr, specs, nil)
	var frames []fixture.Frame
	for i, idx := range order {
		var f []byte = pkts[idx]
		if frame != nil {
			f = frame(f)
		}
		frames = append(frames, fixture.Frame{TS: base.Add(time.Duration(i) * time.Millisecond), Frame: f})
	}
	var buf bytes.Buffer
	if err := fixture.WritePCAP(&buf, linkType, frames); err != nil {
		t.Fatalf("write pcap: %v", err)
	}
	return &buf, netmodel.FragKey{Src: hdr.Src, Dst: hdr.Dst, Protocol: hdr.Protocol, ID: hdr.ID}
}

func TestReplayPCAPRoundtrip(t *testing.T) {
	log := testlog.New(t, "replay/roundtrip")
	data := fixture.PatternPayload(40)
	specs := fixture.SplitPayload(data, []int{8, 16, 16})
	order := []int{2, 0, 1} // 末片先到
	pcap, key := buildPCAP(t, specs, order, netmodel.LinkTypeEthernet,
		time.Unix(1_700_000_000, 0), fixture.EthernetFrame)

	st := store.NewMemory()
	asm, err := reasm.New(reasm.Config{Timeout: time.Minute, ResultTTL: time.Minute, MaxDatagramBytes: 65535}, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	rep := replay.New(asm)
	report, err := rep.Run(context.Background(), pcap, time.Time{}, []netmodel.FragKey{key})
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if report.PacketsRead != 3 {
		t.Fatalf("应读到 3 帧，实际 %d", report.PacketsRead)
	}
	if len(report.Finals) != 1 || report.Finals[0].State != "complete" || report.Finals[0].Length != 40 {
		t.Fatalf("终态错误: %+v", report.Finals)
	}
	wantSum := sha256.Sum256(data)
	if report.Finals[0].SHA256 != fmt.Sprintf("%x", wantSum[:]) {
		t.Fatalf("重组字节 SHA256 不匹配")
	}
	log.Pass("roundtrip", "pcap-3frames", "以太网 pcap 往返回放，末片先到仍正确重组且哈希一致",
		map[string]any{"frames": 3, "length": 40})
}

func TestReplayTimeoutAndIDReuse(t *testing.T) {
	log := testlog.New(t, "replay/timeout-reuse")
	data := fixture.PatternPayload(32)
	specs := fixture.SplitPayload(data, []int{16, 16})
	hdr := fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.0.0.1"), Dst: netip.MustParseAddr("10.0.0.2"),
		Protocol: netmodel.ProtoUDP, ID: 0x8888,
	}
	pkts := fixture.BuildFragments(hdr, specs, nil)
	base := time.Unix(1_700_000_000, 0)
	frames := []fixture.Frame{
		// 第一轮只送首片。
		{TS: base, Frame: fixture.LoopbackFrame(pkts[0])},
		// 第二轮（1 秒后，超过 500ms 超时）完整重发同 ID。
		{TS: base.Add(time.Second), Frame: fixture.LoopbackFrame(pkts[0])},
		{TS: base.Add(time.Second + 10*time.Millisecond), Frame: fixture.LoopbackFrame(pkts[1])},
	}
	var buf bytes.Buffer
	if err := fixture.WritePCAP(&buf, netmodel.LinkTypeLoop, frames); err != nil {
		t.Fatal(err)
	}

	st := store.NewMemory()
	asm, _ := reasm.New(reasm.Config{Timeout: 500 * time.Millisecond, ResultTTL: time.Hour, MaxDatagramBytes: 65535}, st, nil)
	rep := replay.New(asm)
	key := netmodel.FragKey{Src: hdr.Src, Dst: hdr.Dst, Protocol: hdr.Protocol, ID: hdr.ID}
	report, err := rep.Run(context.Background(), &buf, base.Add(2*time.Second), []netmodel.FragKey{key})
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if len(report.SweepTimedOut) != 1 || report.SweepTimedOut[0] != key.String() {
		t.Fatalf("第一轮应被记录为超时回收: %+v", report.SweepTimedOut)
	}
	if len(report.Finals) != 1 || report.Finals[0].State != "complete" {
		t.Fatalf("第二轮复用 ID 应完成: %+v", report.Finals)
	}
	log.Pass("timeout-reuse", "pcap", "缺片超时回收后，同 ID 完整第二轮重组成功",
		map[string]any{"timeout": "500ms", "final_state": report.Finals[0].State})
}

func TestReplaySkipsNonIPv4AndBadFrames(t *testing.T) {
	base := time.Unix(1_700_000_000, 0)
	// 一帧 ARP + 一帧坏 IPv4 + 一帧合法。
	arp := make([]byte, 64)
	arp[12], arp[13] = 0x08, 0x06

	good := fixture.BuildIPv4([]byte{1, 2, 3, 4}, fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.0.0.1"), Dst: netip.MustParseAddr("10.0.0.2"),
		Protocol: netmodel.ProtoUDP, ID: 1,
	})
	bad := fixture.BuildIPv4([]byte{9}, fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.0.0.1"), Dst: netip.MustParseAddr("10.0.0.2"),
		Protocol: netmodel.ProtoUDP, ID: 2, BadChecksum: true,
	})
	frames := []fixture.Frame{
		{TS: base, Frame: arp}, // arp 本身即 64 字节原始以太网帧，不能再包一层
		{TS: base.Add(time.Millisecond), Frame: fixture.EthernetFrame(bad)},
		{TS: base.Add(2 * time.Millisecond), Frame: fixture.EthernetFrame(good)},
	}
	var buf bytes.Buffer
	if err := fixture.WritePCAP(&buf, netmodel.LinkTypeEthernet, frames); err != nil {
		t.Fatal(err)
	}
	asm, _ := testutil.NewAssembler(t, time.Minute, time.Minute, 65535, nil)
	report, err := replay.New(asm).Run(context.Background(), &buf, time.Time{}, nil)
	if err != nil {
		t.Fatalf("replay: %v", err)
	}
	if report.Skipped != 1 {
		t.Fatalf("ARP 应恰好跳过 1 帧，实际 %d", report.Skipped)
	}
	// 坏校验和帧记录具体 error_kind，合法帧照常处理；不能把坏帧算成功。
	sawBadChecksum := false
	processedGood := false
	for _, r := range report.Results {
		if r.ErrorKind == string(netmodel.ErrBadChecksum) {
			sawBadChecksum = true
		}
		if r.State == "pending" || r.State == "complete" {
			processedGood = true
		}
	}
	if !sawBadChecksum || !processedGood {
		t.Fatalf("坏校验和必须显式标记且合法帧仍被处理: %+v", report.Results)
	}
}

func TestReassemblerRecoversFromSQLite(t *testing.T) {
	// 先把“末片先到”的单片持久化到 SQLite，关闭后重开，
	// 验证重组器能恢复 pending 组并在补齐后完成（重启恢复语义）。
	ctx := context.Background()
	path := t.TempDir() + "/rec.sqlite"
	key := netmodel.FragKey{
		Src: netip.MustParseAddr("10.0.0.1"), Dst: netip.MustParseAddr("10.0.0.2"),
		Protocol: netmodel.ProtoUDP, ID: 0x9999,
	}
	data := fixture.PatternPayload(24)
	specs := fixture.SplitPayload(data, []int{8, 16})
	pkts := fixture.BuildFragments(fixture.IPHeaderOptions{
		Src: key.Src, Dst: key.Dst, Protocol: key.Protocol, ID: key.ID,
	}, specs, nil)

	st1, err := store.OpenSQLite(ctx, "file:"+path)
	if err != nil {
		t.Fatal(err)
	}
	asm1, err := reasm.New(reasm.Config{Timeout: time.Hour, ResultTTL: time.Hour, MaxDatagramBytes: 65535}, st1, nil)
	if err != nil {
		t.Fatal(err)
	}
	p2, err := netmodel.ParseIPv4(pkts[1]) // 末片
	if err != nil {
		t.Fatal(err)
	}
	if _, err := asm1.Process(ctx, p2); err != nil {
		t.Fatalf("末片: %v", err)
	}
	if err := st1.Close(); err != nil {
		t.Fatal(err)
	}

	st2, err := store.OpenSQLite(ctx, "file:"+path)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	asm2, err := reasm.New(reasm.Config{Timeout: time.Hour, ResultTTL: time.Hour, MaxDatagramBytes: 65535}, st2, nil)
	if err != nil {
		t.Fatal(err)
	}
	snap, err := asm2.Lookup(ctx, key)
	if err != nil || snap.State != reasm.StatePending || !snap.Progress.HasLast {
		t.Fatalf("重启后应恢复为 pending 且记得末片: snap=%+v err=%v", snap, err)
	}
	p0, _ := netmodel.ParseIPv4(pkts[0])
	out, err := asm2.Process(ctx, p0)
	if err != nil || out.State != reasm.StateComplete || !bytes.Equal(out.Assembled, data) {
		t.Fatalf("恢复后补齐应完成: err=%v state=%s", err, out.State)
	}
}
