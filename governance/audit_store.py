"""Audit log backends for GovernanceMiddleware -- same swap pattern as
cache_store.py. Which one is active is an env var
(GOVERNANCE_STORE=memory|firestore), not a code change -- see
"""


from __future__ import annotations

import json
from pathlib import Path


class AuditStore:
    """Interface every audit-log backend implements.
"""

    def append(self, record) -> None:
        raise NotImplementedError

    def all(self) -> list[dict]:
        raise NotImplementedError


class LocalFileAuditStore(AuditStore):
    """Default backend -- append-only local JSONL file, exactly what
GovernanceMiddleware did inline before this refactor. Fine for tests
"""


    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps(record.__dict__) + "\n")

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path) as f:
            return [json.loads(line) for line in f if line.strip()]


class FirestoreAuditStore(AuditStore):
    """Production backend -- durable across restarts, and shared across
concurrent instances, which is what a real compliance audit trail
"""


    def __init__(self, project: str | None = None, collection: str | None = None,
                 database: str | None = None):
        import os

        try:
            from google.cloud import firestore
        except ImportError as e:
            raise ImportError(
                "FirestoreAuditStore requires the 'google-cloud-firestore' package "
                "(pip install google-cloud-firestore, or uncomment it in requirements.txt). "
                "Only needed when GOVERNANCE_STORE=firestore -- the default 'memory' "
                "backend has no such dependency."
            ) from e

        # FIRESTORE_PROJECT if set; otherwise GCP_PROJECT (cloudbuild.yaml
        # already injects it as $PROJECT_ID) so the client targets the same
        # project the (default) database was created in, instead of whatever
        # ADC happens to resolve.
        self.project = project or os.environ.get("FIRESTORE_PROJECT") or os.environ.get("GCP_PROJECT")
        self.collection_name = collection or os.environ.get("FIRESTORE_AUDIT_COLLECTION", "llm_audit_log")
        self.database = database or os.environ.get("FIRESTORE_DATABASE", "default")
        self._client = firestore.Client(project=self.project, database=self.database)
        self._collection = self._client.collection(self.collection_name)
        # Fail fast with an actionable message instead of a NotFound deep in
        # the first append()/all() call.
        try:
            next(iter(self._collection.limit(1).stream()), None)
        except Exception as e:
            raise RuntimeError(
                f"Firestore database {self.database!r} is not reachable in project "
                f"{self.project or self._client.project!r}: {e}.\n"
                "If the database already exists, the client is probably resolving a "
                "DIFFERENT project -- set FIRESTORE_PROJECT (or GCP_PROJECT) to the "
                "project that owns the database. If it truly doesn't exist:\n"
                f"  gcloud firestore databases create --location=us-central1 "
                f"--project={self.project or self._client.project}\n"
                "Or set GOVERNANCE_STORE=memory to skip Firestore."
            ) from e


        # Fail fast with an actionable message instead of a NotFound deep in
        # the first append()/all() call.
        try:
            next(iter(self._collection.limit(1).stream()), None)
        except Exception as e:
            raise RuntimeError(
                f"Firestore database {self.database!r} is not reachable in project "
                f"{self.project or self._client.project!r}: {e}.\n"
                "If the database already exists, the client is probably resolving a "
                "DIFFERENT project -- set FIRESTORE_PROJECT (or GCP_PROJECT) to the "
                "project that owns the database. If it truly doesn't exist:\n"
                f"  gcloud firestore databases create --location=us-central1 "
                f"--project={self.project or self._client.project}\n"
                "Or set GOVERNANCE_STORE=memory to skip Firestore."
            ) from e

    def append(self, record) -> None:
        from google.cloud import firestore
        data = dict(record.__dict__)
        data["created_at"] = firestore.SERVER_TIMESTAMP
        self._collection.document(record.trace_id).set(data)

    def all(self) -> list[dict]:
        return [doc.to_dict() for doc in self._collection.stream()]
