// Command reasm 是离线 IPv4 分片重组后端的命令行入口。
//
// 子命令：
//
//	serve    在回环地址启动 HTTP 回放接口（不主动外发任何报文）
//	replay   回放本地 pcap 夹具并输出 JSON 审计报告
//	fixture  生成确定性合成夹具（pcap + 期望清单）
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"net/netip"
	"os"
	"time"

	"ipfragreasm/internal/api"
	"ipfragreasm/internal/config"
	"ipfragreasm/internal/fixture"
	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/reasm"
	"ipfragreasm/internal/replay"
	"ipfragreasm/internal/store"
	"ipfragreasm/internal/version"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "serve":
		err = cmdServe(os.Args[2:])
	case "replay":
		err = cmdReplay(os.Args[2:])
	case "fixture":
		err = cmdFixture(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	case "version":
		fmt.Printf("reasm %s (algorithm=%s, go offline build)\n", version.Version, version.Algorithm)
	default:
		fmt.Fprintf(os.Stderr, "未知子命令: %s\n", os.Args[1])
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "错误: %v\n", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `reasm — 离线 IPv4 分片重组后端

用法:
  reasm serve    [-config path] [-listen 127.0.0.1:8080] [-sqlite dsn] [-timeout 30s]
  reasm replay   -pcap file [-linktype 1] [-sqlite dsn] [-timeout 2s] [-after 3s]
  reasm fixture  -outdir testdata/scenario [-scenario permutation] [-size 60]
  reasm version
`)
}

func openStore(ctx context.Context, dsn string) (store.Store, error) {
	if dsn == "" || dsn == "memory" {
		return store.NewMemory(), nil
	}
	return store.OpenSQLite(ctx, dsn)
}

func assemblerConfig(cfg config.Config) reasm.Config {
	return reasm.Config{
		Timeout:          cfg.ReassembleTimeout.Duration(),
		ResultTTL:        cfg.ResultTTL.Duration(),
		MaxDatagramBytes: cfg.MaxDatagramBytes,
	}
}

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	cfgPath := fs.String("config", "configs/config.json", "配置文件路径（不存在则用默认值）")
	listen := fs.String("listen", "", "覆盖监听地址（默认仅回环）")
	dsn := fs.String("sqlite", "", "覆盖 SQLite DSN（memory 表示内存）")
	timeout := fs.Duration("timeout", 0, "覆盖重组超时")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		return err
	}
	if *listen != "" {
		cfg.Listen = *listen
	}
	if *dsn != "" {
		cfg.SQLiteDSN = *dsn
	}
	if *timeout > 0 {
		cfg.ReassembleTimeout = config.Duration(*timeout)
	}
	if err := cfg.Validate(); err != nil {
		return err
	}

	ctx := context.Background()
	st, err := openStore(ctx, cfg.SQLiteDSN)
	if err != nil {
		return err
	}
	defer st.Close()

	asm, err := reasm.New(assemblerConfig(cfg), st, nil)
	if err != nil {
		return err
	}
	if cfg.SweepInterval.Duration() > 0 {
		stop := startSweeper(ctx, asm, cfg.SweepInterval.Duration())
		defer stop()
	}

	srv := &httpServer{addr: cfg.Listen, handler: api.NewService(asm).Router()}
	fmt.Printf("reasm %s 监听 %s（离线，仅回环；存储=%s；超时=%s）\n",
		version.Version, cfg.Listen, maskDSN(cfg.SQLiteDSN), cfg.ReassembleTimeout.Duration())
	return srv.listenAndServe()
}

