"""本地合成 PKI：为“仅访问本机测试服务”的演示生成一次性 CA 与服务证书。

不依赖任何外部 CA / 账号；密钥仅落在演示 state 目录。

* CA：EC prime256v1，自签；
* 服务证书：同一曲线，SAN 同时包含 demo 主机名与 127.0.0.1 / ::1，
  这样 TLS 校验按主机名或 IP 都能通过；
* 客户端使用仅信任该 CA 的 SSLContext（trust-on-first-use 之外的隔离信任）。
"""
from __future__ import annotations

import datetime
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@dataclass
class PkiMaterial:
    ca_cert_path: Path
    server_cert_path: Path
    server_key_path: Path


def ensure_demo_pki(
    state_dir: str | Path,
    *,
    hostnames: tuple[str, ...] = ("demo.local",),
    ips: tuple[str, ...] = ("127.0.0.1", "::1"),
) -> PkiMaterial:
    """若 state_dir 中没有 CA/证书则生成；返回路径。"""
    base = Path(state_dir)
    base.mkdir(parents=True, exist_ok=True)
    ca_cert_p = base / "demo_ca.pem"
    server_cert_p = base / "demo_server.pem"
    server_key_p = base / "demo_server_key.pem"

    if ca_cert_p.exists() and server_cert_p.exists() and server_key_p.exists():
        return PkiMaterial(ca_cert_p, server_cert_p, server_key_p)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "ssrf-guard demo CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "local-synthetic"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=False, content_commitment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = ec.generate_private_key(ec.SECP256R1())
    server_name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0]),
    ])
    san = x509.SubjectAlternativeName(
        [x509.DNSName(h) for h in hostnames]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    )
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(san, critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_cert_p.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_cert_p.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    server_key_p.write_bytes(server_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    server_key_p.chmod(0o600)
    return PkiMaterial(ca_cert_p, server_cert_p, server_key_p)


def client_ssl_context(ca_cert_path: str | Path) -> "ssl.SSLContext":
    import ssl

    ctx = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH,
        cadata=Path(ca_cert_path).read_text(encoding="utf-8"),
    )
    # 仅信任演示 CA：不回落到系统信任库
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def server_ssl_context(pki: PkiMaterial) -> "ssl.SSLContext":
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=pki.server_cert_path, keyfile=pki.server_key_path)
    return ctx
