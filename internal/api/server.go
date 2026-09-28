// Package api 暴露仅监听回环地址的 HTTP 回放接口。
// 服务端自身不发起任何对外连接；所有输入均来自请求体中的本地夹具字节。
package api

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/netip"
	"strings"
	"time"

	"ipfragreasm/internal/netmodel"
	"ipfragreasm/internal/reasm"
	"ipfragreasm/internal/replay"
	"ipfragreasm/internal/version"
)

// Service 组装重组器与回放器并提供 HTTP 处理器。
type Service struct {
	Asm *reasm.Assembler
}

// NewService 创建服务。
func NewService(asm *reasm.Assembler) *Service { return &Service{Asm: asm} }

// Router 注册全部路由。
func (s *Service) Router() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.handleHealth)
	mux.HandleFunc("POST /api/v1/fragments", s.handleFragment)
	mux.HandleFunc("POST /api/v1/replay/pcap", s.handleReplayPCAP)
	mux.HandleFunc("GET /api/v1/groups/", s.handleGetGroup)
	mux.HandleFunc("DELETE /api/v1/groups/", s.handleDeleteGroup)
	mux.HandleFunc("GET /api/v1/stats", s.handleStats)
	return logRequest(mux)
}

func (s *Service) handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{
		"status": "ok", "version": version.Version, "algorithm": version.Algorithm,
		"network": "offline-loopback-only",
	})
}

// fragmentRequest 为单片提交载荷。
type fragmentRequest struct {
	// PacketBase64 为裸 IPv4 报文的 base64；也可用下面字段手工指定。
	PacketBase64 string  `json:"packet_base64"`
	Src          string  `json:"src,omitempty"`
	Dst          string  `json:"dst,omitempty"`
	Protocol     *int    `json:"protocol,omitempty"`
	ID           *uint16 `json:"id,omitempty"`
	Offset8      uint16  `json:"offset8,omitempty"`
	More         bool    `json:"more,omitempty"`
	PayloadB64   string  `json:"payload_base64,omitempty"`
}

func (s *Service) handleFragment(w http.ResponseWriter, r *http.Request) {
	var req fragmentRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_json", "请求体不是合法 JSON: "+err.Error())
		return
	}

	pkt, err := decodePacket(&req)
	if err != nil {
		kind := "invalid_packet"
		if pe, ok := err.(*netmodel.ParseError); ok {
			kind = string(pe.Kind)
		}
		writeError(w, reasm.HTTPStatus(err), kind, err.Error())
		return
	}

	out, perr := s.Asm.Process(r.Context(), pkt)
	if perr != nil {
		kind := "reassembly_error"
		if e, ok := reasm.AsError(perr); ok {
			kind = string(e.Kind)
		}
		if pe, ok := perr.(*netmodel.ParseError); ok {
			kind = string(pe.Kind)
		}
		writeError(w, reasm.HTTPStatus(perr), kind, perr.Error())
		return
	}
	status := http.StatusAccepted
	if out.State == reasm.StateComplete {
		status = http.StatusCreated
	}
	if out.Duplicate {
		status = http.StatusOK
	}
	writeJSON(w, status, outcomeJSON(out))
}

func decodePacket(req *fragmentRequest) (*netmodel.Packet, error) {
	if req.PacketBase64 != "" {
		raw, err := base64.StdEncoding.DecodeString(req.PacketBase64)
		if err != nil {
			return nil, &netmodel.ParseError{Kind: netmodel.ErrTruncated,
				Detail: "packet_base64 解码失败: " + err.Error()}
		}
		return netmodel.ParseIPv4(raw)
	}
	// 手工字段模式：构造最小首部（无选项），供自动化测试使用。
	if req.Src == "" || req.Dst == "" || req.Protocol == nil || req.ID == nil {
		return nil, &netmodel.ParseError{Kind: netmodel.ErrTruncated,
			Detail: "需提供 packet_base64，或同时提供 src/dst/protocol/id/payload_base64"}
	}
	src, err := netip.ParseAddr(req.Src)
	if err != nil {
		return nil, &netmodel.ParseError{Kind: netmodel.ErrBadVersion, Detail: "src 非 IP: " + err.Error()}
	}
	dst, err := netip.ParseAddr(req.Dst)
	if err != nil {
		return nil, &netmodel.ParseError{Kind: netmodel.ErrBadVersion, Detail: "dst 非 IP: " + err.Error()}
	}
	payload, err := base64.StdEncoding.DecodeString(req.PayloadB64)
	if err != nil {
		return nil, &netmodel.ParseError{Kind: netmodel.ErrTruncated, Detail: "payload_base64 解码失败: " + err.Error()}
	}
	raw := buildMinimalIPv4(src, dst, uint8(*req.Protocol), *req.ID, req.Offset8, req.More, payload)
	return netmodel.ParseIPv4(raw)
}

// buildMinimalIPv4 仅用于 API 的手工字段便捷模式，与 fixture 包逻辑一致但单独维护。
func buildMinimalIPv4(src, dst netip.Addr, proto uint8, id uint16, off8 uint16, more bool, payload []byte) []byte {
	const ihl = 20
	b := make([]byte, ihl+len(payload))
	b[0] = 0x45
	b[2] = byte((ihl + len(payload)) >> 8)
	b[3] = byte(ihl + len(payload))
	b[4] = byte(id >> 8)
	b[5] = byte(id)
	var fo uint16 = off8 & netmodel.OffsetMask
	if more {
		fo |= netmodel.FlagMoreFragments
	}
	b[6] = byte(fo >> 8)
	b[7] = byte(fo)
	b[8] = 64
	b[9] = proto
	sa, da := src.As4(), dst.As4()
	copy(b[12:16], sa[:])
	copy(b[16:20], da[:])
	copy(b[20:], payload)
	csum := netmodel.Checksum(b[:ihl])
	b[10] = byte(csum >> 8)
	b[11] = byte(csum)
	return b
}

