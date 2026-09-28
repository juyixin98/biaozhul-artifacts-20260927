// Package config 定义后端运行配置。
//
// 优先级：命令行 flag > 环境变量 > 默认值。所有依赖都是本地的：
// SQLite 文件、HTTP 监听地址、初始夹具路径——没有任何生产账号/外部服务。
package config

import (
	"flag"
	"fmt"
	"os"
	"strconv"
)

// Config 是运行配置。
type Config struct {
	// HTTP 监听地址。
	Addr string
	// SQLite DSN（文件路径，如 file:data/netpol.db?_txlock=immediate）。
	SQLiteDSN string
	// 初始数据包（可选）：存在则启动时装载并协调一次。
	SeedBundle string
	// 启动时若库中已有数据，是否仍用 SeedBundle 覆盖。
	SeedOverwrite bool
	// 是否输出 DEBUG 级别诊断（含两侧阻断细节）。
	Debug bool
}

// Default 返回本地开发默认配置。
func Default() Config {
	return Config{
		Addr:          ":8080",
		SQLiteDSN:     "file:data/netpol.db",
		SeedBundle:    "",
		SeedOverwrite: false,
		Debug:         false,
	}
}

// envOr 读取环境变量，缺失时返回 fallback。
func envOr(key, fallback string) string {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		return v
	}
	return fallback
}

func envBool(key string, fallback bool) bool {
	if v, ok := os.LookupEnv(key); ok && v != "" {
		b, err := strconv.ParseBool(v)
		if err == nil {
			return b
		}
	}
	return fallback
}

// FromEnv 用环境变量填充默认配置。
//
//	NETPOL_ADDR, NETPOL_SQLITE_DSN, NETPOL_SEED, NETPOL_SEED_OVERWRITE, NETPOL_DEBUG
func FromEnv() Config {
	c := Default()
	c.Addr = envOr("NETPOL_ADDR", c.Addr)
	c.SQLiteDSN = envOr("NETPOL_SQLITE_DSN", c.SQLiteDSN)
	c.SeedBundle = envOr("NETPOL_SEED", c.SeedBundle)
	c.SeedOverwrite = envBool("NETPOL_SEED_OVERWRITE", c.SeedOverwrite)
	c.Debug = envBool("NETPOL_DEBUG", c.Debug)
	return c
}

// Flags 把可覆盖项绑定到 flag set；Parse 后环境变量之上再叠加 flag。
func BindFlags(fs *flag.FlagSet, c *Config) {
	fs.StringVar(&c.Addr, "addr", c.Addr, "HTTP 监听地址")
	fs.StringVar(&c.SQLiteDSN, "sqlite-dsn", c.SQLiteDSN, "SQLite DSN（本地文件）")
	fs.StringVar(&c.SeedBundle, "seed", c.SeedBundle, "启动时装载的离线数据包 JSON 路径（可选）")
	fs.BoolVar(&c.SeedOverwrite, "seed-overwrite", c.SeedOverwrite, "用 seed 覆盖库中已有数据")
	fs.BoolVar(&c.Debug, "debug", c.Debug, "输出 DEBUG 级别诊断")
}

// Validate 做最小自检。
func (c Config) Validate() error {
	if c.Addr == "" {
		return fmt.Errorf("addr 不能为空")
	}
	if c.SQLiteDSN == "" {
		return fmt.Errorf("sqlite-dsn 不能为空")
	}
	if c.SeedBundle != "" {
		if _, err := os.Stat(c.SeedBundle); err != nil {
			return fmt.Errorf("seed 数据包不可读: %w", err)
		}
	}
	return nil
}
