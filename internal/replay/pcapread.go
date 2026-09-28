package replay

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"time"
)

// pcap 魔数（字节序与纳秒/微秒变体）。
const (
	magicMicroLE = 0xa1b2c3d4
	magicMicroBE = 0xd4c3b2a1
	magicNanoLE  = 0xa1b23c4d
	magicNanoBE  = 0x4d3cb2a1
)

// ErrBadPCAP 表示文件不是受支持的经典 pcap。
var ErrBadPCAP = errors.New("bad pcap file")

// RawRecord 是一条 pcap 记录的原始字节与捕获时间。
type RawRecord struct {
	TS    time.Time
	Bytes []byte
}

// ReadPCAP 解析经典 pcap（micro/nano、LE/BE 均可），逐条返回记录。
// 不做抓包，只读本地夹具字节。linkType 在首条记录前即已解析，
// 因此连同记录一起交给回调。
func ReadPCAP(r io.Reader, emit func(linkType uint32, rec RawRecord) error) error {
	var magicBuf [4]byte
	if _, err := io.ReadFull(r, magicBuf[:]); err != nil {
		return fmt.Errorf("%w: 读取魔数失败: %v", ErrBadPCAP, err)
	}
	magic := binary.LittleEndian.Uint32(magicBuf[:])

	var (
		bo   binary.ByteOrder = binary.LittleEndian
		nano bool
	)
	switch magic {
	case magicMicroLE:
	case magicNanoLE:
		nano = true
	case magicMicroBE, magicNanoBE:
		bo = binary.BigEndian
		nano = magic == magicNanoBE
	default:
		return fmt.Errorf("%w: 未知魔数 0x%08x", ErrBadPCAP, magic)
	}

	var rest [20]byte
	if _, err := io.ReadFull(r, rest[:]); err != nil {
		return fmt.Errorf("%w: 全局首部不完整: %v", ErrBadPCAP, err)
	}
	major := bo.Uint16(rest[0:2])
	minor := bo.Uint16(rest[2:4])
	link := bo.Uint32(rest[16:20])
	if major != 2 || minor != 4 {
		return fmt.Errorf("%w: 不支持的 pcap 版本 %d.%d（仅 2.4）", ErrBadPCAP, major, minor)
	}

	var hdr [16]byte
	idx := 0
	for {
		if _, err := io.ReadFull(r, hdr[:]); err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			if errors.Is(err, io.ErrUnexpectedEOF) {
				return fmt.Errorf("%w: 第 %d 条记录首部被截断", ErrBadPCAP, idx)
			}
			return err
		}
		sec := bo.Uint32(hdr[0:4])
		frac := bo.Uint32(hdr[4:8])
		capLen := bo.Uint32(hdr[8:12])
		origLen := bo.Uint32(hdr[12:16])
		if capLen > 8*1024*1024 {
			return fmt.Errorf("%w: 第 %d 条记录 snaplen=%d 异常", ErrBadPCAP, idx, capLen)
		}

		buf := make([]byte, capLen)
		if _, err := io.ReadFull(r, buf); err != nil {
			return fmt.Errorf("%w: 第 %d 条记录数据不完整: %v", ErrBadPCAP, idx, err)
		}
		var ts time.Time
		if nano {
			ts = time.Unix(int64(sec), int64(frac))
		} else {
			ts = time.Unix(int64(sec), int64(frac)*1000)
		}
		if err := emit(link, RawRecord{TS: ts, Bytes: buf}); err != nil {
			return err
		}
		_ = origLen
		idx++
	}
}
