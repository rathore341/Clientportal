UPLOAD_SCHEMAS = {
    "sales": {
        "label": "Sales",
        "table": "sales",
        "columns": ["client_id", "month", "amount"],
        "numeric_columns": ["client_id", "amount"],
    },
    "purchase": {
        "label": "Purchase",
        "table": "purchase",
        "columns": ["client_id", "month", "amount"],
        "numeric_columns": ["client_id", "amount"],
    },
    "debtors": {
        "label": "Debtors",
        "table": "debtors",
        "columns": ["client_id", "party_name", "pending_amount", "month"],
        "numeric_columns": ["client_id", "pending_amount"],
    },
    "profit_loss": {
        "label": "Profit/Loss",
        "table": "profit_loss",
        "columns": ["client_id", "month", "profit_loss_amount"],
        "numeric_columns": ["client_id", "profit_loss_amount"],
    },
}
