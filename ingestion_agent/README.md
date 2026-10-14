# Ingestion Agent

Reads a raw payments CSV, maps it onto a canonical schema, masks sensitive
fields, validates every row (bad rows quarantined, not dropped), and computes
a historical baseline (monthly + rolling 3/6/12-month averages) per
counterparty. Output written to the bucket as JSON, partitioned by customer
and run date.

## Folder structure

```
kapitalwerk-nxt/
  ingestion_agent/
    schema_config.py        column mappings per source type
    masking.py               PII hashing utility
    storage.py                bucket write abstraction (local folder / GCS)
    ingestion_agent.py        the agent itself
    run_ingestion.py          demo runner
    test_ingestion_agent.py   unit tests
  ingestion_data/            sample input CSVs
  bucket_output/              agent output (created automatically)
```

## Requirements

- Python 3.10+
- pytest (tests only)

## Sample input data

In `../ingestion_data/`:

| File | Source type | What it is |
|---|---|---|
| `hospital_vendor_2yr_history.csv` | `hospital_vendor` | 2-year glucometer vendor billing baseline |
| `gym_membership_2yr_history.csv` | `membership_club` | 2-year gym membership dues baseline |
| `boho_house_2yr_history.csv` | `membership_club` | 2-year invite-only club dues baseline |
| `hospital_vendor_current_period_jul1_23.csv` | `hospital_vendor` | Current-period transactions to evaluate |

## Run it

```
cd kapitalwerk-nxt/ingestion_agent
python3 run_ingestion.py
```

## Where the output goes

```
bucket_output/{customer_id}/{run_date}/
  normalized_transactions.json   masked, canonical-schema records
  historical_stats.json           per-counterparty rolling averages
  quarantined_rows.json           rows that failed validation
```

`historical_stats.json` is what downstream agents (Inflow, Risk, Reporting)
read from.

## Ingesting a new source

Add a line to the `jobs` list in `run_ingestion.py`:

```python
("../ingestion_data/your_new_file.csv", "hospital_vendor", "your-customer-id")
```

New column names go into `SOURCE_SCHEMAS` in `schema_config.py`.

## Run the tests

```
cd kapitalwerk-nxt/ingestion_agent
python3 -m pytest test_ingestion_agent.py -v
```
