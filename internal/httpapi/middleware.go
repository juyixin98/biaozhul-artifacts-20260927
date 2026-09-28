package httpapi

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
)

type ctxKey string

const requestIDKey ctxKey = "request_id"

// requestID 为每个请求分配/透传标识，并回写 X-Request-ID。
func (s *Server) requestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get("X-Request-ID")
		if id == "" {
			id = newRequestID()
		}
		w.Header().Set("X-Request-ID", id)
		ctx := context.WithValue(r.Context(), requestIDKey, id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// recoverPanic 把处理器 panic 转成 500，避免连接裸断；日志带 request_id。
func (s *Server) recoverPanic(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				s.Log.Slog().Error("panic recovered",
					"request_id", requestIDFromCtx(r.Context()),
					"panic", rec,
					"path", r.URL.Path)
				s.writeError(w, r, http.StatusInternalServerError, CodeInternal, "内部错误")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

func requestIDFromCtx(ctx context.Context) string {
	if v, ok := ctx.Value(requestIDKey).(string); ok {
		return v
	}
	return "-"
}

func newRequestID() string {
	var b [12]byte
	if _, err := rand.Read(b[:]); err != nil {
		// rand 失败极不寻常；退回时间无关的固定前缀仍可关联日志。
		return "req-unknown"
	}
	return "req-" + hex.EncodeToString(b[:])
}
