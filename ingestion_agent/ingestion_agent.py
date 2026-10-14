"""Ingestion Agent.

Responsibilities (matches the "Ingestion agent" box in the architecture diagram):
"""


from __future__ import annotations

import csv
import os
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from statistics import mean

MODULE_DIR = Path(__file__).resolve().parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from masking import apply_masking
from schema_config import SOURCE_SCHEMAS
from storage import LocalBucket


def _parse_gcs_uri(csv_path: str) -> tuple[str, str] | None:
    """Split a ``gs://bucket-name/path/to/file.csv`` URI into ``(bucket, blob)``,
or return ``None`` for anything that isn't a gs:// URI (i.e. a plain local
"""

    if not csv_path.startswith("gs://"):
        return None
    without_scheme = csv_path[len("gs://"):]
    if "/" not in without_scheme:
        raise ValueError(
            f"malformed GCS URI {csv_path!r}: expected gs://bucket-name/path/to/file.csv"
        )
    bucket, blob = without_scheme.split("/", 1)
    if not bucket or not blob:
        raise ValueError(
            f"malformed GCS URI {csv_path!r}: expected gs://bucket-name/path/to/file.csv"
        )
    return bucket, blob


def _resolve_csv_path(csv_path: str) -> str:
    """Return a readable *local* path for ``csv_path``, downloading it first if
it's a ``gs://`` URI.
"""

    parsed = _parse_gcs_uri(csv_path)
    if parsed is None:
        return csv_path

    bucket_name, blob_name = parsed
    # Lazy import so local/mock runs never require google-cloud-storage.
    try:
        from google.cloud import storage

        client = storage.Client()
        blob = client.bucket(bucket_name).blob(blob_name)
        # delete=False: the caller opens this path by name after we return.
        suffix = os.path.splitext(blob_name)[1] or ".csv"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.close()
        blob.download_to_filename(tmp.name)
        return tmp.name
    except Exception as exc:
        filename = os.path.basename(blob_name)
        candidate = MODULE_DIR.parent / "ingestion_data" / filename
        if candidate.exists():
            print(f"[_resolve_csv_path] GCS download failed ({exc}), falling back to local {candidate}")
            return str(candidate.resolve())
        raise



def _configured_bucket_name() -> str | None:
    """The GCS bucket from config.json (CONFIG_FILE env or repo root), used to
resolve bucket-relative csv_paths when GCS_BUCKET_NAME isn't in the env.
"""

    import json
    config_path = os.environ.get("CONFIG_FILE") or str(MODULE_DIR.parent / "config.json")
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        return config.get("GCS_BUCKET_NAME") or config.get("gcs_bucket")
    except Exception:
        return None


@dataclass
class IngestionResult:
    source_type: str
    total_rows: int = 0
    accepted_rows: int = 0
    quarantined_rows: list = field(default_factory=list)
    records: list = field(default_factory=list)


