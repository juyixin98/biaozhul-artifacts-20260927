// Package config 负责服务配置的加载与校验：JSON 文件 + 环境变量覆盖。
// 不引入第三方配置库，保持依赖面最小。
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Config 是全部可调参数。零值字段在 Load 中由 Defaults 填充。
type Config struct {
	ListenAddr  string `json:"listen_addr"`
	SQLiteDSN   string `json:"sqlite_dsn"`
	MaxDepth    int    `json:"max_recursion_depth"`
	RedactDiag  bool   `json:"redact_diagnostics"`
	LogLevel    string `json:"log_level"`
	ReplayLimit int    `json:"replay_limit"`
}

// Defaults 返回带默认值的配置。
func Defaults() Config {
	return Config{
		ListenAddr:  "127.0.0.1:8080",
		SQLiteDSN:   "file:rib.db?cache=shared&_pragma=busy_timeout(5000)",
		MaxDepth:    16,
		RedactDiag:  true,
		LogLevel:    "info",
		ReplayLimit: 10000,
	}
}

// Load 读取 path（可为空字符串，表示全默认），再用 RIB_ 前缀的环境变量
// 覆盖标量字段，最后校验范围。
func Load(path string) (Config, error) {
	cfg := Defaults()
	if path != "" {
		raw, err := os.ReadFile(path)
		if err != nil {
			return cfg, fmt.Errorf("read config %s: %w", path, err)
		}
		if err := json.Unmarshal(raw, &cfg); err != nil {
			return cfg, fmt.Errorf("parse config %s: %w", path, err)
		}
	}
	applyEnv(&cfg)
	if err := cfg.Validate(); err != nil {
		return cfg, err
	}
	return cfg, nil
}

func applyEnv(cfg *Config) {
	if v, ok := os.LookupEnv("RIB_LISTEN_ADDR"); ok {
		cfg.ListenAddr = v
	}
	if v, ok := os.LookupEnv("RIB_SQLITE_DSN"); ok {
		cfg.SQLiteDSN = v
	}
	if v, ok := os.LookupEnv("RIB_MAX_DEPTH"); ok {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.MaxDepth = n
		}
	}
	if v, ok := os.LookupEnv("RIB_REDACT_DIAG"); ok {
		if b, err := strconv.ParseBool(v); err == nil {
			cfg.RedactDiag = b
		}
	}
	if v, ok := os.LookupEnv("RIB_LOG_LEVEL"); ok {
		cfg.LogLevel = v
	}
}

// Validate 校验取值范围与基本格式。
func (c Config) Validate() error {
	var problems []string
	if c.ListenAddr == "" {
		problems = append(problems, "listen_addr must not be empty")
	}
	if c.SQLiteDSN == "" {
		problems = append(problems, "sqlite_dsn must not be empty")
	}
	if c.MaxDepth < 1 || c.MaxDepth > 255 {
		problems = append(problems, "max_recursion_depth must be in [1,255]")
	}
	if c.ReplayLimit < 0 {
		problems = append(problems, "replay_limit must be >= 0")
	}
	switch strings.ToLower(c.LogLevel) {
	case "debug", "info", "warn", "error":
	default:
		problems = append(problems, "log_level must be one of debug/info/warn/error")
	}
	if len(problems) > 0 {
		return fmt.Errorf("invalid config: %s", strings.Join(problems, "; "))
	}
	return nil
}
