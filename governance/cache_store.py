"""Cache backends for GovernanceMiddleware -- same swap pattern already used
elsewhere in this codebase (storage.py's LocalBucket/GCSBucket,
llm_client.py's Mock/Ollama/Gemini clients). Which one is active is an env
"""


from __future__ import annotations


class CacheStore:
    """Interface every cache backend implements.
"""

    def get(self, prompt_hash: str) -> str | None:
        raise NotImplementedError

    def set(self, prompt_hash: str, response_text: str, agent: str, model: str) -> None:
        raise NotImplementedError

    def size(self) -> int:
        raise NotImplementedError


class InMemoryCacheStore(CacheStore):
    """Default backend -- a plain process-local dict, exactly what
GovernanceMiddleware did inline before this refactor. Fine for tests
"""


    def __init__(self):
        self._data: dict[str, str] = {}

    def get(self, prompt_hash: str) -> str | None:
        return self._data.get(prompt_hash)

    def set(self, prompt_hash: str, response_text: str, agent: str, model: str) -> None:
        self._data[prompt_hash] = response_text

    def size(self) -> int:
        return len(self._data)


class FirestoreCacheStore(CacheStore):
    """Production backend -- persists across restarts and is shared across
every concurrent instance, which InMemoryCacheStore cannot do.
"""


    def __init__(self, project: str | None = None, collection: str | None = None,
                 database: str | None = None):
        import os

        try:
            from google.cloud import firestore
        except ImportError as e:
            raise ImportError(
                "FirestoreCacheStore requires the 'google-cloud-firestore' package "
                "(pip install google-cloud-firestore, or uncomment it in requirements.txt). "
                "Only needed when GOVERNANCE_STORE=firestore -- the default 'memory' "
                "backend has no such dependency."
            ) from e

        # Same project resolution as FirestoreAuditStore: explicit env first,
        # then GCP_PROJECT (set by cloudbuild.yaml), then ADC as last resort.
        self.project = project or os.environ.get("FIRESTORE_PROJECT") or os.environ.get("GCP_PROJECT")
        self.collection_name = collection or os.environ.get("FIRESTORE_CACHE_COLLECTION", "llm_cache")
        self.database = database or os.environ.get("FIRESTORE_DATABASE", "default")
        self._client = firestore.Client(project=self.project, database=self.database)
        self._collection = self._client.collection(self.collection_name)

    def get(self, prompt_hash: str) -> str | None:
        doc = self._collection.document(prompt_hash).get()
        if doc.exists:
            return doc.to_dict().get("response_text")
        return None

    def set(self, prompt_hash: str, response_text: str, agent: str, model: str) -> None:
        from google.cloud import firestore
        self._collection.document(prompt_hash).set({
            "response_text": response_text,
            "agent": agent,
            "model": model,
            "created_at": firestore.SERVER_TIMESTAMP,
        })

    def size(self) -> int:
        # Firestore has no O(1) count -- fine occasionally, not in a hot loop.
        return self._collection.count().get()[0][0].value

class LocalFileCacheStore(CacheStore):
    """Local file-based backend -- persists across restarts for local dev."""

    def __init__(self, filepath: str = "bucket_output/_audit/llm_cache.json"):
        import os, json
        self.filepath = filepath
        os.makedirs(os.path.dirname(self.filepath), exist_ok=True)
        self._data: dict[str, dict] = {}
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r") as f:
                    self._data = json.load(f)
            except Exception:
                pass

    def _save(self):
        import json
        try:
            with open(self.filepath, "w") as f:
                json.dump(self._data, f)
        except Exception as e:
            print(f"[cache_store] Warning: failed to save cache: {e}")

    def get(self, prompt_hash: str) -> str | None:
        val = self._data.get(prompt_hash)
        if val is not None:
            return val.get("response_text")
        return None

    def set(self, prompt_hash: str, response_text: str, agent: str, model: str) -> None:
        self._data[prompt_hash] = {
            "response_text": response_text,
            "agent": agent,
            "model": model,
        }
        self._save()

    def size(self) -> int:
        return len(self._data)
