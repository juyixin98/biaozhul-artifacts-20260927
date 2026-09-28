package api_test

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"testing"
	"time"

	"ipfragreasm/internal/api"
	"ipfragreasm/internal/fixture"
	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/reasm"
	"ipfragreasm/internal/store"
	"ipfragreasm/internal/testlog"
)

func newTestServer(t *testing.T) (*httptest.Server, *reasm.Assembler) {
	t.Helper()
	st := store.NewMemory()
	asm, err := reasm.New(reasm.Config{Timeout: time.Minute, ResultTTL: time.Minute, MaxDatagramBytes: 65535}, st, nil)
	if err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(api.NewService(asm).Router())
	t.Cleanup(srv.Close)
	return srv, asm
}

func pktB64(t *testing.T, off8 uint16, more bool, payload []byte, id uint16) string {
	t.Helper()
	raw := fixture.BuildIPv4(payload, fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.1.0.1"), Dst: netip.MustParseAddr("10.1.0.2"),
		Protocol: netmodel.ProtoUDP, ID: id, Offset8: off8, More: more,
	})
	return base64.StdEncoding.EncodeToString(raw)
}

func TestHealth(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("health 状态码 %d", resp.StatusCode)
	}
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body["network"] != "offline-loopback-only" || body["version"] == "" {
		t.Fatalf("health 内容错误: %+v", body)
	}
}

func TestFragmentSubmissionCompleteAndLookup(t *testing.T) {
	log := testlog.New(t, "api/fragments")
	srv, _ := newTestServer(t)
	data := []byte("0123456789abcdef") // 16 字节单片即末片

	// 单片非分片数据报（offset=0, MF=0）应直接 complete -> 201。
	body := map[string]string{"packet_base64": pktB64(t, 0, false, data, 0x4242)}
	rb, _ := json.Marshal(body)
	resp, err := http.Post(srv.URL+"/api/v1/fragments", "application/json", bytes.NewReader(rb))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusCreated {
		t.Fatalf("单片完成应 201，实际 %d", resp.StatusCode)
	}
	var got map[string]any
	json.NewDecoder(resp.Body).Decode(&got)
	if got["state"] != "complete" {
		t.Fatalf("state 错误: %v", got["state"])
	}
	enc, _ := got["assembled_base64"].(string)
	decoded, _ := base64.StdEncoding.DecodeString(enc)
	if string(decoded) != string(data) {
		t.Fatalf("重组字节回传错误")
	}
	log.Pass("fragments", "single-complete", "单片完成返回 201 且字节正确",
		map[string]any{"status": 201})

	// 查询该组（完成留存）。
	key := "10.1.0.1->10.1.0.2/proto=17/id=16962"
	gresp, err := http.Get(srv.URL + "/api/v1/groups/" + key)
	if err != nil {
		t.Fatal(err)
	}
	defer gresp.Body.Close()
	if gresp.StatusCode != http.StatusOK {
		t.Fatalf("查询完成组应 200，实际 %d", gresp.StatusCode)
	}

	// 完成留存期内复用 ID -> 409。
	resp2, err := http.Post(srv.URL+"/api/v1/fragments", "application/json", bytes.NewReader(rb))
	if err != nil {
		t.Fatal(err)
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusConflict {
		t.Fatalf("完成留存期复用 ID 应 409，实际 %d", resp2.StatusCode)
	}
	var errBody map[string]string
	json.NewDecoder(resp2.Body).Decode(&errBody)
	if errBody["error"] != string(reasm.KindGroupAlreadyTerminal) {
		t.Fatalf("错误类别应为 group_already_terminal，实际 %s", errBody["error"])
	}
	log.Pass("fragments", "ttl-reuse-blocked", "完成留存期复用 ID 返回 409 与具体类别",
		map[string]any{"status": 409})
}

func TestFragmentRejectStatusCodes(t *testing.T) {
	srv, _ := newTestServer(t)

	// 非法 JSON -> 400。
	resp, _ := http.Post(srv.URL+"/api/v1/fragments", "application/json", strings.NewReader("{not-json"))
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("非法 JSON 应 400，实际 %d", resp.StatusCode)
	}
	resp.Body.Close()

	// 坏校验和报文 -> 400 + 具体 kind。
	raw := fixture.BuildIPv4([]byte{1}, fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.1.0.1"), Dst: netip.MustParseAddr("10.1.0.2"),
		Protocol: netmodel.ProtoUDP, ID: 0x5000, BadChecksum: true,
	})
	rb, _ := json.Marshal(map[string]string{"packet_base64": base64.StdEncoding.EncodeToString(raw)})
	resp, _ = http.Post(srv.URL+"/api/v1/fragments", "application/json", bytes.NewReader(rb))
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("坏校验和应 400，实际 %d", resp.StatusCode)
	}
	var eb map[string]string
	json.NewDecoder(resp.Body).Decode(&eb)
	resp.Body.Close()
	if eb["error"] != string(netmodel.ErrBadChecksum) {
		t.Fatalf("应返回具体 kind bad_header_checksum，实际 %s", eb["error"])
	}

	// MF=1 长度非 8 倍数 -> 422。
	rb, _ = json.Marshal(map[string]string{"packet_base64": pktB64(t, 0, true, make([]byte, 7), 0x5001)})
	resp, _ = http.Post(srv.URL+"/api/v1/fragments", "application/json", bytes.NewReader(rb))
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("非对齐片应 422，实际 %d", resp.StatusCode)
	}
	resp.Body.Close()

	// 查询不存在组 -> 404。
	resp, _ = http.Get(srv.URL + "/api/v1/groups/1.2.3.4->5.6.7.8/proto=6/id=1")
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("不存在组应 404，实际 %d", resp.StatusCode)
	}
	resp.Body.Close()
}

