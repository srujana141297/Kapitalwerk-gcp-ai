"""Last-4-weeks notifications aggregator for the Reporting Agent.

The weekly reminder cycle (weekly_reminders.py) acts on ONE snapshot at a time
"""


from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import weekly_reminders as wr

WEEKS_LOOKBACK = 4
TOP_N = 10
MIN_TRANSACTIONS_FOR_BASELINE = wr.MIN_TRANSACTIONS_FOR_BASELINE

# Per-snapshot cache: snapshot files are immutable once written, so re-parsing
# the same (bucket_root, persona, run_date) on every request is pure waste.
_MISSED_CACHE: dict[tuple[str, str, str], list[dict]] = {}
_UPCOMING_CACHE: dict[tuple[str, str, str], list[dict]] = {}


def _is_gcs_path(path: str) -> bool:
    return isinstance(path, str) and path.startswith("gs://")


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"Malformed GCS URI: {uri!r}")
    without_scheme = uri[len("gs://"):]
    if "/" not in without_scheme:
        raise ValueError(f"Malformed GCS URI: {uri!r}")
    bucket, blob = without_scheme.split("/", 1)
    return bucket, blob


_gcs_client_singleton = None


def _gcs_client():
    """Reused across calls -- constructing storage.Client() repeatedly redoes
ADC credential discovery and was the dominant cost of dashboard() requests.
"""

    global _gcs_client_singleton
    if _gcs_client_singleton is None:
        from google.cloud import storage
        _gcs_client_singleton = storage.Client()
    return _gcs_client_singleton


def _read_gcs_text(path: str) -> str:
    bucket_name, blob_name = _parse_gcs_uri(path)
    client = _gcs_client()
    blob = client.bucket(bucket_name).blob(blob_name)
    return blob.download_as_text()


def _exists(path: str) -> bool:
    if _is_gcs_path(path):
        bucket_name, blob_name = _parse_gcs_uri(path)
        client = _gcs_client()
        return client.bucket(bucket_name).blob(blob_name).exists()
    return os.path.exists(path)


def _is_dir(path: str) -> bool:
    if _is_gcs_path(path):
        bucket_name, prefix = _parse_gcs_uri(path)
        client = _gcs_client()
        bucket = client.bucket(bucket_name)
        prefix = prefix.rstrip("/") + "/"
        return any(True for _ in bucket.list_blobs(prefix=prefix, max_results=1))
    return os.path.isdir(path)


def _load(path):
    if _is_gcs_path(path):
        return json.loads(_read_gcs_text(path))
    with open(path) as f:
        return json.load(f)


def _snapshot_dates(bucket_root: str, persona: str) -> list[str]:
    """All run_date folders for a persona that have an inflow_flags.json (the
marker of a fully-processed snapshot), newest first.
"""

    persona_dir = os.path.join(bucket_root, persona)
    if not _is_dir(persona_dir):
        return []
    dates = []
    if _is_gcs_path(persona_dir):
        bucket_name, prefix = _parse_gcs_uri(persona_dir)
        client = _gcs_client()
        bucket = client.bucket(bucket_name)
        root_prefix = prefix.rstrip("/") + "/"
        # delimiter="/" returns only immediate subfolder names instead of
        # every object under every date folder -- avoids an O(all files ever
        # written) scan just to find date-folder names.
        iterator = bucket.list_blobs(prefix=root_prefix, delimiter="/")
        list(iterator)  # must be consumed for .prefixes to populate
        candidates = sorted(
            (p[len(root_prefix):].rstrip("/") for p in iterator.prefixes
             if not p[len(root_prefix):].rstrip("/").startswith("_")),
            reverse=True,
        )
        for d in candidates:
            if _exists(os.path.join(persona_dir, d, "inflow_flags.json")):
                dates.append(d)
        return dates
    for name in os.listdir(persona_dir):
        if os.path.exists(os.path.join(persona_dir, name, "inflow_flags.json")):
            dates.append(name)
    return sorted(dates, reverse=True)


def recent_weekly_snapshots(bucket_root: str, persona: str, today: date,
                            weeks: int = WEEKS_LOOKBACK) -> list[str]:
    """Up to `weeks` most-recent snapshot dates that fall within weeks*7 days
on/before `today`. Works whenever that much history exists and simply
"""

    cutoff = today - timedelta(days=weeks * 7)
    picked = []
    for d in _snapshot_dates(bucket_root, persona):
        try:
            dd = date.fromisoformat(d)
        except ValueError:
            continue
        if cutoff <= dd <= today:
            picked.append(d)
        if len(picked) >= weeks:
            break
    return picked


def _missed_notifications(bucket_root, persona, run_date) -> list[dict]:
    cache_key = (bucket_root, persona, run_date)
    cached = _MISSED_CACHE.get(cache_key)
    if cached is not None:
        return cached
    out = _missed_notifications_uncached(bucket_root, persona, run_date)
    _MISSED_CACHE[cache_key] = out
    return out


