package fixture

import (
	"encoding/binary"
	"io"
	"time"
)

// pcap 常量（little-endian / microsecond 经典格式，tcpdump 可直接读取）。
const (
	pcapMagicMicro = 0xa1b2c3d4
	pcapVersionMaj = 2
	pcapVersionMin = 4
)

// Frame 是一条 pcap 记录：时间戳 + 链路层帧。
type Frame struct {
	TS    time.Time
	Frame []byte
}

// EthernetFrame 用给定 ethertype（IPv4=0x0800）封装一帧。
func EthernetFrame(ipv4 []byte) []byte {
	frame := make([]byte, 14+len(ipv4))
	// dst=02:00:00:00:00:01 src=02:00:00:00:00:02
	copy(frame[0:6], []byte{0x02, 0x00, 0x00, 0x00, 0x00, 0x01})
	copy(frame[6:12], []byte{0x02, 0x00, 0x00, 0x00, 0x00, 0x02})
	binary.BigEndian.PutUint16(frame[12:14], 0x0800)
	copy(frame[14:], ipv4)
	return frame
}

// LoopbackFrame 以 BSD loopback（family=AF_INET=2，LE）封装。
func LoopbackFrame(ipv4 []byte) []byte {
	frame := make([]byte, 4+len(ipv4))
	frame[0] = 0x02
	copy(frame[4:], ipv4)
	return frame
}

// WritePCAP 以小端经典 pcap 写出全部帧。linkType 取 netmodel.LinkType*。
func WritePCAP(w io.Writer, linkType uint32, frames []Frame) error {
	var gh [24]byte
	binary.LittleEndian.PutUint32(gh[0:4], pcapMagicMicro)
	binary.LittleEndian.PutUint16(gh[4:6], pcapVersionMaj)
	binary.LittleEndian.PutUint16(gh[6:8], pcapVersionMin)
	// thiszone(4)=0 sigfigs(4)=0
	binary.LittleEndian.PutUint32(gh[16:20], 65535) // snaplen
	binary.LittleEndian.PutUint32(gh[20:24], linkType)
	if _, err := w.Write(gh[:]); err != nil {
		return err
	}

	var rh [16]byte
	for _, f := range frames {
		ts := f.TS
		if ts.IsZero() {
			ts = time.Unix(0, 0)
		}
		binary.LittleEndian.PutUint32(rh[0:4], uint32(ts.Unix()))
		binary.LittleEndian.PutUint32(rh[4:8], uint32(ts.Nanosecond()/1000))
		binary.LittleEndian.PutUint32(rh[8:12], uint32(len(f.Frame)))
		binary.LittleEndian.PutUint32(rh[12:16], uint32(len(f.Frame)))
		if _, err := w.Write(rh[:]); err != nil {
			return err
		}
		if _, err := w.Write(f.Frame); err != nil {
			return err
		}
	}
	return nil
}
