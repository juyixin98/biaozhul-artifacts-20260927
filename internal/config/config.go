// Package config 定义服务配置。所有数据都落在本地目录，无外部账号依赖。
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"time"
)

// VersionBehavior 描述某版本在模拟进程管理器下的夹具行为。
type VersionBehavior struct {
	// 行为类型：always_ok | fail_start | crash_after | flaky | flaky_first
	Behavior string `json:"behavior"`
	// crash_after: 启动成功后多少次探针崩溃；flaky: 每 N 次探针有一次失败；
	// flaky_first: 前 N 次探针失败。
	Parameter int `json:"parameter"`
	// StartDelayChecks：进程需要被探针“观察”多少次后才完成启动，
	// 用于模拟慢启动（未完成启动期间 start 返回 starting）。0 表示立即启动。
	StartDelayChecks int `json:"start_delay_checks"`
}

// Fixture 是模拟进程管理器的合成环境。
type Fixture struct {
	// Capacity 为 0 表示容量无限；>0 时同时存活进程数达到上限即拒绝新建。
	Capacity int `json:"capacity"`
	// Behaviors 按版本标签选择行为；未配置的版本默认 always_ok。
	Behaviors map[string]VersionBehavior `json:"behaviors"`
}

// Config 是服务的全部配置。
type Config struct {
	HTTPAddr              string   `json:"http_addr"`
	DataDir               string   `json:"data_dir"`      // SQLite 与模拟器状态文件目录
	TickInterval          Duration `json:"tick_interval"` // 后台协调间隔
	DefaultMaxSurge       int      `json:"default_max_surge"`
	DefaultMaxUnavailable int      `json:"default_max_unavailable"`
	DefaultReadyThreshold int      `json:"default_ready_threshold"`
	DefaultFailureLimit   int      `json:"default_failure_limit"`
	DefaultProgressTicks  int      `json:"default_progress_ticks"`
	AutoRollbackOnFailure bool     `json:"auto_rollback_on_failure"`
	Fixture               Fixture  `json:"fixture"`
}

// Duration 包装 time.Duration，使其支持 JSON 字符串（如 "100ms"）。
type Duration struct{ time.Duration }

func (d *Duration) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	v, err := time.ParseDuration(s)
	if err != nil {
		return fmt.Errorf("invalid duration %q: %w", s, err)
	}
	d.Duration = v
	return nil
}

func (d Duration) MarshalJSON() ([]byte, error) {
	return json.Marshal(d.String())
}

// Default 返回带合理默认值的配置。
func Default() Config {
	return Config{
		HTTPAddr:              ":18080",
		DataDir:               "./data",
		TickInterval:          Duration{500 * time.Millisecond},
		DefaultMaxSurge:       1,
		DefaultMaxUnavailable: 0,
		DefaultReadyThreshold: 2,
		DefaultFailureLimit:   2,
		DefaultProgressTicks:  60,
		Fixture:               Fixture{Capacity: 0, Behaviors: map[string]VersionBehavior{}},
	}
}

// Load 从 JSON 文件读取配置；缺失字段沿用 Default。
func Load(path string) (Config, error) {
	cfg := Default()
	b, err := os.ReadFile(path)
	if err != nil {
		return cfg, fmt.Errorf("read config %s: %w", path, err)
	}
	if err := json.Unmarshal(b, &cfg); err != nil {
		return cfg, fmt.Errorf("parse config %s: %w", path, err)
	}
	if cfg.Fixture.Behaviors == nil {
		cfg.Fixture.Behaviors = map[string]VersionBehavior{}
	}
	return cfg, nil
}