def _missed_notifications_uncached(bucket_root, persona, run_date) -> list[dict]:
    prefix = os.path.join(bucket_root, persona, run_date)
    flags = _load(os.path.join(prefix, "inflow_flags.json"))
    stats_path = os.path.join(prefix, "historical_stats.json")
    stats = _load(stats_path) if _exists(stats_path) else {}
    out = []
    for miss in flags.get("missing_payments", []):
        cust = miss["customer_reference_id"]
        s = stats.get(cust, {})
        amount = s.get("rolling_3m_avg") or s.get("overall_avg") or 0.0
        days_overdue = miss.get("days_overdue", 0)
        out.append({
            "persona": persona,
            "snapshot_date": run_date,
            "customer_reference_id": cust,
            "type": "missed_payment",
            "amount": round(amount, 2),
            "expected_by": miss.get("expected_by"),
            "days_overdue": days_overdue,
            # missed payments are more urgent than upcoming ones; overdue days
            # and amount both push relevance up.
            "relevance": round(1_000_000 + days_overdue * 1000 + amount, 2),
        })
    return out


def _upcoming_notifications(bucket_root, persona, run_date) -> list[dict]:
    cache_key = (bucket_root, persona, run_date)
    cached = _UPCOMING_CACHE.get(cache_key)
    if cached is not None:
        return cached
    out = _upcoming_notifications_uncached(bucket_root, persona, run_date)
    _UPCOMING_CACHE[cache_key] = out
    return out


def _upcoming_notifications_uncached(bucket_root, persona, run_date) -> list[dict]:
    prefix = os.path.join(bucket_root, persona, run_date)
    stats_path = os.path.join(prefix, "historical_stats.json")
    txns_path = os.path.join(prefix, "normalized_transactions.json")
    if not (_exists(stats_path) and _exists(txns_path)):
        return []
    stats = _load(stats_path)
    transactions = _load(txns_path)
    dates_by_cust = wr._successful_dates_by_customer(transactions)

    snapshot = date.fromisoformat(run_date)
    out = []
    for cust, s in stats.items():
        if s.get("transaction_count", 0) < MIN_TRANSACTIONS_FOR_BASELINE:
            continue
        last = s.get("last_transaction_date")
        if not last:
            continue
        cadence = wr._cadence_days(dates_by_cust.get(cust, []))
        next_expected = datetime.strptime(last, "%Y-%m-%d").date() + timedelta(days=cadence)
        if next_expected < snapshot:
            continue  # already due/overdue -- that's a missed-payment concern, not upcoming
        amount = s.get("rolling_3m_avg") or s.get("overall_avg") or 0.0
        out.append({
            "persona": persona,
            "snapshot_date": run_date,
            "customer_reference_id": cust,
            "type": "upcoming_payment",
            "amount": round(amount, 2),
            "expected_date": next_expected.isoformat(),
            "days_until": (next_expected - snapshot).days,
            "relevance": round(amount, 2),  # ranked below any missed payment
        })
    return out


def last_4_weeks_notifications(bucket_root: str, persona: str, today: date,
                               weeks: int = WEEKS_LOOKBACK, top_n: int = TOP_N) -> list[dict]:
    """Merge upcoming + missed notifications across the last `weeks` weekly
snapshots for one persona, deduplicate to the most relevant entry per
"""

    snapshots = recent_weekly_snapshots(bucket_root, persona, today, weeks)

    best: dict[tuple[str, str], dict] = {}
    for run_date in snapshots:
        for note in _missed_notifications(bucket_root, persona, run_date) + \
                    _upcoming_notifications(bucket_root, persona, run_date):
            key = (note["customer_reference_id"], note["type"])
            existing = best.get(key)
            # keep the most relevant; tie-break on the more recent snapshot
            if (existing is None
                    or note["relevance"] > existing["relevance"]
                    or (note["relevance"] == existing["relevance"]
                        and note["snapshot_date"] > existing["snapshot_date"])):
                best[key] = note

    missed = sorted((n for n in best.values() if n["type"] == "missed_payment"),
                     key=lambda n: n["relevance"], reverse=True)
    upcoming = sorted((n for n in best.values() if n["type"] == "upcoming_payment"),
                       key=lambda n: n["relevance"], reverse=True)

    missed_quota = -(-top_n // 2)  # ceil(top_n / 2) -- missed gets first claim on an odd slot
    upcoming_quota = top_n - missed_quota

    picked_missed = missed[:missed_quota]
    picked_upcoming = upcoming[:upcoming_quota]
    # backfill unused capacity from the other bucket so a persona with few
    # missed payments (or few upcoming ones) still fills a full top_n
    leftover = top_n - len(picked_missed) - len(picked_upcoming)
    if leftover > 0:
        picked_missed += missed[len(picked_missed):len(picked_missed) + leftover]
        leftover = top_n - len(picked_missed) - len(picked_upcoming)
    if leftover > 0:
        picked_upcoming += upcoming[len(picked_upcoming):len(picked_upcoming) + leftover]

    merged = sorted(picked_missed + picked_upcoming, key=lambda n: n["relevance"], reverse=True)
    return merged[:top_n]
