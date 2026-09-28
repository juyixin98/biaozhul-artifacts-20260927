"""SSRF 出站目标校验内核 —— 应用包。

模块边界（数据与错误契约见 :mod:`app.contracts`）::

    urlparse  : 受控 URL 解析 / 主机规范化
    ipclass   : IP 字面量识别与地址段分类（拒绝判定的事实来源）
    policy    : 规则/证据策略文件解析与有序求值
    resolver  : 受控 DNS 夹具（重绑定脚本、一次性快照）
    connector : 固定到已校验 IP 的连接原语（Socket / TLS）
    httpclient: 最小 HTTP/1.1 客户端，重定向每跳交回内核重校验
    kernel    : 安全内核：统一策略编排、证据链、运行编号
    audit     : SQLite + JSONL 审计落盘，Ed25519 证据签名
    webapi    : FastAPI 审计接口与代理调用入口
"""

__version__ = "1.0.0"