func cmdReplay(args []string) error {
	fs := flag.NewFlagSet("replay", flag.ContinueOnError)
	pcapPath := fs.String("pcap", "", "本地 pcap 夹具路径（必填）")
	dsn := fs.String("sqlite", "memory", "状态存储 DSN")
	timeout := fs.Duration("timeout", 2*time.Second, "重组超时（虚拟时间）")
	ttl := fs.Duration("ttl", 1*time.Hour, "终结结果留存")
	after := fs.Duration("after", 0, "回放结束后再推进的虚拟时间（用于确定性触发超时）")
	maxBytes := fs.Int("maxbytes", 65535, "重组字节上限")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *pcapPath == "" {
		return fmt.Errorf("-pcap 必填")
	}
	f, err := os.Open(*pcapPath)
	if err != nil {
		return err
	}
	defer f.Close()

	ctx := context.Background()
	st, err := openStore(ctx, *dsn)
	if err != nil {
		return err
	}
	defer st.Close()

	asm, err := reasm.New(reasm.Config{
		Timeout: *timeout, ResultTTL: *ttl, MaxDatagramBytes: *maxBytes,
	}, st, nil)
	if err != nil {
		return err
	}

	rep := replay.New(asm)
	var finalTime time.Time
	if *after > 0 {
		finalTime = time.Unix(0, 0).Add(*timeout + *after)
	}
	report, err := rep.Run(ctx, f, finalTime, nil)
	if err != nil {
		return err
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	return enc.Encode(report)
}

// Manifest 描述夹具的期望结果，供测试/CI 独立核对。
type Manifest struct {
	Version     string                 `json:"version"`
	Algorithm   string                 `json:"algorithm"`
	Scenario    string                 `json:"scenario"`
	PayloadLen  int                    `json:"payload_len"`
	PayloadSHA  string                 `json:"payload_sha256"`
	FragmentSeq []fixture.FragmentSpec `json:"fragment_seq"`
	Permutation []int                  `json:"permutation"`
	Expected    map[string]any         `json:"expected"`
	GeneratedAt string                 `json:"generated_at"`
}

func cmdFixture(args []string) error {
	fs := flag.NewFlagSet("fixture", flag.ContinueOnError)
	outDir := fs.String("outdir", "", "输出目录（必填）")
	scenario := fs.String("scenario", "ordered", "ordered|permutation|duplicate|overlap|conflict-last|timeout-reuse")
	size := fs.Int("size", 60, "载荷字节数")
	seed := fs.Uint64("seed", 42, "确定性 RNG 种子")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *outDir == "" {
		return fmt.Errorf("-outdir 必填")
	}
	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		return err
	}

	data := fixture.NewRNG(*seed).Bytes(*size)
	sizes := fragmentSizes(*size, 16)
	specs := fixture.SplitPayload(data, sizes)

	order := make([]int, len(specs))
	for i := range order {
		order[i] = i
	}
	expected := map[string]any{"verdict": "complete", "length": *size}

	hdr := fixture.IPHeaderOptions{
		Src: netip.MustParseAddr("10.0.0.1"), Dst: netip.MustParseAddr("10.0.0.2"),
		Protocol: netmodel.ProtoUDP, ID: 0x1234,
	}

	var frames []fixture.Frame
	base := time.Unix(1_700_000_000, 0)
	addFrames := func(seq []fixture.FragmentSpec, perm []int, id uint16, mutate func(int, fixture.IPHeaderOptions) fixture.IPHeaderOptions) {
		pkts := fixture.BuildFragments(func() fixture.IPHeaderOptions {
			h := hdr
			h.ID = id
			return h
		}(), seq, mutate)
		for i, idx := range perm {
			frames = append(frames, fixture.Frame{
				TS:    base.Add(time.Duration(i) * 10 * time.Millisecond),
				Frame: fixture.EthernetFrame(pkts[idx]),
			})
		}
	}

	switch *scenario {
	case "ordered":
		addFrames(specs, order, 0x1234, nil)
	case "permutation":
		order = reversePerm(len(specs)) // 末片先到的极端排列
		addFrames(specs, order, 0x1234, nil)
	case "duplicate":
		// 完全重复片夹在完成前送达：0,1,重复1,2,3...
		seq := dupSpecs(specs)
		perm := make([]int, len(seq))
		for i := range perm {
			perm[i] = i
		}
		addFrames(seq, perm, 0x1234, nil)
		expected["duplicates"] = 1
	case "overlap":
		// 0,1 之后立即送一片与第 2 片同区间但内容不同的覆盖片（完成前触发）。
		seq := overlapSpecs(specs, data)
		perm := make([]int, len(seq))
		for i := range perm {
			perm[i] = i
		}
		addFrames(seq, perm, 0x1234, nil)
		expected = map[string]any{"verdict": "rejected", "error_kind": "overlap_group_rejected"}
	case "conflict-last":
		// 先送一个“伪末片”宣告更大的总长，再送真实片，真实末片终点不同即冲突。
		seq := conflictLastSpecs(specs, data)
		perm := make([]int, len(seq))
		for i := range perm {
			perm[i] = i
		}
		addFrames(seq, perm, 0x1234, nil)
		expected = map[string]any{"verdict": "rejected", "error_kind": "conflicting_last_fragment"}
	case "timeout-reuse":
		// 第一轮缺少末片并超时；同一 ID 第二轮完整送达。
		first := specs[:len(specs)-1]
		addFrames(first, order[:len(first)], 0x2222, nil)
		gap := time.Second
		// 第二轮：把时间戳推进到超时之后，重新发送完整序列。
		pkts2 := fixture.BuildFragments(func() fixture.IPHeaderOptions {
			h := hdr
			h.ID = 0x2222
			return h
		}(), specs, nil)
		for i, pkt := range pkts2 {
			frames = append(frames, fixture.Frame{
				TS:    base.Add(gap + time.Duration(i)*10*time.Millisecond),
				Frame: fixture.EthernetFrame(pkt),
			})
		}
		expected = map[string]any{
			"verdict": "complete", "length": *size,
			"note": "第一轮缺末片应超时，第二轮复用同 ID 重组成功",
		}
	default:
		return fmt.Errorf("未知 scenario=%s", *scenario)
	}

	pcapFile, err := os.Create(*outDir + "/capture.pcap")
	if err != nil {
		return err
	}
	defer pcapFile.Close()
	if err := fixture.WritePCAP(pcapFile, netmodel.LinkTypeEthernet, frames); err != nil {
		return err
	}

	sum := sha256.Sum256(data)
	manifest := Manifest{
		Version: version.Version, Algorithm: version.Algorithm,
		Scenario: *scenario, PayloadLen: *size,
		PayloadSHA:  hex.EncodeToString(sum[:]),
		FragmentSeq: specs, Permutation: order, Expected: expected,
		GeneratedAt: time.Now().UTC().Format(time.RFC3339Nano),
	}
	mf, err := os.Create(*outDir + "/manifest.json")
	if err != nil {
		return err
	}
	defer mf.Close()
	enc := json.NewEncoder(mf)
	enc.SetIndent("", "  ")
	if err := enc.Encode(manifest); err != nil {
		return err
	}
	fmt.Printf("已生成夹具: %s/capture.pcap (%d 帧), %s/manifest.json scenario=%s\n",
		*outDir, len(frames), *outDir, *scenario)
	return nil
}

