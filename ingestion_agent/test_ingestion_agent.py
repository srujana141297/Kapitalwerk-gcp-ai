"""Unit + integration tests for the Ingestion Agent.

Run with: pytest test_ingestion_agent.py -v
"""


import csv
import os
import sys
import tempfile

import pytest

from ingestion_agent import IngestionAgent
from masking import mask_value
from storage import LocalBucket


@pytest.fixture
def bucket(tmp_path):
    return LocalBucket(root_dir=str(tmp_path / "bucket"))


@pytest.fixture
def agent(bucket):
    return IngestionAgent(bucket)


def write_csv(tmp_path, name, header, rows):
    path = tmp_path / name
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return str(path)


# ---- mapping + masking ------------------------------------------------------

def test_hospital_vendor_row_maps_correctly(agent, tmp_path):
    path = write_csv(
        tmp_path, "hosp.csv",
        ["Hospital_Name", "End_to_End_Reference_ID", "Customer_Reference_ID",
         "Invoice_Number", "Payment_Method", "Transaction_Date", "Value_Date",
         "Amount", "Currency"],
        [["Klinikum Test", "E2E-1", "CUST-HOSP-9001", "INV-1",
          "SEPA Credit Transfer", "2026-01-05", "2026-01-06", "1200.00", "EUR"]],
    )
    result = agent.ingest_csv(path, "hospital_vendor")
    assert result.accepted_rows == 1
    r = result.records[0]
    assert r["customer_reference_id"] == "CUST-HOSP-9001"
    assert r["amount"] == 1200.00
    assert r["status"] == "Success"          # source has no failure concept -> defaults
    assert r["extra"]["Invoice_Number"] == "INV-1"
    assert r["counterparty_name"] == "Klinikum Test"  # not masked -- business name, not PII


def test_individual_tenant_name_is_masked(agent, tmp_path):
    path = write_csv(
        tmp_path, "tenant.csv",
        ["Company_Customer_Name", "Customer_Reference_ID", "End_to_End_Reference_ID",
         "Payment_Method", "Transaction_Date", "Value_Date", "Amount", "Currency"],
        [["Anna Keller", "CUST-1001", "SEPA-MND7741-01", "SEPA",
          "2026-07-01", "2026-07-02", "950.00", "EUR"]],
    )
    result = agent.ingest_csv(path, "individual_tenant")
    r = result.records[0]
    assert r["counterparty_name"].startswith("MASKED-")
    assert "Anna Keller" not in r["counterparty_name"]


def test_masking_is_stable_within_a_run():
    assert mask_value("Anna Keller") == mask_value("Anna Keller")
    assert mask_value("Anna Keller") != mask_value("Markus Vogel")


def test_unknown_source_type_raises(agent, tmp_path):
    path = write_csv(tmp_path, "x.csv", ["a"], [["1"]])
    with pytest.raises(ValueError):
        agent.ingest_csv(path, "not_a_real_source")


# ---- validation / quarantine ------------------------------------------------

def test_missing_required_field_is_quarantined(agent, tmp_path):
    path = write_csv(
        tmp_path, "bad.csv",
        ["Hospital_Name", "End_to_End_Reference_ID", "Customer_Reference_ID",
         "Invoice_Number", "Payment_Method", "Transaction_Date", "Value_Date",
         "Amount", "Currency"],
        [["Klinikum Test", "E2E-1", "", "INV-1",  # missing customer_reference_id
          "SEPA Credit Transfer", "2026-01-05", "2026-01-06", "1200.00", "EUR"]],
    )
    result = agent.ingest_csv(path, "hospital_vendor")
    assert result.accepted_rows == 0
    assert len(result.quarantined_rows) == 1
    assert "customer_reference_id" in result.quarantined_rows[0]["reason"]


def test_bad_date_is_quarantined(agent, tmp_path):
    path = write_csv(
        tmp_path, "bad_date.csv",
        ["Hospital_Name", "End_to_End_Reference_ID", "Customer_Reference_ID",
         "Invoice_Number", "Payment_Method", "Transaction_Date", "Value_Date",
         "Amount", "Currency"],
        [["Klinikum Test", "E2E-1", "CUST-HOSP-9001", "INV-1",
          "SEPA Credit Transfer", "05/01/2026", "2026-01-06", "1200.00", "EUR"]],
    )
    result = agent.ingest_csv(path, "hospital_vendor")
    assert result.accepted_rows == 0
    assert "unparsable transaction_date" in result.quarantined_rows[0]["reason"]


def test_negative_amount_is_quarantined(agent, tmp_path):
    path = write_csv(
        tmp_path, "neg.csv",
        ["Hospital_Name", "End_to_End_Reference_ID", "Customer_Reference_ID",
         "Invoice_Number", "Payment_Method", "Transaction_Date", "Value_Date",
         "Amount", "Currency"],
        [["Klinikum Test", "E2E-1", "CUST-HOSP-9001", "INV-1",
          "SEPA Credit Transfer", "2026-01-05", "2026-01-06", "-50.00", "EUR"]],
    )
    result = agent.ingest_csv(path, "hospital_vendor")
    assert result.accepted_rows == 0
    assert "negative amount" in result.quarantined_rows[0]["reason"]


def test_one_bad_row_does_not_block_good_rows(agent, tmp_path):
    header = ["Hospital_Name", "End_to_End_Reference_ID", "Customer_Reference_ID",
              "Invoice_Number", "Payment_Method", "Transaction_Date", "Value_Date",
              "Amount", "Currency"]
    good = ["Klinikum Test", "E2E-1", "CUST-HOSP-9001", "INV-1",
            "SEPA Credit Transfer", "2026-01-05", "2026-01-06", "1200.00", "EUR"]
    bad = ["Klinikum Test", "E2E-2", "", "INV-2",
           "SEPA Credit Transfer", "2026-01-05", "2026-01-06", "1200.00", "EUR"]
    path = write_csv(tmp_path, "mixed.csv", header, [good, bad])
    result = agent.ingest_csv(path, "hospital_vendor")
    assert result.total_rows == 2
    assert result.accepted_rows == 1
    assert len(result.quarantined_rows) == 1


