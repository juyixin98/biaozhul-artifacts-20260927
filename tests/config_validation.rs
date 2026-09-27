//! 配置层测试：非法参数必须在启动期被拒绝并指出具体字段。

use deletable_cuckoo::config::Config;

fn base() -> Config {
    let mut c = Config::default();
    c.filter.seed_hex = "a".repeat(64);
    c
}

#[test]
fn default_config_is_valid() {
    // 默认配置里种子是固定非零值，应通过校验。
    Config::default().validate().expect("内置默认配置必须合法");
}

#[test]
fn rejects_non_power_of_two_buckets() {
    let mut c = base();
    c.filter.num_buckets = 1000;
    let e = c.validate().unwrap_err();
    assert!(e.to_string().contains("num_buckets"), "{e}");
}

#[test]
fn rejects_too_few_buckets() {
    let mut c = base();
    c.filter.num_buckets = 1;
    assert!(c.validate().is_err());
}

#[test]
fn rejects_bad_fingerprint_bits() {
    for bits in [0u32, 3, 33, 64] {
        let mut c = base();
        c.filter.fingerprint_bits = bits;
        assert!(c.validate().is_err(), "fingerprint_bits={bits} 应被拒绝");
    }
}

#[test]
fn rejects_bad_seed() {
    let mut c = base();
    c.filter.seed_hex = "0".repeat(64);
    assert!(c.validate().is_err(), "全零种子应拒绝");

    let mut c = base();
    c.filter.seed_hex = "zz".repeat(32);
    assert!(c.validate().is_err(), "非十六进制应拒绝");

    let mut c = base();
    c.filter.seed_hex = "ab".repeat(16); // 32 字节但字符 32 -> 16 字节
    assert!(c.validate().is_err(), "长度不足应拒绝");
}

#[test]
fn rejects_excessive_kicks_and_bad_log_level() {
    let mut c = base();
    c.filter.max_kicks = 1_000_000;
    assert!(c.validate().is_err());

    let mut c = base();
    c.logging.level = "verbose".into();
    assert!(c.validate().is_err());
}

#[test]
fn rejects_bad_hmac_secret_override() {
    let mut c = base();
    c.credentials.hmac_secret_hex = "dead".repeat(8); // 32 hex chars = 16 bytes
    assert!(c.validate().is_err());
}

#[test]
fn kernel_params_roundtrip() {
    let c = Config::default();
    let kp = c.kernel_params();
    assert_eq!(kp.num_buckets, c.filter.num_buckets);
    assert_eq!(kp.fingerprint_bits, c.filter.fingerprint_bits);
    // 种子解码正确。
    assert_eq!(hex::encode(kp.seed), c.filter.seed_hex);
}
