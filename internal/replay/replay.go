// Package replay 把离线 pcap 夹具回放进重组器，全程不发送任何网络报文。
//
// 时间语义：使用 pcap 记录自身的捕获时间戳驱动“虚拟时钟”，因此无需 sleep
// 即可确定性地验证“超时 + ID 复用”场景；回放结束后可再推进虚拟时间做最终 Sweep。
package replay

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	"io"
	"time"

	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/reasm"
)

// PacketResult 记录单条 pcap 记录的处理事实。
type PacketResult struct {
	Index     int    `json:"index"`
	TS        string `json:"ts"`
	Skipped   bool   `json:"skipped,omitempty"`
	SkipWhy   string `json:"skip_why,omitempty"`
	Key       string `json:"key,omitempty"`
	State     string `json:"state,omitempty"`
	Accepted  bool   `json:"accepted,omitempty"`
	Duplicate bool   `json:"duplicate,omitempty"`
	ErrorKind string `json:"error_kind,omitempty"`
	Error     string `json:"error,omitempty"`
}

// FinalGroup 汇总回放结束时一个被关注分组键的终态。
type FinalGroup struct {
	Key    string `json:"key"`
	State  string `json:"state"`
	Reason string `json:"reason,omitempty"`
	Length int    `json:"length,omitempty"`
	SHA256 string `json:"sha256,omitempty"`
}

// Report 是一次回放的完整审计记录，测试日志与 CLI 共用。
type Report struct {
	LinkType      uint32         `json:"link_type"`
	PacketsRead   int            `json:"packets_read"`
	Processed     int            `json:"processed"`
	Skipped       int            `json:"skipped"`
	Results       []PacketResult `json:"results"`
	Finals        []FinalGroup   `json:"finals"`
	SweepTimedOut []string       `json:"sweep_timed_out,omitempty"`
	SweepRecycled []string       `json:"sweep_recycled,omitempty"`
	FinalTime     string         `json:"final_time,omitempty"`
}

// Replayer 回放 pcap 到重组器。
type Replayer struct {
	asm *reasm.Assembler
}

// New 构造回放器。
func New(asm *reasm.Assembler) *Replayer { return &Replayer{asm: asm} }

// Run 读取 pcap 字节流并逐帧回放。
//
// finalVirtualTime：回放结束后以此时间执行一次 Sweep（零值则取最后一条时间戳），
// 用于确定性触发超时；关注键用于在报告中附最终重组结果（可为空表示不附加）。
func (r *Replayer) Run(ctx context.Context, pcap io.Reader, finalVirtualTime time.Time,
	watchKeys []netmodel.FragKey) (*Report, error) {

	report := &Report{Results: []PacketResult{}}

	var (
		lastTS   time.Time
		linkType uint32
		err      error
	)
	err = ReadPCAP(pcap, func(lt uint32, rec RawRecord) error {
		linkType = lt
		// 逐帧前按当前虚拟时间回收：捕捉“两帧之间越过 deadline”的超时，
		// 使随后同 ID 的片开启新一轮重组（无需真实 sleep）。
		if !rec.TS.IsZero() {
			sr, serr := r.asm.SweepAt(ctx, rec.TS)
			if serr != nil {
				return serr
			}
			for _, k := range sr.TimedOut {
				report.SweepTimedOut = append(report.SweepTimedOut, k.String())
			}
			for _, k := range sr.Recycled {
				report.SweepRecycled = append(report.SweepRecycled, k.String())
			}
		}
		idx := len(report.Results)
		if rec.TS.After(lastTS) {
			lastTS = rec.TS
		}
		pr := PacketResult{Index: idx, TS: rec.TS.Format(time.RFC3339Nano)}

		ipBytes, err := netmodel.ExtractIPv4(linkType, rec.Bytes)
		if err != nil {
			if pe, ok := err.(*netmodel.ParseError); ok && pe.Kind == netmodel.ErrNotIPv4Ethertype {
				pr.Skipped, pr.SkipWhy = true, "非 IPv4 帧（ethertype/family 不匹配），跳过"
				report.Results = append(report.Results, pr)
				report.Skipped++
				return nil
			}
			pr.Skipped, pr.SkipWhy = true, fmt.Sprintf("链路层解封装失败: %v", err)
			report.Results = append(report.Results, pr)
			report.Skipped++
			return nil
		}

		pkt, err := netmodel.ParseIPv4(ipBytes)
		if err != nil {
			pr.Error, pr.ErrorKind = err.Error(), parseErrorKindString(err)
			report.Results = append(report.Results, pr)
			return nil
		}

		pr.Key = pkt.Key.String()
		out, perr := r.asm.ProcessAt(ctx, pkt, rec.TS)
		if perr != nil {
			if e, ok := reasm.AsError(perr); ok {
				pr.ErrorKind = string(e.Kind)
			} else {
				pr.ErrorKind = parseErrorKindString(perr)
			}
			pr.Error = perr.Error()
			report.Results = append(report.Results, pr)
			return nil
		}
		pr.State = string(out.State)
		pr.Accepted = out.Accepted
		pr.Duplicate = out.Duplicate
		report.Results = append(report.Results, pr)
		report.Processed++
		return nil
	})
	if err != nil {
		return nil, err
	}
	report.LinkType = linkType
	report.PacketsRead = len(report.Results)

	sweepAt := finalVirtualTime
	if sweepAt.IsZero() {
		sweepAt = lastTS
	}
	if !sweepAt.IsZero() {
		sr, err := r.asm.SweepAt(ctx, sweepAt)
		if err != nil {
			return nil, err
		}
		for _, k := range sr.TimedOut {
			report.SweepTimedOut = append(report.SweepTimedOut, k.String())
		}
		for _, k := range sr.Recycled {
			report.SweepRecycled = append(report.SweepRecycled, k.String())
		}
		report.FinalTime = sweepAt.Format(time.RFC3339Nano)
	}

	for _, key := range watchKeys {
		snap, err := r.asm.Lookup(ctx, key)
		if err != nil {
			continue // 可能已彻底回收
		}
		fg := FinalGroup{Key: key.String(), State: string(snap.State), Reason: snap.Reason}
		if len(snap.Assembled) > 0 {
			fg.Length = len(snap.Assembled)
			fg.SHA256 = sha256Hex(snap.Assembled)
		}
		report.Finals = append(report.Finals, fg)
	}
	return report, nil
}

func parseErrorKindString(err error) string {
	if pe, ok := err.(*netmodel.ParseError); ok {
		return string(pe.Kind)
	}
	return "error"
}

// AssembledFor 取出某键当前已重组字节（供 CLI/测试核对），不存在返回 nil。
func (r *Replayer) AssembledFor(ctx context.Context, key netmodel.FragKey) []byte {
	snap, err := r.asm.Lookup(ctx, key)
	if err != nil {
		return nil
	}
	return snap.Assembled
}

// EqualBytes 小工具：比较重组字节。
func EqualBytes(a, b []byte) bool { return bytes.Equal(a, b) }

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return fmt.Sprintf("%x", sum[:])
}
