"""命令行：本地签名/提交/演示/服务启动。

示例（详见 README）::

    python -m teachchain.cli demo --db demo.db
    python -m teachchain.cli serve --db teachchain.db
    python -m teachchain.cli replay --db teachchain.db
    python -m teachchain.cli keygen

签名全部在本机完成；``keygen`` 才会使用密码学随机源，交易执行/回放不使用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import fixtures
from .config import get_settings
from .diagnostics import Diagnostics
from .errors import Rejected
from .models import b64e
from .opcodes import assemble
from .replay import replay_store
from .service import ChainService
from .storage import IndexStore
from .version import ENGINE_VERSION


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _sign(s, tx_type, nonce, gas_limit, **kw):
    return fixtures.envelope(s, tx_type, nonce=nonce, gas_limit=gas_limit, **kw)


def cmd_keygen(args) -> int:
    from .crypto import generate_private_key, private_key_to_pem
    key = generate_private_key()
    pem = private_key_to_pem(key).decode()
    if args.out:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, pem.encode())
        os.close(fd)
        print(f"wrote private key (mode 600) to {args.out}")
    else:
        sys.stdout.write(pem)
    from .crypto import address_from_public_key
    print("address:", address_from_public_key(key.public_key()), file=sys.stderr)
    return 0


def _open_service(db: str) -> tuple[ChainService, IndexStore]:
    store = IndexStore(db)
    return ChainService(store, diag=Diagnostics()), store


def cmd_serve(args) -> int:
    import uvicorn
    settings = get_settings()
    uvicorn.run(
        "teachchain.api:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=False,
    )
    return 0


def cmd_replay(args) -> int:
    store = IndexStore(args.db)
    try:
        report = replay_store(store, stop_on_first=not args.all)
    finally:
        store.close()
    _print(report.summary())
    return 0 if report.summary()["ok"] else 1


def cmd_demo(args) -> int:
    """跑一条覆盖成功/REVERT/异常/嵌套调用的链，并打印断言结果。"""
    if os.path.exists(args.db):
        os.remove(args.db)
    svc, store = _open_service(args.db)
    alice, bob = fixtures.signer("alice"), fixtures.signer("bob")
    svc.seed(alice.address, 10_000_000)
    print(f"alice = {alice.address}\nbob   = {bob.address}\nengine = {ENGINE_VERSION}\n")

    def send(env, label):
        try:
            r = svc.submit(env)
            print(f"[{label}] accepted status={r['status']} halt={r['halt_code']} "
                  f"gas_charged={r['gas_charged']} height={r['height']}")
            return r
        except Rejected as rej:
            print(f"[{label}] REJECTED {rej.code}: {rej.message}")
            return None

    # 1) 部署 counter
    counter_addr = "0x" + __import__("hashlib").sha256(
        f"{alice.address}:0".encode()).hexdigest()[:16]
    r1 = send(_sign(alice, "deploy", 0, 500_000, code=fixtures.counter_code()), "deploy counter")

    # 2) 调用 counter +10，断言槽 1 == 10
    r2 = send(_sign(alice, "invoke", 1, 500_000, to=counter_addr, words=[10]),
              "invoke counter +10")
    assert r2 and r2["status"] == 1
    assert store.get_slot(counter_addr, 1) == 10
    print(f"    slot[1] = {store.get_slot(counter_addr, 1)}  (expect 10)")

    # 3) 临界 gas：gas_limit 恰好等于 intrinsic，VM 可用为 0 -> out_of_gas，
    #    状态不变、全部 gas 消耗
    r3 = send(_sign(alice, "invoke", 2, r2["intrinsic_gas"], to=counter_addr, words=[5]),
              "invoke at exact intrinsic gas")
    assert r3 and r3["status"] == 0 and r3["halt_code"] == "out_of_gas"
    assert store.get_slot(counter_addr, 1) == 10
    assert r3["gas_charged"] == r3["gas_limit"]
    print(f"    slot[1] still = {store.get_slot(counter_addr, 1)}; "
          f"gas fully consumed={r3['gas_charged']}")

    # 4) 部署 write-then-halt 合约并调用：写回滚，gas 不退还
    halt_addr = "0x" + __import__("hashlib").sha256(
        f"{alice.address}:3".encode()).hexdigest()[:16]
    send(_sign(alice, "deploy", 3, 500_000, code=fixtures.write_then_halt_code()),
         "deploy write-then-halt")
    r4 = send(_sign(alice, "invoke", 4, 200_000, to=halt_addr), "invoke write-then-halt")
    assert r4 and r4["status"] == 0 and r4["halt_code"] == "invalid_instruction"
    assert store.get_slot(halt_addr, 7) is None
    assert r4["gas_charged"] == r4["gas_limit"]
    print(f"    slot[7] unset after exceptional halt; gas_charged={r4['gas_charged']}")

    # 5) 部署 write-then-revert：写回滚，剩余 gas 退还
    rev_addr = "0x" + __import__("hashlib").sha256(
        f"{alice.address}:5".encode()).hexdigest()[:16]
    send(_sign(alice, "deploy", 5, 500_000, code=fixtures.write_then_revert_code()),
         "deploy write-then-revert")
    r5 = send(_sign(alice, "invoke", 6, 200_000, to=rev_addr), "invoke write-then-revert")
    assert r5 and r5["status"] == 0 and r5["reverted"] and r5["halt_code"] == "revert"
    assert store.get_slot(rev_addr, 8) is None
    assert r5["gas_charged"] < r5["gas_limit"]
    print(f"    slot[8] unset after revert; charged {r5['gas_charged']} "
          f"of {r5['gas_limit']} (remainder returned)")

    # 6) 嵌套调用：父调用会 INVALID 的子合约；子写回滚、父槽 3 保留
    child_addr = halt_addr
    caller_addr = "0x" + __import__("hashlib").sha256(
        f"{alice.address}:7".encode()).hexdigest()[:16]
    send(_sign(alice, "deploy", 7, 500_000, code=fixtures.caller_code()),
         "deploy caller")
    child_word = int(child_addr, 16)
    r6 = send(_sign(alice, "invoke", 8, 500_000, to=caller_addr, words=[child_word]),
              "invoke nested caller")
    assert r6 and r6["status"] == 1
    assert store.get_slot(caller_addr, 3) == 99
    assert store.get_slot(halt_addr, 7) is None
    print(f"    parent slot[3]={store.get_slot(caller_addr, 3)} persisted, "
          f"child slot[7] rolled back")

    # 7) 重放整条链，必须完全一致
    report = replay_store(store, stop_on_first=False)
    _print(report.summary())
    assert report.summary()["ok"]
    store.close()
    print("\nDEMO OK: 所有行为断言与离线重放均通过。")
    return 0


def cmd_asm(args) -> int:
    code = assemble(sys.stdin.read())
    if args.hex:
        print(code.hex())
    else:
        print(b64e(code))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="teachchain", description="教学链确定性状态机")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="启动 HTTP 服务")
    sp.add_argument("--db", default="teachchain.db")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("replay", help="离线重放数据库")
    sp.add_argument("--db", default="teachchain.db")
    sp.add_argument("--all", action="store_true")
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("demo", help="本地合成端到端演示 + 断言")
    sp.add_argument("--db", default="demo.db")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("keygen", help="生成新的 Ed25519 密钥（唯一使用随机源的命令）")
    sp.add_argument("--out", help="写入文件（0600），否则打印 PEM 到 stdout")
    sp.set_defaults(func=cmd_keygen)

    sp = sub.add_parser("asm", help="stdin 读汇编，输出 base64（--hex 则十六进制）")
    sp.add_argument("--hex", action="store_true")
    sp.set_defaults(func=cmd_asm)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
