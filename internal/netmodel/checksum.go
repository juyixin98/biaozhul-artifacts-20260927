package netmodel

// Checksum 计算 RFC 1071 互联网校验和，供校验与合成夹具分别使用。
// data 长度为奇数时按尾部补 0 处理。
func Checksum(data []byte) uint16 {
	var sum uint32
	for len(data) > 1 {
		sum += uint32(data[0])<<8 | uint32(data[1])
		data = data[2:]
	}
	if len(data) == 1 {
		sum += uint32(data[0]) << 8
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return ^uint16(sum)
}

// VerifyChecksum 校验 IPv4 首部校验和：把含首部校验和字段的整个首部参与求和应得 0。
func VerifyChecksum(header []byte) bool {
	if len(header) < 20 || len(header)%2 != 0 {
		return false
	}
	var sum uint32
	for i := 0; i < len(header); i += 2 {
		sum += uint32(header[i])<<8 | uint32(header[i+1])
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return uint16(sum) == 0xffff
}