# ---- historical stats math ---------------------------------------------------

def test_rolling_average_math(agent, tmp_path):
    header = ["Hospital_Name", "End_to_End_Reference_ID", "Customer_Reference_ID",
              "Invoice_Number", "Payment_Method", "Transaction_Date", "Value_Date",
              "Amount", "Currency"]
    rows = [
        ["H", "E2E-1", "CUST-1", "INV-1", "SEPA Credit Transfer", "2026-01-05", "2026-01-06", "1000.00", "EUR"],
        ["H", "E2E-2", "CUST-1", "INV-2", "SEPA Credit Transfer", "2026-02-05", "2026-02-06", "2000.00", "EUR"],
        ["H", "E2E-3", "CUST-1", "INV-3", "SEPA Credit Transfer", "2026-03-05", "2026-03-06", "3000.00", "EUR"],
    ]
    path = write_csv(tmp_path, "roll.csv", header, rows)
    result = agent.ingest_csv(path, "hospital_vendor")
    stats = agent.compute_historical_stats(result.records)
    s = stats["CUST-1"]
    assert s["transaction_count"] == 3
    assert s["rolling_3m_avg"] == 2000.0   # mean(1000, 2000, 3000)
    assert s["overall_avg"] == 2000.0


def test_failed_transactions_excluded_from_baseline(agent, tmp_path):
    header = ["Member_Reference_ID", "Mandate_Reference_ID", "Payment_Method",
              "Transaction_Date", "Value_Date", "Amount", "Currency", "Status"]
    rows = [
        ["MEM-1", "MND-1", "SEPA Direct Debit", "2026-01-01", "2026-01-02", "50.00", "EUR", "Success"],
        ["MEM-1", "MND-1", "SEPA Direct Debit", "2026-02-01", "", "50.00", "EUR", "Failed"],
        ["MEM-1", "MND-1", "SEPA Direct Debit", "2026-03-01", "2026-03-02", "50.00", "EUR", "Success"],
    ]
    path = write_csv(tmp_path, "gym.csv", header, rows)
    result = agent.ingest_csv(path, "membership_club")
    stats = agent.compute_historical_stats(result.records)
    s = stats["MEM-1"]
    assert s["transaction_count"] == 2  # the Failed row must not count toward the baseline


# ---- gs:// input resolution: _resolve_csv_path is tested by monkeypatching
# google.cloud.storage, no live GCS/credentials needed. ----

from ingestion_agent import _parse_gcs_uri, _resolve_csv_path


def test_parse_gcs_uri_splits_bucket_and_blob():
    assert _parse_gcs_uri("gs://my-bucket/incoming/2026-07-16/data.csv") == (
        "my-bucket",
        "incoming/2026-07-16/data.csv",
    )


def test_parse_gcs_uri_returns_none_for_local_path():
    # local paths must be a no-op -- this is what keeps every existing test and
    # demo run working unchanged (and avoids importing google.cloud at all).
    assert _parse_gcs_uri("../ingestion_data/hospital_vendor_2yr_history.csv") is None
    assert _parse_gcs_uri("/abs/path/file.csv") is None


def test_parse_gcs_uri_rejects_malformed_uri():
    with pytest.raises(ValueError):
        _parse_gcs_uri("gs://bucket-only-no-blob")


def test_resolve_csv_path_passes_local_paths_through_unchanged():
    # no google.cloud import, no network -- a local path returns itself as-is.
    assert _resolve_csv_path("/tmp/whatever.csv") == "/tmp/whatever.csv"


def test_resolve_csv_path_downloads_gs_uri(monkeypatch, tmp_path):
    # Stand in for google.cloud.storage -- zero network/credentials needed.
    captured = {}
    sentinel_csv = tmp_path / "downloaded.csv"

    class FakeBlob:
        def __init__(self, name):
            captured["blob"] = name

        def download_to_filename(self, dest):
            captured["dest"] = dest
            # write a minimal valid hospital_vendor CSV to the resolved path
            with open(dest, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Hospital_Name", "End_to_End_Reference_ID",
                            "Customer_Reference_ID", "Invoice_Number",
                            "Payment_Method", "Transaction_Date", "Value_Date",
                            "Amount", "Currency"])
                w.writerow(["Klinikum Test", "E2E-1", "CUST-HOSP-9001", "INV-1",
                            "SEPA Credit Transfer", "2026-01-05", "2026-01-06",
                            "1200.00", "EUR"])

    class FakeBucket:
        def __init__(self, name):
            captured["bucket"] = name

        def blob(self, name):
            return FakeBlob(name)

    class FakeClient:
        def bucket(self, name):
            return FakeBucket(name)

    import types
    fake_storage = types.SimpleNamespace(Client=lambda: FakeClient())
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.storage = fake_storage
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", fake_storage)

    resolved = _resolve_csv_path("gs://my-bucket/incoming/data.csv")
    assert captured["bucket"] == "my-bucket"
    assert captured["blob"] == "incoming/data.csv"
    assert os.path.exists(resolved)

    # and the whole ingest path works when handed a gs:// URI
    result = IngestionAgent(LocalBucket(root_dir=str(tmp_path / "b"))).ingest_csv(
        "gs://my-bucket/incoming/data.csv", "hospital_vendor"
    )
    assert result.accepted_rows == 1
    assert result.records[0]["customer_reference_id"] == "CUST-HOSP-9001"
