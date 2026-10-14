"""Customer contact store for the Reporting Agent.

WHO THE BANK ACTUALLY EMAILS
"""


from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from contact_crypto import decrypt_email, encrypt_email

# The single safe inbox every demo reminder resolves to. Swap for each
# owner's real address (or a Firestore-backed store) in production.
DEMO_CONTACT_EMAIL = "anu.shree@neversaydie.co.in"

# The bank's three real customers, the only recipients ever emailed.
# persona/customer_id -> owner's first name ("Hi Claire,").
PERSONA_OWNERS = {
    "glucometer-vendor": "Claire",
    "gym-chain": "Mark",
    "boho-house": "Arya",
}

# Display name used in the email body/footer; callers may override per run.
PERSONA_TENANTS = {
    "glucometer-vendor": "MedSupply Direct",
    "gym-chain": "Gym Chain",
    "boho-house": "Boho House Club",
}

# Alongside the pipeline's other cross-run artifacts under bucket_output.
DEFAULT_CONTACT_STORE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "bucket_output", "_contacts", "contact_directory.json"
)

# Append-only log of each plaintext email access -- never the value itself.
DEFAULT_CONTACT_ACCESS_LOG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "bucket_output", "_audit", "contact_access.jsonl"
)


def _mask_email(email: str) -> str:
    """Masked display form (first + last local-char kept), e.g.
'anu.shree@neversaydie.co.in' -> 'a*******e@neversaydie.co.in'. Used for
"""

    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked_local}@{domain}"


def build_contact_directory_file(
    out_path: str | None = None,
    email: str = DEMO_CONTACT_EMAIL,
    personas: dict[str, str] | None = None,
) -> str:
    """(Re)generate the JSON contact store. Writes exactly one record per
persona in PERSONA_OWNERS (three: Claire / Mark / Arya) -- NOT one per
"""

    out_path = out_path or DEFAULT_CONTACT_STORE_PATH
    personas = personas or PERSONA_OWNERS
    now = datetime.now(timezone.utc).isoformat()

    records = [
        {
            "customer_id": persona,
            "owner_name": owner_name,
            "tenant_name": PERSONA_TENANTS.get(persona, persona),
            "email_masked": _mask_email(email),
            "email_encrypted": encrypt_email(email),
            "source": "demo_contact_store",
            "updated_at": now,
        }
        for persona, owner_name in sorted(personas.items())
    ]

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"records": records}, f, indent=2)
    return out_path


def load_contact_directory(
    path: str | None = None,
    customer_id: str | None = None,
):
    """Read the persona-level contact store back.

"""

    path = path or DEFAULT_CONTACT_STORE_PATH
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = json.load(f)

    by_persona = {rec["customer_id"]: rec for rec in data.get("records", [])}
    if customer_id is not None:
        return by_persona.get(customer_id, {})
    return by_persona


def resolve_send_email(record: dict, *, trace: str, access_log_path: str | None = None) -> str:
    """Decrypt a contact record's `email_encrypted` and return the plaintext
address -- the ONLY place in the whole codebase that produces a plaintext
"""

    ciphertext = record.get("email_encrypted")
    if not ciphertext:
        raise ValueError(
            "resolve_send_email: record has no 'email_encrypted' -- was it "
            "built by build_contact_directory_file()? Plaintext emails are "
            "never stored, so there is nothing to send to without it."
        )
    plaintext = decrypt_email(ciphertext)

    path = access_log_path or DEFAULT_CONTACT_ACCESS_LOG_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "trace": trace,
        "customer_id": record.get("customer_id"),
        "owner_name": record.get("owner_name"),
        "action": "email_resolved_for_send",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")

    return plaintext
