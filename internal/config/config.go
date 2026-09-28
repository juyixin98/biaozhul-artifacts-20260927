// Package config 负责解析离线重组后端的配置。
//
// 配置来源优先级（高到低）：环境变量 REASM_* > JSON 配置文件 > 内置默认值。
// 不依赖任何第三方 YAML/TOML 库，保持工程精简与可离线构建。
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// Config 为重组后端的全部可调参数。
type Config struct {
	// Listen 是 HTTP 回放接口监听地址，默认仅绑定回环地址，绝不主动发送网络报文。
	Listen string `json:"listen"`

	// SQLiteDSN 为状态存储 DSN；空串表示使用内存存储。
	SQLiteDSN string `json:"sqlite_dsn"`

	// ReassembleTimeout 是单个重组组自“首个分片到达”起的明确超时。
	ReassembleTimeout Duration `json:"reassemble_timeout"`

	// ResultTTL 是已终结（完成/拒绝/超时）组在存储中保留供查询/审计的时长。
	ResultTTL Duration `json:"result_ttl"`

	// MaxDatagramBytes 为重组数据报总载荷的硬上限，标准上限 65535。
	MaxDatagramBytes int `json:"max_datagram_bytes"`

	// SweepInterval 是后台回收扫描间隔；<=0 表示关闭后台扫描（测试中可手动 Sweep）。
	SweepInterval Duration `json:"sweep_interval"`
}

// Duration 包装 time.Duration，使其能以 "30s"/"2m" 的 JSON 字符串形式配置。
type Duration time.Duration

// Duration 还原为标准库类型。
func (d Duration) Duration() time.Duration { return time.Duration(d) }

// MarshalJSON 输出字符串形式。
func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(time.Duration(d).String())
}

// UnmarshalJSON 接受 "30s" 字符串或纯数字（按纳秒解释）。
func (d *Duration) UnmarshalJSON(raw []byte) error {
	s := strings.Trim(string(raw), `"`)
	if s == "" || s == "null" {
		return nil
	}
	if n, err := strconv.ParseInt(s, 10, 64); err == nil {
		*d = Duration(n)
		return nil
	}
	parsed, err := time.ParseDuration(s)
	if err != nil {
		return fmt.Errorf("duration %q: %w", s, err)
	}
	*d = Duration(parsed)
	return nil
}

// Default 返回内置默认配置。
func Default() Config {
	return Config{
		Listen:            "127.0.0.1:8080",
		SQLiteDSN:         "file:data/reasm.sqlite?cache=shared",
		ReassembleTimeout: Duration(30 * time.Second),
		ResultTTL:         Duration(5 * time.Minute),
		MaxDatagramBytes:  65535,
		SweepInterval:     Duration(250 * time.Millisecond),
	}
}

// Load 从 path（可为空串）读取 JSON 配置，再叠加 REASM_* 环境变量。
// 文件不存在不视为错误（适用于完全依赖默认值/环境变量的场景）。
func Load(path string) (Config, error) {
	cfg := Default()

	if path != "" {
		raw, err := os.ReadFile(path)
		switch {
		case err == nil:
			if err := json.Unmarshal(raw, &cfg); err != nil {
				return Config{}, fmt.Errorf("parse config %s: %w", path, err)
			}
		case errors.Is(err, os.ErrNotExist):
			// 允许缺省配置文件，继续走环境变量与默认值。
		default:
			return Config{}, fmt.Errorf("read config %s: %w", path, err)
		}
	}

	if err := applyEnv(&cfg); err != nil {
		return Config{}, err
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// applyEnv 将 REASM_* 环境变量叠加到配置上。
func applyEnv(cfg *Config) error {
	type binding struct {
		key string
		set func(string) error
	}
	bindings := []binding{
		{"REASM_LISTEN", func(v string) error { cfg.Listen = v; return nil }},
		{"REASM_SQLITE_DSN", func(v string) error { cfg.SQLiteDSN = v; return nil }},
		{"REASM_REASSEMBLE_TIMEOUT", func(v string) (err error) {
			cfg.ReassembleTimeout, err = parseDurationEnv(v)
			return err
		}},
		{"REASM_RESULT_TTL", func(v string) (err error) {
			cfg.ResultTTL, err = parseDurationEnv(v)
			return err
		}},
		{"REASM_SWEEP_INTERVAL", func(v string) (err error) {
			cfg.SweepInterval, err = parseDurationEnv(v)
			return err
		}},
		{"REASM_MAX_DATAGRAM_BYTES", func(v string) error {
			n, err := strconv.Atoi(v)
			if err != nil {
				return fmt.Errorf("REASM_MAX_DATAGRAM_BYTES=%q: %w", v, err)
			}
			cfg.MaxDatagramBytes = n
			return nil
		}},
	}
	for _, b := range bindings {
		if v, ok := os.LookupEnv(b.key); ok {
			if err := b.set(v); err != nil {
				return err
			}
		}
	}
	return nil
}

func parseDurationEnv(v string) (Duration, error) {
	if n, err := strconv.ParseInt(v, 10, 64); err == nil {
		return Duration(n), nil
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return 0, fmt.Errorf("invalid duration %q: %w", v, err)
	}
	return Duration(d), nil
}

// Validate 校验配置取值范围。
func (c Config) Validate() error {
	var problems []string
	if c.Listen == "" {
		problems = append(problems, "listen 不能为空")
	}
	if c.ReassembleTimeout.Duration() <= 0 {
		problems = append(problems, "reassemble_timeout 必须为正")
	}
	if c.ResultTTL.Duration() < 0 {
		problems = append(problems, "result_ttl 不能为负")
	}
	if c.MaxDatagramBytes <= 0 || c.MaxDatagramBytes > 65535 {
		problems = append(problems, "max_datagram_bytes 必须在 1..65535 之间（IPv4 数据报受 16 位总长约束）")
	}
	if c.SweepInterval.Duration() < 0 {
		problems = append(problems, "sweep_interval 不能为负")
	}
	if len(problems) > 0 {
		return fmt.Errorf("配置校验失败: %s", strings.Join(problems, "; "))
	}
	return nil
}
