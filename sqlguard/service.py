"""Service layer: assemble policy, schema snapshot, kernel and audit store."""

from __future__ import annotations

from dataclasses import dataclass

from .audit_store import AuditStore
from .crypto import KeyMaterial
from .isolation import snapshot_schema
from .kernel import Kernel
from .policy import load_policy


@dataclass
class Services:
    kernel: Kernel
    store: AuditStore


def build_services(settings) -> Services:
    policy = load_policy(settings.policy_path)
    schema = snapshot_schema(settings.fixture_db)  # raises if fixture missing
    key = KeyMaterial.load_or_create(settings.audit_key)
    store = AuditStore(settings.audit_db, key)
    return Services(kernel=Kernel(policy, schema), store=store)
