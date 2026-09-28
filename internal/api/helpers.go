package api

import (
	"errors"
	"io"
	"log"
	"net/http"
	"strconv"
)

func fmtSscan(s string, target any) (int, error) {
	switch p := target.(type) {
	case *int:
		v, err := strconv.Atoi(s)
		if err != nil {
			return 0, err
		}
		*p = v
		return 1, nil
	case *uint64:
		v, err := strconv.ParseUint(s, 10, 64)
		if err != nil {
			return 0, err
		}
		*p = v
		return 1, nil
	}
	return 0, errors.New("不支持的扫描目标")
}

func readAllLimit(r interface{ Read([]byte) (int, error) }, limit int) ([]byte, error) {
	lr := io.LimitReader(r.(io.Reader), int64(limit)+1)
	b, err := io.ReadAll(lr)
	if err != nil {
		return nil, err
	}
	if len(b) > limit {
		return nil, errors.New("请求体超过 16MiB 夹具上限")
	}
	return b, nil
}

// statusWriter 捕获状态码用于访问日志。
type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

func logRequest(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)
		log.Printf("http %d %s %s", sw.status, r.Method, r.URL.Path)
	})
}
