"""Schema configuration for the Ingestion Agent.

Each *source_type* describes one shape of raw CSV the bank might receive from a
"""


# Fields every source must resolve to. "extra" catches source-specific data
# (City/Postal_Code, Membership_Tier, ...) that's useful but not universal.
CANONICAL_FIELDS = [
    "customer_reference_id",   # stable key -- never changes for this counterparty
    "reference_id",            # end-to-end / mandate reference for this transaction
    "counterparty_name",       # display name if the source provides one (may be masked)
    "payment_method",
    "transaction_date",
    "value_date",
    "amount",
    "currency",
    "status",                  # Success / Failed -- defaults to Success if source has no concept of failure
]

SOURCE_SCHEMAS = {
    "hospital_vendor": {
        "column_map": {
            "Hospital_Name": "counterparty_name",
            "Customer_Reference_ID": "customer_reference_id",
            "End_to_End_Reference_ID": "reference_id",
            "Payment_Method": "payment_method",
            "Transaction_Date": "transaction_date",
            "Value_Date": "value_date",
            "Amount": "amount",
            "Currency": "currency",
        },
        "extra_columns": ["Invoice_Number", "City", "Postal_Code", "Phone"],
        "mask_fields": [],            # business entity name -- not PII, kept as-is
        "default_status": "Success",  # this source has no failure concept
    },
    "membership_club": {
        "column_map": {
            "Member_Reference_ID": "customer_reference_id",
            "Mandate_Reference_ID": "reference_id",
            "Payment_Method": "payment_method",
            "Transaction_Date": "transaction_date",
            "Value_Date": "value_date",
            "Amount": "amount",
            "Currency": "currency",
            "Status": "status",
        },
        "extra_columns": ["Membership_Tier", "City", "Postal_Code", "Attempt_Number"],
        "mask_fields": [],  # already a pseudonymous member ID -- nothing to mask
        "default_status": None,
    },
    # Example of a source that WOULD need masking: an individual tenant's name
    # flowing through, unlike the two sources above which are already non-PII.
    "individual_tenant": {
        "column_map": {
            "Company_Customer_Name": "counterparty_name",
            "Customer_Reference_ID": "customer_reference_id",
            "End_to_End_Reference_ID": "reference_id",
            "Payment_Method": "payment_method",
            "Transaction_Date": "transaction_date",
            "Value_Date": "value_date",
            "Amount": "amount",
            "Currency": "currency",
        },
        "extra_columns": [],
        "mask_fields": ["counterparty_name"],  # real person name -- must be hashed before it leaves ingestion
        "default_status": "Success",
    },
}