func outcomeJSON(o *reasm.Outcome) map[string]any {
	resp := map[string]any{
		"key":       o.KeyText,
		"state":     string(o.State),
		"accepted":  o.Accepted,
		"duplicate": o.Duplicate,
		"progress":  o.Progress,
		"deadline":  o.Deadline.Format(time.RFC3339Nano),
	}
	if o.Reason != "" {
		resp["reason"] = o.Reason
		resp["detail"] = o.Detail
	}
	if len(o.Assembled) > 0 {
		resp["assembled_base64"] = base64.StdEncoding.EncodeToString(o.Assembled)
		resp["length"] = len(o.Assembled)
	}
	if o.ExpiresAt != nil {
		resp["expires_at"] = o.ExpiresAt.Format(time.RFC3339Nano)
	}
	return resp
}

func (s *Service) handleReplayPCAP(w http.ResponseWriter, r *http.Request) {
	var body []byte
	var err error
	if r.Body != nil {
		body, err = readLimited(r.Body)
		if err != nil {
			writeError(w, http.StatusRequestEntityTooLarge, "too_large", err.Error())
			return
		}
	}
	if len(body) == 0 {
		writeError(w, http.StatusBadRequest, "empty_body", "需要提供 pcap 字节（application/vnd.tcpdump.pcap）")
		return
	}
	rep := replay.New(s.Asm)
	report, err := rep.Run(r.Context(), bytes.NewReader(body), time.Time{}, nil)
	if err != nil {
		status := http.StatusBadRequest
		if !errors.Is(err, replay.ErrBadPCAP) {
			status = http.StatusInternalServerError
		}
		writeError(w, status, "pcap_error", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, report)
}

func (s *Service) handleGetGroup(w http.ResponseWriter, r *http.Request) {
	keyText := strings.TrimPrefix(r.URL.Path, "/api/v1/groups/")
	key, err := parseKeyPath(keyText)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_key", err.Error())
		return
	}
	snap, err := s.Asm.Lookup(r.Context(), key)
	if err != nil {
		writeError(w, reasm.HTTPStatus(err), "lookup_failed", err.Error())
		return
	}
	resp := map[string]any{
		"key": snap.KeyText, "state": string(snap.State), "progress": snap.Progress,
		"deadline": snap.Deadline.Format(time.RFC3339Nano),
	}
	if snap.Reason != "" {
		resp["reason"] = snap.Reason
	}
	if len(snap.Assembled) > 0 {
		resp["assembled_base64"] = base64.StdEncoding.EncodeToString(snap.Assembled)
		resp["length"] = len(snap.Assembled)
	}
	if snap.ExpiresAt != nil {
		resp["expires_at"] = snap.ExpiresAt.Format(time.RFC3339Nano)
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Service) handleDeleteGroup(w http.ResponseWriter, r *http.Request) {
	keyText := strings.TrimPrefix(r.URL.Path, "/api/v1/groups/")
	key, err := parseKeyPath(keyText)
	if err != nil {
		writeError(w, http.StatusBadRequest, "bad_key", err.Error())
		return
	}
	if err := s.Asm.Purge(r.Context(), key); err != nil {
		writeError(w, http.StatusInternalServerError, "purge_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"key": keyText, "purged": "true"})
}

func (s *Service) handleStats(w http.ResponseWriter, r *http.Request) {
	st, err := s.Asm.Stats(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "stats_failed", err.Error())
		return
	}
	writeJSON(w, http.StatusOK, st)
}

// parseKeyPath 解析形如 src->dst/proto=6/id=1234 的规范键。
func parseKeyPath(text string) (netmodel.FragKey, error) {
	// URL 中 '>' 一般被转义为 %3E，net/http 已还原。
	var k netmodel.FragKey
	parts := strings.SplitN(text, "/", 3)
	if len(parts) != 3 || !strings.Contains(parts[0], "->") {
		return k, errors.New("键路径需形如 /api/v1/groups/1.2.3.4->5.6.7.8/proto=6/id=1234")
	}
	addrs := strings.SplitN(parts[0], "->", 2)
	src, err := netip.ParseAddr(addrs[0])
	if err != nil {
		return k, err
	}
	dst, err := netip.ParseAddr(addrs[1])
	if err != nil {
		return k, err
	}
	var proto int
	var id uint64
	for _, p := range parts[1:] {
		if v, ok := strings.CutPrefix(p, "proto="); ok {
			if _, err := fmtSscan(v, &proto); err != nil {
				return k, err
			}
		}
		if v, ok := strings.CutPrefix(p, "id="); ok {
			if _, err := fmtSscan(v, &id); err != nil {
				return k, err
			}
		}
	}
	if proto == 0 && !strings.Contains(text, "proto=") {
		return k, errors.New("缺少 proto")
	}
	return netmodel.FragKey{Src: src, Dst: dst, Protocol: netmodel.Protocol(byte(proto)), ID: uint16(id)}, nil
}

func readLimited(r interface{ Read([]byte) (int, error) }) ([]byte, error) {
	return readAllLimit(r, 16<<20) // 16 MiB 夹具上限
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeError(w http.ResponseWriter, status int, kind, msg string) {
	writeJSON(w, status, map[string]string{"error": kind, "message": msg})
}
