"""测试合成夹具。

全部为人工构造的合成值：卡号为 16 位测试数字段，手机号使用文档常见的
示例段 + 明显测试尾号，令牌/工号/邮箱均为虚构值。严禁包含任何真实
业务数据。
"""
from __future__ import annotations

# 16 位合成卡号（16 位纯数字，演示用测试段）
BANK_CARD = "6225123456789010"
# 11 位合成手机号（199 示例段 + 0000111 明显非真实尾号）
MOBILE = "19900001111"
# 32 位合成令牌（固定模式，便于在输出/日志中搜索确认零泄漏）
TOKEN = "SYN0123456789ABCDEFabcdef1234567"
# 另一个独立令牌，用于重复片段测试
TOKEN2 = "DUP9876543210zyxwvutsrqponmlkjih"
# 工号
EMP_ID = "EMP-000123"
# 合成邮箱
EMAIL = "ops.synth@example.test"

# 敏感键的密码值（合成）
PASSWORD = "P@ssw0rd-SYNTHETIC-9931"
SECRET_NUMBER = "1357902468"  # 放在数字型敏感值里

# 构造的长日志：多类秘密、JSON 转义、重复片段、相邻秘密（冒号/引号紧邻）
SAMPLE_LOG = (
    "2026-09-28T10:00:00Z INFO login user=ops.synth@example.test "
    'msg={"user":"tester","password":"P@ssw0rd-SYNTHETIC-9931",'
    '"card":"6225123456789010","note":"call \\"me\\" 19900001111",'
    '"nested":{"api_key":"SYN0123456789ABCDEFabcdef1234567"},'
    '"repeat":"SYN0123456789ABCDEFabcdef1234567",'
    # 相邻规则：两个秘密仅以 JSON 标点 "," 相邻（保留各自 token 边界）
    '"adjacent":"6225123456789010","adj2":"EMP-000123",'
    '"emp":"EMP-000123","token_num":1357902468}\n'
    "tail plain SYN0123456789ABCDEFabcdef1234567 end"
)

# 相邻规则：两个秘密仅以 JSON 结构标点 "," 相邻（卡号/工号两侧都是引号，
# 词边界成立），两条规则应各自命中、互不压制
ADJACENT_TEXT = '{"a":"6225123456789010","b":"EMP-000123"}'

ALL_SECRETS = [BANK_CARD, MOBILE, TOKEN, TOKEN2, EMP_ID, EMAIL,
               PASSWORD, SECRET_NUMBER]

# 各种切分点（故意切在秘密中间、转义符上、多字节字符上）
SPLIT_POINTS = [1, 3, 7, 16, 17, 31, 32, 40, 64, 77, 100, 128, 200]


def chunks_of(text: str, sizes: list[int]) -> list[str]:
    out: list[str] = []
    i = 0
    k = 0
    while i < len(text):
        size = sizes[k % len(sizes)]
        out.append(text[i:i + size])
        i += size
        k += 1
    return out
