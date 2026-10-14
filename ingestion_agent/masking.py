"""Masking utilities for the Ingestion Agent.

Anything flagged as a PII field in schema_config.py gets irreversibly hashed
"""


import hashlib
import os


def get_run_salt() -> str:
    """Salt should be stable for the lifetime of a customer's data pipeline
(so the same tenant hashes to the same pseudonym across ingestion runs)
"""

    return os.environ.get("INGESTION_MASK_SALT", "demo-salt-change-me")


def mask_value(raw_value: str) -> str:
    """Irreversibly pseudonymize a raw PII value.
"""
    if raw_value is None:
        return None
    salted = f"{get_run_salt()}:{raw_value}".encode("utf-8")
    digest = hashlib.sha256(salted).hexdigest()[:16]
    return f"MASKED-{digest}"


def apply_masking(record: dict, mask_fields: list[str]) -> dict:
    """Return a copy of record with each field in mask_fields hashed.
"""
    masked = dict(record)
    for field in mask_fields:
        if field in masked and masked[field] is not None:
            masked[field] = mask_value(masked[field])
    return masked