func TestReplayPCAPEndpoint(t *testing.T) {
	srv, _ := newTestServer(t)
	data := fixture.PatternPayload(24)
	specs := fixture.SplitPayload(data, []int{8, 16})
	pkts := fixture.BuildFragments(fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.1.0.1"), Dst: netip.MustParseAddr("10.1.0.2"),
		Protocol: netmodel.ProtoUDP, ID: 0x6000,
	}, specs, nil)
	var frames []fixture.Frame
	for i, p := range []int{1, 0} {
		frames = append(frames, fixture.Frame{Frame: fixture.EthernetFrame(pkts[p])})
		_ = i
	}
	var buf bytes.Buffer
	if err := fixture.WritePCAP(&buf, netmodel.LinkTypeEthernet, frames); err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(srv.URL+"/api/v1/replay/pcap", "application/vnd.tcpdump.pcap", &buf)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("pcap 回放应 200，实际 %d", resp.StatusCode)
	}
	var report map[string]any
	json.NewDecoder(resp.Body).Decode(&report)
	if report["packets_read"].(float64) != 2 {
		t.Fatalf("应读 2 帧: %v", report["packets_read"])
	}

	// 非 pcap 垃圾字节 -> 400。
	resp2, _ := http.Post(srv.URL+"/api/v1/replay/pcap", "application/octet-stream",
		strings.NewReader("this-is-not-a-pcap"))
	if resp2.StatusCode != http.StatusBadRequest {
		t.Fatalf("垃圾 pcap 应 400，实际 %d", resp2.StatusCode)
	}
	resp2.Body.Close()
}

func TestStats(t *testing.T) {
	srv, _ := newTestServer(t)
	resp, err := http.Get(srv.URL + "/api/v1/stats")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("stats 应 200，实际 %d", resp.StatusCode)
	}
	var stats map[string]int
	if err := json.NewDecoder(resp.Body).Decode(&stats); err != nil {
		t.Fatal(err)
	}
	if stats["active_pending"] != 0 || stats["store_groups"] != 0 {
		t.Fatalf("初始 stats 应为零: %+v", stats)
	}
}