// fragmentSizes 按 base（8 的倍数）切分，末片吸收余数。
func fragmentSizes(total, base int) []int {
	var sizes []int
	for remaining := total; remaining > 0; {
		n := base
		if n > remaining {
			n = remaining
		}
		// 除末片外必须保持 8 字节对齐。
		if remaining-n != 0 && n%8 != 0 {
			n -= n % 8
		}
		sizes = append(sizes, n)
		remaining -= n
	}
	return sizes
}

func reversePerm(n int) []int {
	p := make([]int, n)
	for i := range p {
		p[i] = n - 1 - i
	}
	return p
}

func dupSpecs(specs []fixture.FragmentSpec) []fixture.FragmentSpec {
	// 0,1,重复1,2,...（完成前送达完全重复片）。
	out := make([]fixture.FragmentSpec, 0, len(specs)+1)
	out = append(out, specs[0], specs[1], specs[1])
	out = append(out, specs[2:]...)
	return out
}

func overlapSpecs(specs []fixture.FragmentSpec, data []byte) []fixture.FragmentSpec {
	// 0,1 后追加与第 2 片同区间但首字节被改坏的覆盖片，再送剩余片。
	bad := specs[1]
	badPayload := append([]byte(nil), bad.Payload...)
	badPayload[0] ^= 0xFF
	out := make([]fixture.FragmentSpec, 0, len(specs)+1)
	out = append(out, specs[0], specs[1],
		fixture.FragmentSpec{Offset8: bad.Offset8, Payload: badPayload, More: bad.More})
	out = append(out, specs[2:]...)
	return out
}

func conflictLastSpecs(specs []fixture.FragmentSpec, data []byte) []fixture.FragmentSpec {
	// 最先送“伪末片”（MF=0，宣告更大总长），随后送真实片，真实末片终点不同即冲突。
	total := len(data)
	fake := fixture.FragmentSpec{
		Offset8: uint16((total + 8) / 8),
		Payload: []byte{0xAB, 0xCD},
		More:    false,
	}
	out := make([]fixture.FragmentSpec, 0, len(specs)+1)
	out = append(out, fake)
	out = append(out, specs...)
	return out
}

func maskDSN(dsn string) string {
	if dsn == "" {
		return "file:data/reasm.sqlite?cache=shared"
	}
	return dsn
}
