"""Storage abstraction so the Ingestion Agent's logic doesn't care whether it's
writing to a real GCS bucket or a local folder. Swap LocalBucket for a real
google-cloud-storage client in production without touching ingestion_agent.py.
"""


import json
import os


class LocalBucket:
    """Drop-in stand-in for a GCS bucket, for demo.
"""

    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        os.makedirs(root_dir, exist_ok=True)

    def write_json(self, blob_path: str, data) -> str:
        full_path = os.path.join(self.root_dir, blob_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        return full_path

    def read_json(self, blob_path: str):
        full_path = os.path.join(self.root_dir, blob_path)
        with open(full_path, "r", encoding="utf-8") as f:
            return json.load(f)


class GCSBucket:
    """Real implementation for production. Requires `google-cloud-storage`.
Left unimplemented here since the sandbox has no GCP credentials --
"""


    def __init__(self, bucket_name: str):
        from google.cloud import storage
        import google.auth

        credentials, project = google.auth.default()

        print("Project:", project)
        print("Credential type:", type(credentials))
        print(
            "Service account:",
            getattr(credentials, "service_account_email", "unknown")
        )

        self.client = storage.Client(credentials=credentials)
        self.bucket = self.client.bucket(bucket_name)

    def write_json(self, blob_path: str, data) -> str:
        blob = self.bucket.blob(blob_path)
        blob.upload_from_string(json.dumps(data, default=str), content_type="application/json")
        return f"gs://{self.bucket.name}/{blob_path}"

    def read_json(self, blob_path: str):
        blob = self.bucket.blob(blob_path)
        data = blob.download_as_text()
        return json.loads(data)
