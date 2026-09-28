//! 可执行入口（实际逻辑在库 `rb_server` 中，便于集成测试引用）。

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    rb_server::run().await
}
