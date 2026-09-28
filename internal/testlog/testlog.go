// Package testlog 为测试提供可关联“输入/运行身份”的结构化日志，
// 并把每条判定落为 JSONL 报告（默认目录 test-results，可用
// REASM_TEST_REPORT_DIR 覆盖）。失败/未知状态显式标记，绝不统一记为成功。
package testlog

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"ipfragreasm/internal/version"
)

// Level 表示日志级别。
type Level string

const (
	LevelInfo Level = "INFO"
	LevelStep Level = "STEP"
	LevelPass Level = "PASS"
	LevelFail Level = "FAIL"
	LevelWarn Level = "WARN"
	LevelSkip Level = "SKIP"
)

// Logger 绑定一个运行身份并负责 JSONL 落盘。
type Logger struct {
	mu    sync.Mutex
	tb    testing.TB
	runID string
	suite string
	file  io.WriteCloser
	enc   *json.Encoder
	seq   int
}

// Entry 是报告中的一行。
type Entry struct {
	Seq      int    `json:"seq"`
	RunID    string `json:"run_id"`
	Suite    string `json:"suite"`
	Time     string `json:"time"`
	Level    Level  `json:"level"`
	CaseName string `json:"case,omitempty"`
	InputID  string `json:"input_id,omitempty"`
	Message  string `json:"message"`
	Detail   any    `json:"detail,omitempty"`
	Version  string `json:"version"`
	Go       string `json:"go"`
}

// New 为某个测试套件创建 logger，并写出报告头。
func New(tb testing.TB, suite string) *Logger {
	tb.Helper()
	runID := newRunID()
	dir := os.Getenv("REASM_TEST_REPORT_DIR")
	if dir == "" {
		// 从测试工作目录向上找到 go.mod，使所有包的报告汇聚到工程根 test-results/。
		if root, ok := moduleRoot(); ok {
			dir = filepath.Join(root, "test-results")
		} else {
			dir = "test-results"
		}
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		tb.Logf("testlog: 创建报告目录失败: %v", err)
	}
	path := filepath.Join(dir, fmt.Sprintf("%s-%s.jsonl",
		strings.ReplaceAll(suite, "/", "_"), strings.ReplaceAll(runID, ":", "-")))
	f, err := os.Create(path)
	if err != nil {
		tb.Logf("testlog: 创建报告文件失败: %v", err)
	}

	l := &Logger{tb: tb, runID: runID, suite: suite, file: f}
	if f != nil {
		l.enc = json.NewEncoder(f)
	}
	l.Header()
	tb.Cleanup(func() {
		if f != nil {
			f.Close()
			tb.Logf("[testlog] 运行 %s 的 JSONL 报告: %s", runID, path)
		}
	})
	return l
}

// RunID 返回运行身份。
func (l *Logger) RunID() string { return l.runID }

// Header 记录版本/平台等环境事实。
func (l *Logger) Header() {
	l.emit(LevelInfo, "", "", "测试运行开始", map[string]any{
		"algorithm": version.Algorithm,
		"platform":  runtime.GOOS + "/" + runtime.GOARCH,
	})
}

// Step 记录计算步骤。
func (l *Logger) Step(caseName, inputID, format string, args ...any) {
	l.emit(LevelStep, caseName, inputID, fmt.Sprintf(format, args...), nil)
}

// Pass 记录成功判定，basis 为判定依据。
func (l *Logger) Pass(caseName, inputID, message string, basis any) {
	l.emit(LevelPass, caseName, inputID, message, basis)
}

// Fail 记录失败判定（不终止测试，仅记录；配合 t.Errorf 使用）。
func (l *Logger) Fail(caseName, inputID, message string, basis any) {
	l.emit(LevelFail, caseName, inputID, message, basis)
}

// Warn 记录未知/异常但未直接致失败的事实。
func (l *Logger) Warn(caseName, inputID, format string, args ...any) {
	l.emit(LevelWarn, caseName, inputID, fmt.Sprintf(format, args...), nil)
}

// Info 记录一般信息。
func (l *Logger) Info(caseName, inputID, format string, args ...any) {
	l.emit(LevelInfo, caseName, inputID, fmt.Sprintf(format, args...), nil)
}

func (l *Logger) emit(level Level, caseName, inputID, msg string, detail any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.seq++
	e := Entry{
		Seq: l.seq, RunID: l.runID, Suite: l.suite,
		Time:  time.Now().UTC().Format(time.RFC3339Nano),
		Level: level, CaseName: caseName, InputID: inputID,
		Message: msg, Detail: detail,
		Version: version.Version, Go: runtime.Version(),
	}
	if l.enc != nil {
		_ = l.enc.Encode(e)
	}
	if l.tb != nil {
		l.tb.Logf("[%s] run=%s case=%s input=%s %s", level, l.runID, caseName, inputID, msg)
	}
}

func newRunID() string {
	return fmt.Sprintf("run-%d-%d", time.Now().UnixNano(), time.Now().UnixNano()%100000)
}

// moduleRoot 从当前工作目录向上查找包含 go.mod 的目录。
func moduleRoot() (string, bool) {
	dir, err := os.Getwd()
	if err != nil {
		return "", false
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, "go.mod")); err == nil {
			return dir, true
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return "", false
		}
		dir = parent
	}
}
