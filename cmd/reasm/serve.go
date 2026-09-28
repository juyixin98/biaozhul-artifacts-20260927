package main

import (
	"context"
	"net/http"
	"time"

	"ipfragreasm/internal/reasm"
)

// httpServer 是对 net/http 服务器的薄封装，便于测试替换。
type httpServer struct {
	addr    string
	handler http.Handler
}

func (s *httpServer) listenAndServe() error {
	srv := &http.Server{
		Addr:              s.addr,
		Handler:           s.handler,
		ReadHeaderTimeout: 5 * time.Second,
	}
	return srv.ListenAndServe()
}

// startSweeper 周期性执行超时/留存回收，返回停止函数。
func startSweeper(ctx context.Context, asm *reasm.Assembler, interval time.Duration) func() {
	stop := make(chan struct{})
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-stop:
				return
			case <-ticker.C:
				if _, err := asm.Sweep(context.Background()); err != nil {
					// 回收失败不致命：下轮重试；绝不伪装成功。
					continue
				}
			}
		}
	}()
	return func() { close(stop) }
}
