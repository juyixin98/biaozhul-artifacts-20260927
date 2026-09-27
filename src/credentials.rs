//! 删除凭证与记账层。
//!
//! * [`crate::token`]：capability token——插入成功时签发，删除时必须出示。令牌内含键标识、
//!   插入序号与过滤器定位（i1/i2/指纹）并由服务端 HMAC 密钥签名，防伪造、防篡改。
//! * [`crate::ledger`]：每个键的「已签发数量 / 已花费序号」记账。它把 Cuckoo 过滤器的
//!   概率性结构与**精确的删除授权**绑定：重复键计数精确、令牌不可重放，从而删除不会
//!   因假阳性而误伤其他键。

/// 凭证/记账层错误（每一类都是可判定的具体失败，不与成功混用）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CredentialError {
    /// 令牌编码损坏（base64/长度/字段非法）。
    MalformedToken,
    /// HMAC 签名不匹配：伪造或被篡改。
    BadSignature,
    /// 令牌版本不受支持。
    UnsupportedTokenVersion,
    /// 键从未成功插入过（或其计数已归零且无新插入）。
    UnknownKey,
    /// 序号超出该键已签发范围。
    OrdinalNeverIssued,
    /// 该令牌已被使用过一次（重放）。
    TokenReplayed,
}

impl std::fmt::Display for CredentialError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let s = match self {
            CredentialError::MalformedToken => "令牌编码损坏",
            CredentialError::BadSignature => "令牌签名无效",
            CredentialError::UnsupportedTokenVersion => "令牌版本不受支持",
            CredentialError::UnknownKey => "键不存在有效插入记录",
            CredentialError::OrdinalNeverIssued => "令牌序号从未签发",
            CredentialError::TokenReplayed => "令牌已被使用，禁止重放",
        };
        f.write_str(s)
    }
}

impl std::error::Error for CredentialError {}
