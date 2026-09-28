package ribd_test

import (
	"fmt"
	"strconv"
)

func fmtSprintf(format string, args ...any) string { return fmt.Sprintf(format, args...) }

func itoa(i int) string { return strconv.Itoa(i) }