class IngestionAgent:
    def __init__(self, bucket):
        self.bucket = bucket

    def _resolve_csv_path(self, csv_path: str) -> str:
        if not csv_path:
            raise ValueError("csv_path is required")

        # gs:// URIs -- download to a temp file (module-level helper).
        if csv_path.startswith("gs://"):
            return _resolve_csv_path(csv_path)

        if os.path.exists(csv_path):
            return csv_path

        if os.path.exists(os.path.join(os.getcwd(), csv_path)):
            return os.path.join(os.getcwd(), csv_path)

        attempted = [csv_path, os.path.join(os.getcwd(), csv_path)]

        # Bucket-relative paths: callers (e.g. the UI hitting POST /run) often
        # pass "bucket-name/path/to.csv" or a bare blob path without the gs://
        # scheme. If a GCS bucket is configured, try both interpretations.
        bucket_name = os.environ.get("GCS_BUCKET_NAME") or _configured_bucket_name()
        if bucket_name:
            candidates = []
            first, _, rest = csv_path.lstrip("/").partition("/")
            if first == bucket_name and rest:
                candidates.append(f"gs://{csv_path.lstrip('/')}")
            candidates.append(f"gs://{bucket_name}/{csv_path.lstrip('/')}")
            for uri in candidates:
                attempted.append(uri)
                try:
                    return _resolve_csv_path(uri)
                except Exception:
                    continue

        # Last resort: the copy of the demo input data shipped in the image
        # under ingestion_data/, matched by basename.
        shipped = MODULE_DIR.parent / "ingestion_data" / os.path.basename(csv_path)
        attempted.append(str(shipped))
        if shipped.exists():
            return str(shipped)

        raise FileNotFoundError(
            f"No such file or directory: {csv_path} (tried: {', '.join(attempted)})"
        )

    # ---- step 1+2+3+4: read, map, mask, validate ---------------------------

    def ingest_csv(self, csv_path: str, source_type: str) -> IngestionResult:
        if source_type not in SOURCE_SCHEMAS:
            raise ValueError(f"Unknown source_type '{source_type}'. "
                              f"Add it to schema_config.py before ingesting.")

        schema = SOURCE_SCHEMAS[source_type]
        result = IngestionResult(source_type=source_type)
        # csv_path may be local, bucket-relative, or a gs:// URI; resolves to a
        # local path either way. (Was resolved twice before -- one method call,
        # one module call -- causing a double GCS download and the method's
        # stricter local-only check to raise first.)
        local_path = self._resolve_csv_path(csv_path)

        with open(local_path, newline="") as f:
            reader = csv.DictReader(f)
            for raw_row in reader:
                result.total_rows += 1
                try:
                    record = self._map_row(raw_row, schema, source_type)
                    self._validate(record)
                    record = apply_masking(record, schema["mask_fields"])
                    result.records.append(record)
                    result.accepted_rows += 1
                except ValueError as e:
                    result.quarantined_rows.append({"row": raw_row, "reason": str(e)})

        return result

    def _map_row(self, raw_row: dict, schema: dict, source_type: str) -> dict:
        record = {}
        for raw_col, canonical_field in schema["column_map"].items():
            record[canonical_field] = raw_row.get(raw_col)

        # carry through source-specific columns without forcing every
        # source onto the same rigid shape
        record["extra"] = {col: raw_row.get(col) for col in schema.get("extra_columns", [])}

        if not record.get("status"):
            record["status"] = schema.get("default_status") or "Success"

        record["source_type"] = source_type
        return record

    def _validate(self, record: dict) -> None:
        required = ["customer_reference_id", "transaction_date", "amount"]
        missing = [f for f in required if not record.get(f)]
        if missing:
            raise ValueError(f"missing required field(s): {missing}")

        try:
            datetime.strptime(record["transaction_date"], "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"unparsable transaction_date: {record['transaction_date']!r}")

        try:
            record["amount"] = float(record["amount"])
        except (TypeError, ValueError):
            raise ValueError(f"unparsable amount: {record['amount']!r}")

        if record["amount"] < 0:
            raise ValueError(f"negative amount: {record['amount']}")

    # ---- step 5: historical baseline per counterparty -----------------------

    def compute_historical_stats(self, records: list) -> dict:
        """Per customer_reference_id: monthly average and rolling 3/6/12-month
averages, computed only from Success transactions (a Failed direct
"""

        by_customer = defaultdict(list)
        for r in records:
            if r["status"] == "Success":
                by_customer[r["customer_reference_id"]].append(r)

        stats = {}
        for cust_id, txns in by_customer.items():
            txns_sorted = sorted(txns, key=lambda r: r["transaction_date"])
            amounts = [t["amount"] for t in txns_sorted]

            months = defaultdict(list)
            for t in txns_sorted:
                month_key = t["transaction_date"][:7]  # YYYY-MM
                months[month_key].append(t["amount"])
            monthly_avgs = {m: round(mean(v), 2) for m, v in months.items()}
            sorted_months = sorted(monthly_avgs.keys())

            def rolling(n):
                recent = sorted_months[-n:]
                vals = [monthly_avgs[m] for m in recent]
                return round(mean(vals), 2) if vals else None

            stats[cust_id] = {
                "transaction_count": len(txns_sorted),
                "monthly_averages": monthly_avgs,
                "rolling_3m_avg": rolling(3),
                "rolling_6m_avg": rolling(6),
                "rolling_12m_avg": rolling(12),
                "overall_avg": round(mean(amounts), 2) if amounts else None,
                "last_transaction_date": txns_sorted[-1]["transaction_date"],
            }
        return stats

    # ---- step 6: write to bucket --------------------------------------------

    def run(self, csv_path: str | list, source_type: str, customer_id: str, run_date: str | None = None) -> dict:
        """Naming convention for everything this agent writes, mirroring how the
real GCS bucket would be laid out in production:

``csv_path`` may be a single path or a list of paths (e.g. the 2-year
history plus the current-period file); all rows are merged into one
snapshot so every input file for a customer gets processed together.
"""

        run_date = run_date or date.today().isoformat()
        csv_paths = [csv_path] if isinstance(csv_path, (str, Path)) else list(csv_path)

        result = IngestionResult(source_type=source_type)
        for path in csv_paths:
            part = self.ingest_csv(str(path), source_type)
            result.total_rows += part.total_rows
            result.accepted_rows += part.accepted_rows
            result.quarantined_rows.extend(part.quarantined_rows)
            result.records.extend(part.records)
        stats = self.compute_historical_stats(result.records)

        prefix = f"{customer_id}/{run_date}"
        records_path = self.bucket.write_json(
            f"{prefix}/normalized_transactions.json", result.records
        )
        stats_path = self.bucket.write_json(
            f"{prefix}/historical_stats.json", stats
        )
        quarantine_path = self.bucket.write_json(
            f"{prefix}/quarantined_rows.json", result.quarantined_rows
        )

        summary = {
            "customer_id": customer_id,
            "source_type": source_type,
            "run_date": run_date,
            "total_rows": result.total_rows,
            "accepted_rows": result.accepted_rows,
            "quarantined_rows": len(result.quarantined_rows),
            "unique_counterparties": len(stats),
            "outputs": {
                "normalized_transactions": records_path,
                "historical_stats": stats_path,
                "quarantined_rows": quarantine_path,
            },
        }
        return summary
