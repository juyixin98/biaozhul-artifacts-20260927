"""命令行入口：启动服务 / 生成合成密钥。

示例：
    teaching-chain keygen                 # 打印一个确定性种子生成的合成密钥
    teaching-chain serve --port 8000
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys

from .encoding import KeyPair
from .vm.assembler import assemble, disassemble


def _cmd_keygen(args: argparse.Namespace) -> int:
    if args.seed is not None:
        seed = hashlib.sha256(args.seed.encode("utf-8")).digest()
    else:
        seed = hashlib.sha256(b"teaching-chain-example-default-seed").digest()
    kp = KeyPair.from_seed(seed)
    print(json.dumps({
        "address": kp.address_hex(),
        "pubkey": kp.public_bytes().hex(),
        "private_seed": seed.hex(),
        "note": "本地合成教学密钥，绝无真实价值",
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_assemble(args: argparse.Namespace) -> int:
    text = sys.stdin.read()
    code = assemble(text)
    if args.disassemble:
        print(disassemble(code), file=sys.stderr)
    print(code.hex())
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .api import run
    run(host=args.host, port=args.port, db_path=args.db)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="teaching-chain", description="教学链命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    p_keygen = sub.add_parser("keygen", help="生成合成教学密钥")
    p_keygen.add_argument("--seed", default=None, help="可选种子字符串（缺省用固定示例种子）")
    p_keygen.set_defaults(func=_cmd_keygen)

    p_asm = sub.add_parser("assemble", help="从 stdin 读取助记符并输出十六进制字节码")
    p_asm.add_argument("--disassemble", action="store_true", help="同时打印反汇编")
    p_asm.set_defaults(func=_cmd_assemble)

    p_serve = sub.add_parser("serve", help="启动 HTTP 服务")
    p_serve.add_argument("--host", default=None)
    p_serve.add_argument("--port", type=int, default=None)
    p_serve.add_argument("--db", default=None, help="SQLite 索引路径")
    p_serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
