// Package pcap implements a minimal reader and writer for the classic
// libpcap file format, plus link-layer extraction of IPv4 packets.
// It is used both by the offline replay engine and by the fixture
// generator, so fixtures and the verifier share exactly one parser.
package pcap

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"time"

	"ipreasm/internal/ipv4"
)

// Link types we understand.
const (
	LinkEthernet uint32 = 1
	LinkRaw      uint32 = 101 // raw IP, no link-layer header
)

var (
	ErrBadMagic  = errors.New("not a classic pcap file (bad magic)")
	ErrBadHeader = errors.New("truncated pcap header")
	ErrBadRecord = errors.New("truncated pcap record")
	ErrNoIPv4    = errors.New("frame does not carry IPv4")
	ErrLinkType  = errors.New("unsupported link type")
)

const (
	magicUsecLE = 0xa1b2c3d4
	magicNsecLE = 0xa1b23c4d
)

// Record is one captured packet.
type Record struct {
	TS   time.Time
	Data []byte
}

// Reader iterates records of a classic pcap file.
type Reader struct {
	r        io.Reader
	bo       binary.ByteOrder
	nano     bool
	LinkType uint32
	SnapLen  uint32
}

// NewReader consumes the 24-byte global header.
func NewReader(r io.Reader) (*Reader, error) {
	var hdr [24]byte
	if _, err := io.ReadFull(r, hdr[:]); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrBadHeader, err)
	}
	rd := &Reader{}
	switch binary.LittleEndian.Uint32(hdr[0:4]) {
	case magicUsecLE:
		rd.bo = binary.LittleEndian
	case magicNsecLE:
		rd.bo = binary.LittleEndian
		rd.nano = true
	case 0xd4c3b2a1: // big-endian, microsecond
		rd.bo = binary.BigEndian
	case 0x4d3cb2a1: // big-endian, nanosecond
		rd.bo = binary.BigEndian
		rd.nano = true
	default:
		return nil, fmt.Errorf("%w: 0x%08x", ErrBadMagic, binary.LittleEndian.Uint32(hdr[0:4]))
	}
	rd.SnapLen = rd.bo.Uint32(hdr[16:20])
	rd.LinkType = rd.bo.Uint32(hdr[20:24])
	rd.r = r
	return rd, nil
}

// Next returns the next record, or io.EOF at end of file.
func (rd *Reader) Next() (Record, error) {
	var rh [16]byte
	if _, err := io.ReadFull(rd.r, rh[:]); err != nil {
		if err == io.EOF || err == io.ErrUnexpectedEOF {
			return Record{}, io.EOF
		}
		return Record{}, err
	}
	sec := rd.bo.Uint32(rh[0:4])
	frac := rd.bo.Uint32(rh[4:8])
	incl := rd.bo.Uint32(rh[8:12])
	if incl > 1<<24 { // 16 MiB sanity bound per record
		return Record{}, fmt.Errorf("%w: captured length %d", ErrBadRecord, incl)
	}
	data := make([]byte, incl)
	if _, err := io.ReadFull(rd.r, data); err != nil {
		return Record{}, fmt.Errorf("%w: %v", ErrBadRecord, err)
	}
	var ts time.Time
	if rd.nano {
		ts = time.Unix(int64(sec), int64(frac)).UTC()
	} else {
		ts = time.Unix(int64(sec), int64(frac)*1000).UTC()
	}
	return Record{TS: ts, Data: data}, nil
}

// Writer emits classic little-endian microsecond pcap files.
type Writer struct {
	w io.Writer
}

// NewWriter writes the global header for the given link type.
func NewWriter(w io.Writer, linkType uint32) (*Writer, error) {
	var hdr [24]byte
	binary.LittleEndian.PutUint32(hdr[0:4], magicUsecLE)
	binary.LittleEndian.PutUint16(hdr[4:6], 2) // version major
	binary.LittleEndian.PutUint16(hdr[6:8], 4) // version minor
	binary.LittleEndian.PutUint32(hdr[16:20], 65535)
	binary.LittleEndian.PutUint32(hdr[20:24], linkType)
	if _, err := w.Write(hdr[:]); err != nil {
		return nil, err
	}
	return &Writer{w: w}, nil
}

// WriteRecord appends one packet.
func (wr *Writer) WriteRecord(ts time.Time, data []byte) error {
	var rh [16]byte
	binary.LittleEndian.PutUint32(rh[0:4], uint32(ts.Unix()))
	binary.LittleEndian.PutUint32(rh[4:8], uint32(ts.Nanosecond()/1000))
	binary.LittleEndian.PutUint32(rh[8:12], uint32(len(data)))
	binary.LittleEndian.PutUint32(rh[12:16], uint32(len(data)))
	if _, err := wr.w.Write(rh[:]); err != nil {
		return err
	}
	_, err := wr.w.Write(data)
	return err
}

// ExtractIPv4 strips the link-layer header and parses the IPv4 packet.
func ExtractIPv4(linkType uint32, frame []byte) (*ipv4.Packet, error) {
	switch linkType {
	case LinkRaw:
		return ipv4.Parse(frame)
	case LinkEthernet:
		if len(frame) < 14 {
			return nil, fmt.Errorf("%w: ethernet frame of %d bytes", ErrNoIPv4, len(frame))
		}
		eth := binary.BigEndian.Uint16(frame[12:14])
		off := 14
		for eth == 0x8100 || eth == 0x88a8 { // 802.1Q / QinQ tags
			if len(frame) < off+4 {
				return nil, fmt.Errorf("%w: truncated VLAN tag", ErrNoIPv4)
			}
			eth = binary.BigEndian.Uint16(frame[off+2 : off+4])
			off += 4
		}
		if eth != 0x0800 {
			return nil, fmt.Errorf("%w: ethertype 0x%04x", ErrNoIPv4, eth)
		}
		return ipv4.Parse(frame[off:])
	default:
		return nil, fmt.Errorf("%w: %d", ErrLinkType, linkType)
	}
}
