# configs/

- `dev.env`：本地开发环境变量（SQLite 路径、树参数、脱敏开关等），全部为本地合成配置。
- `dev_signing_key.pem`：Ed25519 本地开发私钥，**首次启动或 `scripts/gen_dev_key.py` 时生成，已在 .gitignore 中忽略**。
  公钥同名 `.pub` 旁车文件仅用于离线核验，不构成信任根：离线回放要求显式提供受信公钥。
- 生产部署应通过 KMS/秘钥管理系统注入 `SMT_SIGNING_KEY_HEX`，不要使用开发密钥。
