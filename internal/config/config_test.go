package config_test

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"ipfragreasm/internal/config"
)

func TestDefaultAndValidate(t *testing.T) {
	cfg := config.Default()
	if err := cfg.Validate(); err != nil {
		t.Fatalf("默认配置应合法: %v", err)
	}
	if cfg.MaxDatagramBytes != 65535 {
		t.Fatalf("默认上限应为 65535")
	}
	if cfg.ReassembleTimeout.Duration() <= 0 {
		t.Fatalf("默认超时必须为正")
	}
}

func TestValidateRejectsBadValues(t *testing.T) {
	cfg := config.Default()
	cfg.ReassembleTimeout = config.Duration(0)
	if err := cfg.Validate(); err == nil {
		t.Fatalf("超时为 0 必须报错")
	}
	cfg = config.Default()
	cfg.MaxDatagramBytes = 70000
	if err := cfg.Validate(); err == nil {
		t.Fatalf("上限超过 65535 必须报错")
	}
	cfg = config.Default()
	cfg.Listen = ""
	if err := cfg.Validate(); err == nil {
		t.Fatalf("空监听地址必须报错")
	}
}

func TestLoadJSONAndEnvOverride(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.json")
	if err := os.WriteFile(p, []byte(`{
		"listen":"127.0.0.1:9999",
		"sqlite_dsn":"memory",
		"reassemble_timeout":"250ms",
		"max_datagram_bytes":1000
	}`), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := config.Load(p)
	if err != nil {
		t.Fatalf("加载配置: %v", err)
	}
	if cfg.Listen != "127.0.0.1:9999" || cfg.ReassembleTimeout.Duration() != 250*time.Millisecond {
		t.Fatalf("JSON 字段未生效: %+v", cfg)
	}
	if cfg.MaxDatagramBytes != 1000 {
		t.Fatalf("上限未生效")
	}

	// 环境变量覆盖 JSON。
	t.Setenv("REASM_REASSEMBLE_TIMEOUT", "750ms")
	t.Setenv("REASM_LISTEN", "127.0.0.1:7777")
	cfg, err = config.Load(p)
	if err != nil {
		t.Fatalf("二次加载: %v", err)
	}
	if cfg.ReassembleTimeout.Duration() != 750*time.Millisecond || cfg.Listen != "127.0.0.1:7777" {
		t.Fatalf("环境变量未覆盖: %+v", cfg)
	}
}

func TestLoadMissingFileFallsBack(t *testing.T) {
	cfg, err := config.Load(filepath.Join(t.TempDir(), "absent.json"))
	if err != nil {
		t.Fatalf("缺省配置文件应回退默认值而非报错: %v", err)
	}
	if err := cfg.Validate(); err != nil {
		t.Fatalf("回退默认值应合法: %v", err)
	}
}
