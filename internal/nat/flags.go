package nat

import "strings"

// tcpFlags is the parsed flag set relevant to the state machine.
type tcpFlags struct {
	SYN, ACK, FIN, RST bool
}

// parseTCPFlags accepts a "+"-separated case-insensitive flag set containing
// only the modeled flags. Returns "" on success or a human reason.
func parseTCPFlags(s string) (tcpFlags, string) {
	var f tcpFlags
	for _, tok := range strings.Split(s, "+") {
		switch strings.ToUpper(strings.TrimSpace(tok)) {
		case "SYN":
			if f.SYN {
				return f, "duplicate SYN in flags"
			}
			f.SYN = true
		case "ACK":
			f.ACK = true
		case "FIN":
			f.FIN = true
		case "RST":
			f.RST = true
		case "":
			return f, "empty flag token"
		default:
			return f, "unsupported TCP flag: " + tok
		}
	}
	if f.SYN && (f.FIN || f.RST) {
		return f, "SYN may not combine with FIN or RST in this model"
	}
	if f.RST && f.FIN {
		return f, "RST may not combine with FIN"
	}
	return f, ""
}
