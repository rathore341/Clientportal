import json
import re
import sqlite3
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import pandas as pd
from flask import Blueprint, current_app, flash, has_request_context, redirect, render_template, request, session, url_for

from admin_auth import admin_required
from app import Client
from upload_config import UPLOAD_SCHEMAS


excel_upload_bp = Blueprint("excel_upload", __name__, url_prefix="/admin/uploads")


class DuplicateInvoiceError(ValueError):
    pass


TALLY_SALES_COLUMNS = [
    "date",
    "particulars",
    "voucher_no.",
    "gstin_uin",
    "sale",
    "cgst",
    "sgst",
    "igst",
    "round_off",
    "gross_total",
]
TALLY_PURCHASE_COLUMNS = [
    "date",
    "particulars",
    "voucher_no.",
    "gstin_uin",
    "purchase",
    "cgst",
    "sgst",
    "igst",
    "round_off",
    "gross_total",
]


def normalize_column_name(column_name):
    return str(column_name).strip().lower().replace(" ", "_").replace("/", "_")


def get_upload_db_path():
    configured_path = current_app.config.get("UPLOAD_SQLITE_DB", "uploads.sqlite3")
    configured_path = Path(configured_path)
    if configured_path.is_absolute():
        db_path = configured_path
    else:
        db_path = Path(current_app.root_path).joinpath(configured_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def connect_upload_database():
    connection = sqlite3.connect(get_upload_db_path())
    connection.execute("PRAGMA journal_mode=MEMORY")
    connection.execute("PRAGMA temp_store=MEMORY")
    return connection


def financial_year_for_date(value=None):
    value = value or date.today()
    parsed_date = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed_date):
        parsed_date = pd.Timestamp(date.today())
    start_year = parsed_date.year if parsed_date.month >= 4 else parsed_date.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def current_financial_year():
    return financial_year_for_date(date.today())


def add_column_if_missing(connection, table_name, column_name, column_definition):
    columns = {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in columns:
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def db_client_missing(client_id):
    return Client.query.get(client_id) is None


def log_tally_sync_history(
    action,
    status,
    client=None,
    tally_url="",
    tally_company_name="",
    from_date="",
    to_date="",
    month="",
    fetched_count=0,
    imported_count=0,
    error_count=0,
    message="",
):
    init_upload_database()
    with connect_upload_database() as connection:
        connection.execute(
            """
            INSERT INTO tally_sync_history (
                admin_name,
                client_id,
                client_name,
                action,
                status,
                tally_url,
                tally_company_name,
                from_date,
                to_date,
                month,
                fetched_count,
                imported_count,
                error_count,
                message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.get("admin_name", ""),
                client.id if client else None,
                client.company_name if client else "",
                action,
                status,
                tally_url,
                tally_company_name,
                from_date,
                to_date,
                month,
                int(fetched_count or 0),
                int(imported_count or 0),
                int(error_count or 0),
                message,
            ),
        )


def init_upload_database():
    db_path = get_upload_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with connect_upload_database() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                financial_year TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                financial_year TEXT NOT NULL DEFAULT '',
                row_data TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS purchase (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                financial_year TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS purchase_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                financial_year TEXT NOT NULL DEFAULT '',
                row_data TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS debtors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                party_name TEXT NOT NULL,
                pending_amount REAL NOT NULL,
                month TEXT NOT NULL,
                financial_year TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS profit_loss (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                financial_year TEXT NOT NULL DEFAULT '',
                profit_loss_amount REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tally_sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                admin_name TEXT,
                client_id INTEGER,
                client_name TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                tally_url TEXT,
                tally_company_name TEXT,
                from_date TEXT,
                to_date TEXT,
                month TEXT,
                fetched_count INTEGER NOT NULL DEFAULT 0,
                imported_count INTEGER NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0,
                message TEXT
            )
            """
        )
        for table_name in ["sales", "sales_invoices", "purchase", "purchase_invoices", "debtors", "profit_loss"]:
            add_column_if_missing(
                connection,
                table_name,
                "financial_year",
                "TEXT NOT NULL DEFAULT ''",
            )
            connection.execute(
                f"""
                UPDATE {table_name}
                SET financial_year = ?
                WHERE financial_year IS NULL OR financial_year = ''
                """,
                (current_financial_year(),),
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tally_locked_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                locked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                locked_by TEXT,
                UNIQUE(client_id, month)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tally_pending_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                admin_name TEXT,
                client_id INTEGER NOT NULL,
                client_name TEXT,
                month TEXT NOT NULL,
                from_date TEXT,
                to_date TEXT,
                tally_company_name TEXT,
                import_mode TEXT,
                invoice_no TEXT,
                party_name TEXT,
                invoice_date TEXT,
                row_data TEXT NOT NULL,
                errors TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )


def read_validated_excel(file_storage, upload_type, client_id=None):
    schema = UPLOAD_SCHEMAS[upload_type]
    dataframe = pd.read_excel(file_storage)
    dataframe.columns = [normalize_column_name(column) for column in dataframe.columns]

    required_columns = list(schema["columns"])
    if client_id:
        required_columns = [column for column in required_columns if column != "client_id"]

    missing_columns = [column for column in required_columns if column not in dataframe.columns]
    if missing_columns:
        readable_columns = ", ".join(missing_columns)
        raise ValueError(f"Missing required column(s): {readable_columns}")

    dataframe = dataframe[required_columns].copy()
    dataframe = dataframe.dropna(how="all")

    for column in schema["numeric_columns"]:
        if column not in dataframe.columns:
            continue
        dataframe[column] = pd.to_numeric(dataframe[column], errors="coerce")

    if dataframe.empty:
        raise ValueError("The uploaded Excel file has no data rows.")

    if dataframe[required_columns].isnull().any().any():
        raise ValueError("Some required cells are blank or contain invalid values.")

    if client_id:
        dataframe.insert(0, "client_id", int(client_id))
    else:
        dataframe["client_id"] = dataframe["client_id"].astype(int)

    return dataframe


def detect_month_from_period(period_value):
    first_date = str(period_value).split(" to ")[0]
    parsed_date = pd.to_datetime(first_date, errors="coerce")
    if not pd.isna(parsed_date):
        return parsed_date.strftime("%B")
    return None


def parse_date(value, field_label):
    parsed_date = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed_date):
        raise ValueError(f"Please enter a valid {field_label}.")
    return parsed_date.to_pydatetime()


def parse_money(value):
    if value is None:
        return 0

    normalized_value = str(value).strip().replace(",", "")
    if not normalized_value:
        return 0

    multiplier = -1 if normalized_value.startswith("(") and normalized_value.endswith(")") else 1
    number_match = re.search(r"-?\d+(?:\.\d+)?", normalized_value)
    if not number_match:
        return 0

    return abs(float(number_match.group(0)) * multiplier)


def serialize_cell(value):
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value.item() if hasattr(value, "item") else value


def normalize_invoice_number(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def normalize_company_name(value):
    normalized_value = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return " ".join(normalized_value.split())


def describe_invoice(row):
    invoice_number = normalize_invoice_number(row.get("voucher_no."))
    name = row.get("particulars") or "Unknown party"
    date = row.get("date") or "Unknown date"
    if isinstance(date, str) and "T" in date:
        date = date.split("T", 1)[0]
    return f"Invoice No. {invoice_number} ({name}, {date})"


def validate_unique_sales_invoices(invoice_rows, client_id):
    invoice_numbers = {}
    duplicate_upload_rows = []

    for row in invoice_rows:
        invoice_number = normalize_invoice_number(row.get("voucher_no."))
        if not invoice_number:
            continue

        if invoice_number in invoice_numbers:
            duplicate_upload_rows.append(describe_invoice(row))
            continue

        invoice_numbers[invoice_number] = row

    if duplicate_upload_rows:
        duplicates = "; ".join(duplicate_upload_rows[:5])
        raise DuplicateInvoiceError(f"Duplicate data entry in uploaded file: {duplicates}")

    if not invoice_numbers:
        return

    existing_duplicates = []
    with connect_upload_database() as connection:
        existing_rows = connection.execute(
            """
            SELECT row_data
            FROM sales_invoices
            WHERE client_id = ?
            """,
            (client_id,),
        ).fetchall()

    for (row_data,) in existing_rows:
        existing_row = json.loads(row_data)
        existing_invoice_number = normalize_invoice_number(existing_row.get("voucher_no."))
        if existing_invoice_number in invoice_numbers:
            existing_duplicates.append(describe_invoice(existing_row))

    if existing_duplicates:
        duplicates = "; ".join(existing_duplicates[:5])
        raise DuplicateInvoiceError(f"Duplicate data entry already exists: {duplicates}")


def get_existing_invoice_numbers(client_id):
    init_upload_database()
    existing_invoice_numbers = set()
    with connect_upload_database() as connection:
        existing_rows = connection.execute(
            """
            SELECT row_data
            FROM sales_invoices
            WHERE client_id = ?
            """,
            (client_id,),
        ).fetchall()

    for (row_data,) in existing_rows:
        existing_row = json.loads(row_data)
        invoice_number = normalize_invoice_number(existing_row.get("voucher_no."))
        if invoice_number:
            existing_invoice_numbers.add(invoice_number)

    return existing_invoice_numbers


def get_existing_invoice_map(client_id):
    init_upload_database()
    existing_invoices = {}
    with connect_upload_database() as connection:
        existing_rows = connection.execute(
            """
            SELECT id, row_data
            FROM sales_invoices
            WHERE client_id = ?
            """,
            (client_id,),
        ).fetchall()

    for invoice_id, row_data in existing_rows:
        existing_row = json.loads(row_data)
        invoice_number = normalize_invoice_number(existing_row.get("voucher_no."))
        if invoice_number:
            existing_invoices[invoice_number] = {"id": invoice_id, "row": existing_row}

    return existing_invoices


def get_existing_purchase_invoice_map(client_id):
    init_upload_database()
    existing_invoices = {}
    with connect_upload_database() as connection:
        existing_rows = connection.execute(
            """
            SELECT id, row_data
            FROM purchase_invoices
            WHERE client_id = ?
            """,
            (client_id,),
        ).fetchall()

    for invoice_id, row_data in existing_rows:
        existing_row = json.loads(row_data)
        invoice_number = normalize_invoice_number(existing_row.get("voucher_no."))
        if invoice_number:
            existing_invoices[invoice_number] = {"id": invoice_id, "row": existing_row}

    return existing_invoices


def is_tally_period_locked(client_id, month):
    init_upload_database()
    with connect_upload_database() as connection:
        row = connection.execute(
            """
            SELECT id
            FROM tally_locked_periods
            WHERE client_id = ? AND month = ?
            """,
            (client_id, month),
        ).fetchone()
    return row is not None


def set_tally_period_lock(client_id, month, locked, admin_name=""):
    init_upload_database()
    with connect_upload_database() as connection:
        if locked:
            connection.execute(
                """
                INSERT OR IGNORE INTO tally_locked_periods (client_id, month, locked_by)
                VALUES (?, ?, ?)
                """,
                (client_id, month, admin_name),
            )
        else:
            connection.execute(
                """
                DELETE FROM tally_locked_periods
                WHERE client_id = ? AND month = ?
                """,
                (client_id, month),
            )


def save_tally_pending_corrections(
    client,
    month,
    from_date,
    to_date,
    tally_company_name,
    import_mode,
    validation_rows,
):
    invalid_rows = [validation_row for validation_row in validation_rows if not validation_row["is_valid"]]
    if not invalid_rows:
        return 0

    init_upload_database()
    admin_name = session.get("admin_name", "") if has_request_context() else ""
    with connect_upload_database() as connection:
        connection.executemany(
            """
            INSERT INTO tally_pending_corrections (
                admin_name,
                client_id,
                client_name,
                month,
                from_date,
                to_date,
                tally_company_name,
                import_mode,
                invoice_no,
                party_name,
                invoice_date,
                row_data,
                errors,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            [
                (
                    admin_name,
                    client.id,
                    client.company_name,
                    month,
                    from_date,
                    to_date,
                    tally_company_name,
                    import_mode,
                    normalize_invoice_number(validation_row["row"].get("voucher_no.")),
                    str(validation_row["row"].get("particulars") or "").strip(),
                    str(validation_row["row"].get("date") or "").strip(),
                    json.dumps(validation_row["row"], default=str),
                    json.dumps(validation_row["errors"], default=str),
                )
                for validation_row in invalid_rows
            ],
        )

    return len(invalid_rows)


def fetch_tally_pending_corrections(limit=200):
    init_upload_database()
    columns = [
        "id",
        "created_at",
        "admin_name",
        "client_name",
        "month",
        "from_date",
        "to_date",
        "tally_company_name",
        "import_mode",
        "invoice_no",
        "party_name",
        "invoice_date",
        "row_data",
        "errors",
        "status",
    ]
    with connect_upload_database() as connection:
        rows = connection.execute(
            f"""
            SELECT {", ".join(columns)}
            FROM tally_pending_corrections
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    records = []
    for row in rows:
        record = dict(zip(columns, row))
        try:
            record["errors"] = json.loads(record["errors"])
        except (TypeError, json.JSONDecodeError):
            record["errors"] = [record["errors"]]
        try:
            record["row_data"] = json.loads(record["row_data"])
        except (TypeError, json.JSONDecodeError):
            record["row_data"] = {}
        records.append(record)
    return records


def validate_tally_voucher_rows(
    invoice_rows,
    client_id,
    from_date_value,
    to_date_value,
    amount_key,
    amount_label,
    existing_rows_getter,
):
    from_date = parse_date(from_date_value, "from date").date()
    to_date = parse_date(to_date_value, "to date").date()
    existing_invoices = existing_rows_getter(client_id)
    seen_invoice_numbers = {}
    validation_rows = []

    for index, row in enumerate(invoice_rows):
        errors = []
        invoice_number = normalize_invoice_number(row.get("voucher_no."))
        party_name = str(row.get("particulars") or "").strip()
        parsed_date = pd.to_datetime(row.get("date"), errors="coerce")
        base_amount = parse_money(row.get(amount_key))
        cgst_amount = parse_money(row.get("cgst"))
        sgst_amount = parse_money(row.get("sgst"))
        igst_amount = parse_money(row.get("igst"))
        round_off_amount = parse_money(row.get("round_off"))
        invoice_value = parse_money(row.get("gross_total"))

        if not invoice_number:
            errors.append("Invoice number is required.")
        elif invoice_number in seen_invoice_numbers:
            errors.append(
                f"Duplicate invoice number in fetched data; first found on row {seen_invoice_numbers[invoice_number] + 1}."
            )
        else:
            seen_invoice_numbers[invoice_number] = index

        if pd.isna(parsed_date):
            errors.append("Invoice date is required and must be valid.")
        else:
            invoice_date = parsed_date.date()
            if invoice_date < from_date or invoice_date > to_date:
                errors.append("Invoice date is outside the selected date range.")

        if not party_name:
            errors.append("Party name is required.")

        if base_amount <= 0:
            errors.append(f"{amount_label} amount must be greater than zero.")

        if invoice_value <= 0:
            errors.append("Invoice value must be greater than zero.")

        expected_invoice_value = base_amount + cgst_amount + sgst_amount + igst_amount + round_off_amount
        if invoice_value > 0 and abs(invoice_value - expected_invoice_value) > 1:
            errors.append(
                f"Invoice value must match {amount_label} + CGST + SGST + IGST + Round Off within Rs. 1."
            )

        if cgst_amount > 0 and sgst_amount > 0 and abs(cgst_amount - sgst_amount) > 1:
            errors.append("CGST and SGST must be equal within Rs. 1.")

        if igst_amount > 0 and (cgst_amount > 0 or sgst_amount > 0):
            errors.append("IGST cannot exist with CGST or SGST on the same invoice.")

        is_existing = bool(invoice_number and invoice_number in existing_invoices)
        validation_rows.append(
            {
                "row": row,
                "errors": errors,
                "is_existing": is_existing,
                "is_valid": not errors,
            }
        )

    return validation_rows


def validate_tally_invoice_rows(invoice_rows, client_id, from_date_value, to_date_value):
    return validate_tally_voucher_rows(
        invoice_rows,
        client_id,
        from_date_value,
        to_date_value,
        "sale",
        "Sale",
        get_existing_invoice_map,
    )


def validate_tally_purchase_rows(invoice_rows, client_id, from_date_value, to_date_value):
    return validate_tally_voucher_rows(
        invoice_rows,
        client_id,
        from_date_value,
        to_date_value,
        "purchase",
        "Purchase",
        get_existing_purchase_invoice_map,
    )


def validate_tally_company_match(client_company_name, tally_company_name):
    normalized_client_name = normalize_company_name(client_company_name)
    normalized_tally_name = normalize_company_name(tally_company_name)

    if not normalized_tally_name:
        return None

    if normalized_client_name == normalized_tally_name:
        return None

    if normalized_client_name in normalized_tally_name or normalized_tally_name in normalized_client_name:
        return None

    return (
        f"Tally company name '{tally_company_name}' does not match the selected client "
        f"'{client_company_name}'."
    )


def build_sales_dataframe_from_invoice_rows(invoice_rows, client_id, month, financial_year=None):
    financial_year = financial_year or current_financial_year()
    total_sales = sum(parse_money(row.get("sale")) for row in invoice_rows)
    dataframe = pd.DataFrame(
        [
            {
                "client_id": int(client_id),
                "month": month,
                "financial_year": financial_year,
                "amount": float(total_sales),
            }
        ]
    )
    dataframe.attrs["sales_invoice_rows"] = invoice_rows
    return dataframe


def recalculate_uploaded_sales_total(connection, client_id, month, financial_year):
    rows = connection.execute(
        """
        SELECT row_data
        FROM sales_invoices
        WHERE client_id = ? AND month = ? AND financial_year = ?
        """,
        (client_id, month, financial_year),
    ).fetchall()
    total_sales = sum(parse_money(json.loads(row_data).get("sale")) for (row_data,) in rows)

    connection.execute(
        """
        DELETE FROM sales
        WHERE client_id = ? AND month = ? AND financial_year = ?
        """,
        (client_id, month, financial_year),
    )
    if total_sales:
        connection.execute(
            """
            INSERT INTO sales (client_id, month, financial_year, amount)
            VALUES (?, ?, ?, ?)
            """,
            (client_id, month, financial_year, total_sales),
        )


def recalculate_uploaded_purchase_total(connection, client_id, month, financial_year):
    rows = connection.execute(
        """
        SELECT row_data
        FROM purchase_invoices
        WHERE client_id = ? AND month = ? AND financial_year = ?
        """,
        (client_id, month, financial_year),
    ).fetchall()
    total_purchase = sum(parse_money(json.loads(row_data).get("purchase")) for (row_data,) in rows)

    connection.execute(
        """
        DELETE FROM purchase
        WHERE client_id = ? AND month = ? AND financial_year = ?
        """,
        (client_id, month, financial_year),
    )
    if total_purchase:
        connection.execute(
            """
            INSERT INTO purchase (client_id, month, financial_year, amount)
            VALUES (?, ?, ?, ?)
            """,
            (client_id, month, financial_year, total_purchase),
        )


def save_tally_invoice_rows(client_id, month, invoice_rows, import_mode, financial_year=None):
    financial_year = financial_year or current_financial_year()
    if is_tally_period_locked(client_id, month):
        raise ValueError(f"{month} is locked for this client. Unlock the period before importing.")

    if import_mode not in {"new_only", "update_existing"}:
        import_mode = "new_only"

    inserted_count = 0
    updated_count = 0
    skipped_count = 0

    init_upload_database()
    with connect_upload_database() as connection:
        existing_rows = connection.execute(
            """
            SELECT id, row_data
            FROM sales_invoices
            WHERE client_id = ? AND financial_year = ?
            """,
            (client_id, financial_year),
        ).fetchall()
        existing_by_invoice = {}
        for invoice_id, row_data in existing_rows:
            row = json.loads(row_data)
            invoice_number = normalize_invoice_number(row.get("voucher_no."))
            if invoice_number:
                existing_by_invoice[invoice_number] = invoice_id

        for row in invoice_rows:
            invoice_number = normalize_invoice_number(row.get("voucher_no."))
            existing_id = existing_by_invoice.get(invoice_number)
            row_data = json.dumps(row, default=str)

            if existing_id and import_mode == "new_only":
                skipped_count += 1
                continue

            if existing_id and import_mode == "update_existing":
                connection.execute(
                    """
                    UPDATE sales_invoices
                    SET month = ?, financial_year = ?, row_data = ?
                    WHERE id = ? AND client_id = ?
                    """,
                    (month, financial_year, row_data, existing_id, client_id),
                )
                updated_count += 1
                continue

            connection.execute(
                """
                INSERT INTO sales_invoices (client_id, month, financial_year, row_data)
                VALUES (?, ?, ?, ?)
                """,
                (client_id, month, financial_year, row_data),
            )
            inserted_count += 1

        recalculate_uploaded_sales_total(connection, client_id, month, financial_year)

    return {
        "inserted": inserted_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "imported": inserted_count + updated_count,
    }


def save_tally_purchase_rows(client_id, month, invoice_rows, import_mode, financial_year=None):
    financial_year = financial_year or current_financial_year()
    if is_tally_period_locked(client_id, month):
        raise ValueError(f"{month} is locked for this client. Unlock the period before importing.")

    if import_mode not in {"new_only", "update_existing"}:
        import_mode = "new_only"

    inserted_count = 0
    updated_count = 0
    skipped_count = 0

    init_upload_database()
    with connect_upload_database() as connection:
        existing_rows = connection.execute(
            """
            SELECT id, row_data
            FROM purchase_invoices
            WHERE client_id = ? AND financial_year = ?
            """,
            (client_id, financial_year),
        ).fetchall()
        existing_by_invoice = {}
        for invoice_id, row_data in existing_rows:
            row = json.loads(row_data)
            invoice_number = normalize_invoice_number(row.get("voucher_no."))
            if invoice_number:
                existing_by_invoice[invoice_number] = invoice_id

        for row in invoice_rows:
            invoice_number = normalize_invoice_number(row.get("voucher_no."))
            existing_id = existing_by_invoice.get(invoice_number)
            row_data = json.dumps(row, default=str)

            if existing_id and import_mode == "new_only":
                skipped_count += 1
                continue

            if existing_id and import_mode == "update_existing":
                connection.execute(
                    """
                    UPDATE purchase_invoices
                    SET month = ?, financial_year = ?, row_data = ?
                    WHERE id = ? AND client_id = ?
                    """,
                    (month, financial_year, row_data, existing_id, client_id),
                )
                updated_count += 1
                continue

            connection.execute(
                """
                INSERT INTO purchase_invoices (client_id, month, financial_year, row_data)
                VALUES (?, ?, ?, ?)
                """,
                (client_id, month, financial_year, row_data),
            )
            inserted_count += 1

        recalculate_uploaded_purchase_total(connection, client_id, month, financial_year)

    return {
        "inserted": inserted_count,
        "updated": updated_count,
        "skipped": skipped_count,
        "imported": inserted_count + updated_count,
    }


def is_valid_xml_codepoint(value):
    return (
        value in (0x9, 0xA, 0xD)
        or 0x20 <= value <= 0xD7FF
        or 0xE000 <= value <= 0xFFFD
        or 0x10000 <= value <= 0x10FFFF
    )


def sanitize_xml_text(xml_text):
    def replace_character_reference(match):
        is_hex = match.group(1).lower() == "x"
        raw_value = match.group(2)

        try:
            codepoint = int(raw_value, 16 if is_hex else 10)
        except ValueError:
            return ""

        return match.group(0) if is_valid_xml_codepoint(codepoint) else ""

    xml_text = re.sub(r"&#([xX]?)([0-9A-Fa-f]+);", replace_character_reference, xml_text)
    return "".join(character for character in xml_text if is_valid_xml_codepoint(ord(character)))


def get_xml_error_snippet(xml_text, position):
    if not position:
        return ""

    line_number, column_number = position
    lines = xml_text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return ""

    line = lines[line_number - 1]
    start = max(column_number - 80, 0)
    end = min(column_number + 80, len(line))
    return line[start:end]


def save_tally_debug_xml(raw_xml, sanitized_xml):
    debug_dir = Path(current_app.instance_path)
    debug_dir.mkdir(parents=True, exist_ok=True)
    debug_dir.joinpath("tally_last_response.xml").write_text(raw_xml, encoding="utf-8", errors="replace")
    debug_dir.joinpath("tally_last_sanitized.xml").write_text(
        sanitized_xml,
        encoding="utf-8",
        errors="replace",
    )


def clean_xml_tag(tag_name):
    return str(tag_name).split("}", 1)[-1].upper()


def find_all_by_tag(element, tag_name):
    tag_name = tag_name.upper()
    return [child for child in element.iter() if clean_xml_tag(child.tag) == tag_name]


def find_all_by_tag_names(element, tag_names):
    normalized_tag_names = {tag_name.upper() for tag_name in tag_names}
    return [
        child
        for child in element.iter()
        if clean_xml_tag(child.tag) in normalized_tag_names
    ]


def child_text(element, names, default=""):
    normalized_names = {name.upper() for name in names}
    for child in element:
        if clean_xml_tag(child.tag) in normalized_names:
            return (child.text or "").strip()
    return default


def direct_or_descendant_text(element, names, default=""):
    direct_value = child_text(element, names, default="")
    if direct_value:
        return direct_value
    return descendant_text(element, names, default=default)


def descendant_text(element, names, default=""):
    normalized_names = {name.upper() for name in names}
    for child in element.iter():
        if clean_xml_tag(child.tag) in normalized_names:
            return (child.text or "").strip()
    return default


def extract_tally_company_name(root):
    company_tags = ["SVCURRENTCOMPANY", "CURRENTCOMPANY", "COMPANYNAME", "CMPNAME"]
    for company_tag in company_tags:
        matches = find_all_by_tag(root, company_tag)
        for match in matches:
            company_name = (match.text or "").strip()
            if company_name:
                return company_name
    return ""


def is_sales_ledger(ledger_name):
    ledger_name = ledger_name.lower()
    tax_words = ("cgst", "sgst", "igst", "gst", "tax", "round", "freight", "packing")
    return ("sale" in ledger_name or "sales" in ledger_name) and not any(
        word in ledger_name for word in tax_words
    )


def is_purchase_ledger(ledger_name):
    ledger_name = ledger_name.lower()
    tax_words = ("cgst", "sgst", "igst", "gst", "tax", "round", "freight", "packing")
    return ("purchase" in ledger_name or "purchases" in ledger_name) and not any(
        word in ledger_name for word in tax_words
    )


def add_amount_by_ledger_name(amounts, ledger_name, amount):
    ledger_name = ledger_name.lower()
    if not amount:
        return

    if "cgst" in ledger_name:
        amounts["cgst"] += amount
    elif "sgst" in ledger_name:
        amounts["sgst"] += amount
    elif "igst" in ledger_name:
        amounts["igst"] += amount
    elif "round" in ledger_name:
        amounts["round_off"] += amount
    elif is_sales_ledger(ledger_name):
        amounts["sale"] += amount


def add_purchase_amount_by_ledger_name(amounts, ledger_name, amount):
    ledger_name = ledger_name.lower()
    if not amount:
        return

    if "cgst" in ledger_name:
        amounts["cgst"] += amount
    elif "sgst" in ledger_name:
        amounts["sgst"] += amount
    elif "igst" in ledger_name:
        amounts["igst"] += amount
    elif "round" in ledger_name:
        amounts["round_off"] += amount
    elif is_purchase_ledger(ledger_name):
        amounts["purchase"] += amount


def build_tally_sales_request(from_date, to_date):
    from_date_text = from_date.strftime("%Y%m%d")
    to_date_text = to_date.strftime("%Y%m%d")
    return f"""
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Voucher Register</REPORTNAME>
        <STATICVARIABLES>
          <SVFROMDATE>{from_date_text}</SVFROMDATE>
          <SVTODATE>{to_date_text}</SVTODATE>
          <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
""".strip()


def build_tally_purchase_request(from_date, to_date):
    from_date_text = from_date.strftime("%Y%m%d")
    to_date_text = to_date.strftime("%Y%m%d")
    return f"""
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Voucher Register</REPORTNAME>
        <STATICVARIABLES>
          <SVFROMDATE>{from_date_text}</SVFROMDATE>
          <SVTODATE>{to_date_text}</SVTODATE>
          <VOUCHERTYPENAME>Purchase</VOUCHERTYPENAME>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
""".strip()


def build_tally_company_request():
    return """
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Export Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>List of Companies</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>
""".strip()


def post_tally_xml(tally_url, request_xml):
    request_object = urllib.request.Request(
        tally_url,
        data=request_xml.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
        method="POST",
    )
    timeout = int(current_app.config.get("TALLY_TIMEOUT_SECONDS", 20))

    try:
        with urllib.request.urlopen(request_object, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not connect to Tally at {tally_url}: {exc.reason}") from exc


def fetch_tally_xml(tally_url, from_date, to_date):
    return post_tally_xml(tally_url, build_tally_sales_request(from_date, to_date))


def fetch_tally_purchase_xml(tally_url, from_date, to_date):
    return post_tally_xml(tally_url, build_tally_purchase_request(from_date, to_date))


def test_tally_connection(tally_url):
    xml_text = post_tally_xml(tally_url, build_tally_company_request())
    sanitized_xml = sanitize_xml_text(xml_text)
    try:
        root = ET.fromstring(sanitized_xml)
    except ET.ParseError as exc:
        save_tally_debug_xml(xml_text, sanitized_xml)
        raise ValueError(f"Tally responded, but returned invalid XML: {exc}") from exc

    tally_company_name = extract_tally_company_name(root)
    return tally_company_name


def parse_tally_sales_xml(xml_text):
    sanitized_xml = sanitize_xml_text(xml_text)
    try:
        root = ET.fromstring(sanitized_xml)
    except ET.ParseError as exc:
        save_tally_debug_xml(xml_text, sanitized_xml)
        snippet = get_xml_error_snippet(sanitized_xml, getattr(exc, "position", None))
        if snippet:
            raise ValueError(f"Tally returned invalid XML: {exc}. Near: {snippet}") from exc
        raise ValueError(
            f"Tally returned invalid XML: {exc}. Saved debug files in the instance folder."
        ) from exc

    tally_company_name = extract_tally_company_name(root)
    voucher_rows = []
    for voucher in find_all_by_tag(root, "VOUCHER"):
        voucher_type = descendant_text(voucher, ["VOUCHERTYPENAME", "VCHTYPE"])
        if voucher_type and "sales" not in voucher_type.lower():
            continue

        invoice_number = child_text(voucher, ["VOUCHERNUMBER", "VOUCHERNO", "REFERENCE"])
        if not invoice_number:
            continue

        raw_date = child_text(voucher, ["DATE", "EFFECTIVEDATE"])
        parsed_date = pd.to_datetime(raw_date, errors="coerce")
        date_value = "" if pd.isna(parsed_date) else parsed_date.strftime("%Y-%m-%d")
        party_name = child_text(voucher, ["PARTYLEDGERNAME", "PARTYNAME", "BASICBUYERNAME"])
        gstin = descendant_text(voucher, ["PARTYGSTIN", "GSTIN", "GSTIN/UIN", "PARTYGSTIN/UIN"])

        amounts = {"sale": 0, "cgst": 0, "sgst": 0, "igst": 0, "round_off": 0}
        invoice_value = 0

        ledger_entries = find_all_by_tag_names(
            voucher,
            ["ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"],
        )
        for ledger in ledger_entries:
            ledger_name = direct_or_descendant_text(ledger, ["LEDGERNAME"]).lower()
            amount = parse_money(child_text(ledger, ["AMOUNT"]))
            is_party_ledger = child_text(ledger, ["ISPARTYLEDGER"]).lower() == "yes"

            if is_party_ledger:
                invoice_value = max(invoice_value, amount)
            else:
                add_amount_by_ledger_name(amounts, ledger_name, amount)

        allocation_amounts = {"sale": 0, "cgst": 0, "sgst": 0, "igst": 0, "round_off": 0}
        accounting_allocations = find_all_by_tag(voucher, "ACCOUNTINGALLOCATIONS.LIST")
        for allocation in accounting_allocations:
            ledger_name = direct_or_descendant_text(allocation, ["LEDGERNAME"])
            amount = parse_money(child_text(allocation, ["AMOUNT"]))
            add_amount_by_ledger_name(allocation_amounts, ledger_name, amount)

        for amount_key, amount_value in allocation_amounts.items():
            if not amounts[amount_key] and amount_value:
                amounts[amount_key] = amount_value

        inventory_entries = find_all_by_tag_names(
            voucher,
            ["ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIES.LIST"],
        )
        inventory_sale_amount = sum(
            parse_money(child_text(entry, ["AMOUNT"])) for entry in inventory_entries
        )

        if not amounts["sale"] and inventory_sale_amount:
            amounts["sale"] = inventory_sale_amount

        if not invoice_value:
            invoice_value = (
                amounts["sale"]
                + amounts["cgst"]
                + amounts["sgst"]
                + amounts["igst"]
                + amounts["round_off"]
            )

        voucher_rows.append(
            {
                "date": date_value,
                "particulars": party_name,
                "voucher_no.": invoice_number,
                "gstin_uin": gstin,
                "sale": amounts["sale"],
                "cgst": amounts["cgst"],
                "sgst": amounts["sgst"],
                "igst": amounts["igst"],
                "round_off": amounts["round_off"],
                "gross_total": invoice_value,
            }
        )

    if not voucher_rows:
        raise ValueError("No sales vouchers were returned by Tally for this period.")

    return voucher_rows, tally_company_name


def parse_tally_purchase_xml(xml_text):
    sanitized_xml = sanitize_xml_text(xml_text)
    try:
        root = ET.fromstring(sanitized_xml)
    except ET.ParseError as exc:
        save_tally_debug_xml(xml_text, sanitized_xml)
        snippet = get_xml_error_snippet(sanitized_xml, getattr(exc, "position", None))
        if snippet:
            raise ValueError(f"Tally returned invalid XML: {exc}. Near: {snippet}") from exc
        raise ValueError(
            f"Tally returned invalid XML: {exc}. Saved debug files in the instance folder."
        ) from exc

    tally_company_name = extract_tally_company_name(root)
    voucher_rows = []
    for voucher in find_all_by_tag(root, "VOUCHER"):
        voucher_type = descendant_text(voucher, ["VOUCHERTYPENAME", "VCHTYPE"])
        if voucher_type and "purchase" not in voucher_type.lower():
            continue

        invoice_number = child_text(voucher, ["VOUCHERNUMBER", "VOUCHERNO", "REFERENCE"])
        if not invoice_number:
            continue

        raw_date = child_text(voucher, ["DATE", "EFFECTIVEDATE"])
        parsed_date = pd.to_datetime(raw_date, errors="coerce")
        date_value = "" if pd.isna(parsed_date) else parsed_date.strftime("%Y-%m-%d")
        party_name = child_text(voucher, ["PARTYLEDGERNAME", "PARTYNAME", "BASICBUYERNAME"])
        gstin = descendant_text(voucher, ["PARTYGSTIN", "GSTIN", "GSTIN/UIN", "PARTYGSTIN/UIN"])

        amounts = {"purchase": 0, "cgst": 0, "sgst": 0, "igst": 0, "round_off": 0}
        invoice_value = 0

        ledger_entries = find_all_by_tag_names(voucher, ["ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"])
        for ledger in ledger_entries:
            ledger_name = direct_or_descendant_text(ledger, ["LEDGERNAME"]).lower()
            amount = parse_money(child_text(ledger, ["AMOUNT"]))
            is_party_ledger = child_text(ledger, ["ISPARTYLEDGER"]).lower() == "yes"
            if is_party_ledger:
                invoice_value = max(invoice_value, amount)
            else:
                add_purchase_amount_by_ledger_name(amounts, ledger_name, amount)

        accounting_allocations = find_all_by_tag(voucher, "ACCOUNTINGALLOCATIONS.LIST")
        for allocation in accounting_allocations:
            ledger_name = direct_or_descendant_text(allocation, ["LEDGERNAME"])
            amount = parse_money(child_text(allocation, ["AMOUNT"]))
            add_purchase_amount_by_ledger_name(amounts, ledger_name, amount)

        inventory_entries = find_all_by_tag_names(voucher, ["ALLINVENTORYENTRIES.LIST", "INVENTORYENTRIES.LIST"])
        inventory_purchase_amount = sum(parse_money(child_text(entry, ["AMOUNT"])) for entry in inventory_entries)
        if not amounts["purchase"] and inventory_purchase_amount:
            amounts["purchase"] = inventory_purchase_amount

        if not invoice_value:
            invoice_value = (
                amounts["purchase"]
                + amounts["cgst"]
                + amounts["sgst"]
                + amounts["igst"]
                + amounts["round_off"]
            )

        voucher_rows.append(
            {
                "date": date_value,
                "particulars": party_name,
                "voucher_no.": invoice_number,
                "gstin_uin": gstin,
                "purchase": amounts["purchase"],
                "cgst": amounts["cgst"],
                "sgst": amounts["sgst"],
                "igst": amounts["igst"],
                "round_off": amounts["round_off"],
                "gross_total": invoice_value,
            }
        )

    if not voucher_rows:
        raise ValueError("No purchase vouchers were returned by Tally for this period.")

    return voucher_rows, tally_company_name


def fetch_tally_sales_rows(tally_url, from_date_value, to_date_value, client_id):
    from_date = parse_date(from_date_value, "from date")
    to_date = parse_date(to_date_value, "to date")

    if from_date > to_date:
        raise ValueError("From date cannot be after to date.")

    if from_date.strftime("%B") != to_date.strftime("%B"):
        raise ValueError("Please fetch one month at a time so the dashboard month stays accurate.")

    xml_text = fetch_tally_xml(tally_url, from_date, to_date)
    invoice_rows, tally_company_name = parse_tally_sales_xml(xml_text)
    return invoice_rows, from_date.strftime("%B"), tally_company_name


def fetch_tally_purchase_rows(tally_url, from_date_value, to_date_value, client_id):
    from_date = parse_date(from_date_value, "from date")
    to_date = parse_date(to_date_value, "to date")

    if from_date > to_date:
        raise ValueError("From date cannot be after to date.")

    if from_date.strftime("%B") != to_date.strftime("%B"):
        raise ValueError("Please fetch one month at a time so the dashboard month stays accurate.")

    xml_text = fetch_tally_purchase_xml(tally_url, from_date, to_date)
    invoice_rows, tally_company_name = parse_tally_purchase_xml(xml_text)
    return invoice_rows, from_date.strftime("%B"), tally_company_name


def read_tally_sales_excel(file_storage, client_id):
    raw_dataframe = pd.read_excel(file_storage, sheet_name=0, header=None)
    month = None

    for value in raw_dataframe.iloc[:, 0].dropna():
        if " to " in str(value):
            month = detect_month_from_period(value)
            break

    header_row_index = None
    for index, row in raw_dataframe.iterrows():
        normalized_values = [normalize_column_name(value) for value in row.tolist()]
        if "date" in normalized_values and "particulars" in normalized_values:
            header_row_index = index
            break

    if header_row_index is None:
        raise ValueError("Could not find the Tally sales table header row.")

    headers = [normalize_column_name(value) for value in raw_dataframe.iloc[header_row_index]]
    data_rows = raw_dataframe.iloc[header_row_index + 1 :].copy()
    data_rows.columns = headers

    if "sale" not in data_rows.columns:
        raise ValueError("Could not find the Sale column in the Tally sales file.")

    if not month and "date" in data_rows.columns:
        first_date = pd.to_datetime(data_rows["date"].dropna().iloc[0], errors="coerce")
        if not pd.isna(first_date):
            month = first_date.strftime("%B")

    if not month:
        raise ValueError("Could not detect the month from the Tally sales file.")

    grand_total_rows = data_rows[
        data_rows.apply(
            lambda row: row.astype(str).str.contains("Grand Total", case=False, na=False).any(),
            axis=1,
        )
    ]

    voucher_rows = data_rows[pd.to_datetime(data_rows.get("date"), errors="coerce").notna()].copy()

    if not grand_total_rows.empty:
        amount = pd.to_numeric(grand_total_rows.iloc[0]["sale"], errors="coerce")
    else:
        amount = pd.to_numeric(voucher_rows["sale"], errors="coerce").sum()

    if pd.isna(amount):
        raise ValueError("Could not read the sales amount from the Tally sales file.")

    invoice_rows = [
        {column: serialize_cell(value) for column, value in row.items()}
        for row in voucher_rows.to_dict(orient="records")
    ]
    validate_unique_sales_invoices(invoice_rows, int(client_id))

    monthly_dataframe = build_sales_dataframe_from_invoice_rows(
        invoice_rows,
        client_id,
        month,
        financial_year_for_date(voucher_rows["date"].dropna().iloc[0]),
    )
    monthly_dataframe.loc[0, "amount"] = float(amount)
    return monthly_dataframe


def read_upload_file(file_storage, upload_type, client_id=None):
    if upload_type == "sales" and client_id:
        try:
            return read_tally_sales_excel(file_storage, client_id)
        except DuplicateInvoiceError:
            raise
        except ValueError:
            file_storage.stream.seek(0)

    try:
        return read_validated_excel(file_storage, upload_type, client_id=client_id)
    except ValueError as error:
        if upload_type == "sales" and client_id:
            file_storage.stream.seek(0)
            return read_tally_sales_excel(file_storage, client_id)
        raise error


def save_dataframe(dataframe, upload_type):
    schema = UPLOAD_SCHEMAS[upload_type]
    init_upload_database()

    with connect_upload_database() as connection:
        if "financial_year" not in dataframe.columns:
            dataframe = dataframe.copy()
            dataframe["financial_year"] = current_financial_year()

        if upload_type == "sales":
            client_id = int(dataframe.iloc[0]["client_id"])
            month = str(dataframe.iloc[0]["month"])
            if is_tally_period_locked(client_id, month):
                raise ValueError(f"{month} is locked for this client. Unlock the period before importing.")

        if upload_type == "sales" and dataframe.attrs.get("sales_invoice_rows"):
            validate_unique_sales_invoices(
                dataframe.attrs["sales_invoice_rows"],
                int(dataframe.iloc[0]["client_id"]),
            )

        dataframe.to_sql(schema["table"], connection, if_exists="append", index=False)

        if upload_type == "sales" and dataframe.attrs.get("sales_invoice_rows"):
            invoice_rows = [
                (
                    int(dataframe.iloc[0]["client_id"]),
                    str(dataframe.iloc[0]["month"]),
                    str(dataframe.iloc[0]["financial_year"]),
                    json.dumps(row, default=str),
                )
                for row in dataframe.attrs["sales_invoice_rows"]
            ]
            connection.executemany(
                """
                INSERT INTO sales_invoices (client_id, month, financial_year, row_data)
                VALUES (?, ?, ?, ?)
                """,
                invoice_rows,
            )


def tally_url_from_form():
    tally_url = request.form.get("tally_url", "").strip()
    if not tally_url:
        tally_url = current_app.config.get("TALLY_URL", "http://127.0.0.1:9000")
    return tally_url


@excel_upload_bp.route("/", methods=["GET", "POST"])
@admin_required
def upload_excel():
    if request.method == "POST":
        upload_type = request.form.get("upload_type", "")
        client_id = request.form.get("client_id", type=int)
        upload_file = request.files.get("excel_file")

        if upload_type not in UPLOAD_SCHEMAS:
            flash("Please choose a valid upload type.", "danger")
            return redirect(url_for("excel_upload.upload_excel"))

        if not client_id:
            flash("Please choose a client before uploading the Excel file.", "danger")
            return redirect(url_for("excel_upload.upload_excel"))

        if db_client_missing(client_id):
            flash("Please choose a valid client.", "danger")
            return redirect(url_for("excel_upload.upload_excel"))

        if not upload_file or not upload_file.filename:
            flash("Please select an Excel file to upload.", "danger")
            return redirect(url_for("excel_upload.upload_excel"))

        if not upload_file.filename.lower().endswith((".xlsx", ".xls")):
            flash("Only .xlsx and .xls Excel files are allowed.", "danger")
            return redirect(url_for("excel_upload.upload_excel"))

        try:
            dataframe = read_upload_file(upload_file, upload_type, client_id=client_id)
            save_dataframe(dataframe, upload_type)
        except ValueError as exc:
            flash(str(exc), "danger")
        except Exception as exc:
            flash(f"Upload failed: {exc}", "danger")
        else:
            label = UPLOAD_SCHEMAS[upload_type]["label"]
            flash(f"{label} upload completed successfully. {len(dataframe)} row(s) saved.", "success")

        return redirect(url_for("excel_upload.upload_excel"))

    clients = Client.query.order_by(Client.company_name).all()
    return render_template(
        "admin_uploads.html",
        clients=clients,
        upload_schemas=UPLOAD_SCHEMAS,
        default_tally_url=current_app.config.get("TALLY_URL", "http://127.0.0.1:9000"),
    )


@excel_upload_bp.route("/tally/sales/preview", methods=["POST"])
@admin_required
def preview_tally_sales():
    client_id = request.form.get("client_id", type=int)
    from_date = request.form.get("from_date", "")
    to_date = request.form.get("to_date", "")
    tally_url = tally_url_from_form()

    if not client_id:
        flash("Please choose a client before fetching from Tally.", "danger")
        return redirect(url_for("excel_upload.upload_excel"))

    client = Client.query.get(client_id)
    if client is None:
        flash("Please choose a valid client.", "danger")
        return redirect(url_for("excel_upload.upload_excel"))

    try:
        invoice_rows, month, tally_company_name = fetch_tally_sales_rows(
            tally_url,
            from_date,
            to_date,
            client_id,
        )
    except ValueError as exc:
        log_tally_sync_history(
            "fetch",
            "failed",
            client=client,
            tally_url=tally_url,
            from_date=from_date,
            to_date=to_date,
            error_count=1,
            message=str(exc),
        )
        flash(str(exc), "danger")
        return redirect(url_for("excel_upload.upload_excel"))

    validation_rows = validate_tally_invoice_rows(invoice_rows, client_id, from_date, to_date)
    financial_year = financial_year_for_date(from_date)
    company_match_error = validate_tally_company_match(client.company_name, tally_company_name)
    period_locked = is_tally_period_locked(client_id, month)
    valid_count = sum(1 for validation_row in validation_rows if validation_row["is_valid"])
    invalid_count = len(validation_rows) - valid_count
    has_errors = period_locked or company_match_error is not None or invalid_count > 0
    import_blocked = period_locked or company_match_error is not None or valid_count == 0
    error_count = sum(1 for validation_row in validation_rows if not validation_row["is_valid"])
    if company_match_error:
        error_count += 1
    if period_locked:
        error_count += 1
    log_tally_sync_history(
        "fetch",
        "validation_failed" if has_errors else "previewed",
        client=client,
        tally_url=tally_url,
        tally_company_name=tally_company_name,
        from_date=from_date,
        to_date=to_date,
        month=month,
        fetched_count=len(invoice_rows),
        error_count=error_count,
        message=company_match_error or "",
    )
    preview_rows = [
        {
            "Status": (
                "Error"
                if not validation_row["is_valid"]
                else "Existing"
                if validation_row["is_existing"]
                else "New"
            ),
            "Issues": (
                "; ".join(validation_row["errors"])
                if validation_row["errors"]
                else "Already exists; will be skipped or updated based on import mode."
                if validation_row["is_existing"]
                else ""
            ),
            "Date": validation_row["row"].get("date", ""),
            "Name": validation_row["row"].get("particulars", ""),
            "GSTIN": validation_row["row"].get("gstin_uin", ""),
            "Invoice No.": validation_row["row"].get("voucher_no.", ""),
            "Sale": validation_row["row"].get("sale", ""),
            "CGST": validation_row["row"].get("cgst", ""),
            "SGST": validation_row["row"].get("sgst", ""),
            "IGST": validation_row["row"].get("igst", ""),
            "Round Off": validation_row["row"].get("round_off", ""),
            "Invoice Value": validation_row["row"].get("gross_total", ""),
        }
        for validation_row in validation_rows
    ]

    return render_template(
        "admin_tally_preview.html",
        client=client,
        month=month,
        from_date=from_date,
        to_date=to_date,
        tally_url=tally_url,
        tally_company_name=tally_company_name,
        company_match_error=company_match_error,
        period_locked=period_locked,
        columns=[
            "Status",
            "Issues",
            "Date",
            "Name",
            "GSTIN",
            "Invoice No.",
            "Sale",
            "CGST",
            "SGST",
            "IGST",
            "Round Off",
            "Invoice Value",
        ],
        rows=preview_rows,
        has_errors=has_errors,
        import_blocked=import_blocked,
        valid_count=valid_count,
        invalid_count=invalid_count,
        invoice_rows_json=json.dumps(invoice_rows, default=str),
        financial_year=financial_year,
        import_endpoint="excel_upload.import_tally_sales",
        record_label="Sale",
        page_title="Tally sales preview",
        header_title=f"{month} sales invoices",
        save_label="Pending corrections",
    )


@excel_upload_bp.route("/tally/sales/import", methods=["POST"])
@admin_required
def import_tally_sales():
    client_id = request.form.get("client_id", type=int)
    month = request.form.get("month", "").strip()
    financial_year = request.form.get("financial_year", "").strip() or financial_year_for_date(from_date)
    from_date = request.form.get("from_date", "")
    to_date = request.form.get("to_date", "")
    tally_company_name = request.form.get("tally_company_name", "").strip()
    import_mode = request.form.get("import_mode", "new_only")
    invoice_rows_json = request.form.get("invoice_rows", "")

    if not client_id:
        flash("Please choose a client before importing Tally sales.", "danger")
        return redirect(url_for("excel_upload.upload_excel"))

    if db_client_missing(client_id):
        flash("Please choose a valid client.", "danger")
        return redirect(url_for("excel_upload.upload_excel"))

    try:
        invoice_rows = json.loads(invoice_rows_json)
        if not isinstance(invoice_rows, list) or not invoice_rows:
            raise ValueError("No Tally invoice rows were submitted for import.")

        client = Client.query.get(client_id)
        company_match_error = validate_tally_company_match(client.company_name, tally_company_name)
        if company_match_error:
            raise ValueError(company_match_error)

        if is_tally_period_locked(int(client_id), month):
            raise ValueError(f"{month} is locked for this client. Unlock the period before importing.")

        validation_rows = validate_tally_invoice_rows(invoice_rows, int(client_id), from_date, to_date)
        valid_invoice_rows = [
            validation_row["row"]
            for validation_row in validation_rows
            if validation_row["is_valid"]
        ]
        pending_count = save_tally_pending_corrections(
            client,
            month,
            from_date,
            to_date,
            tally_company_name,
            import_mode,
            validation_rows,
        )
        if not valid_invoice_rows:
            raise ValueError(
                f"No valid Tally sales rows were imported. {pending_count} row(s) were saved for correction."
            )

        result = save_tally_invoice_rows(
            int(client_id),
            month,
            valid_invoice_rows,
            import_mode,
            financial_year,
        )
    except ValueError as exc:
        log_tally_sync_history(
            "import",
            "failed",
            client=Client.query.get(client_id) if client_id else None,
            tally_company_name=tally_company_name,
            from_date=from_date,
            to_date=to_date,
            month=month,
            fetched_count=len(invoice_rows) if "invoice_rows" in locals() and isinstance(invoice_rows, list) else 0,
            error_count=pending_count if "pending_count" in locals() else 1,
            message=str(exc),
        )
        flash(str(exc), "warning" if "pending_count" in locals() and pending_count else "danger")
    except Exception as exc:
        log_tally_sync_history(
            "import",
            "failed",
            client=Client.query.get(client_id) if client_id else None,
            tally_company_name=tally_company_name,
            from_date=from_date,
            to_date=to_date,
            month=month,
            fetched_count=len(invoice_rows) if "invoice_rows" in locals() and isinstance(invoice_rows, list) else 0,
            error_count=1,
            message=str(exc),
        )
        flash(f"Tally import failed: {exc}", "danger")
    else:
        message = (
            f"Inserted {result['inserted']}, updated {result['updated']}, "
            f"skipped {result['skipped']}."
        )
        if pending_count:
            message = f"{message} Saved {pending_count} row(s) for correction."
        log_tally_sync_history(
            "import",
            "imported",
            client=Client.query.get(client_id),
            tally_company_name=tally_company_name,
            from_date=from_date,
            to_date=to_date,
            month=month,
            fetched_count=len(invoice_rows),
            imported_count=result["imported"],
            error_count=pending_count,
            message=message,
        )
        flash(f"Tally sales import completed successfully. {message}", "success")

    return redirect(url_for("excel_upload.upload_excel"))


@excel_upload_bp.route("/tally/purchase/preview", methods=["POST"])
@admin_required
def preview_tally_purchase():
    client_id = request.form.get("client_id", type=int)
    from_date = request.form.get("from_date", "")
    to_date = request.form.get("to_date", "")
    tally_url = tally_url_from_form()

    if not client_id:
        flash("Please choose a client before fetching from Tally.", "danger")
        return redirect(url_for("excel_upload.upload_excel"))

    client = Client.query.get(client_id)
    if client is None:
        flash("Please choose a valid client.", "danger")
        return redirect(url_for("excel_upload.upload_excel"))

    try:
        invoice_rows, month, tally_company_name = fetch_tally_purchase_rows(tally_url, from_date, to_date, client_id)
    except ValueError as exc:
        log_tally_sync_history("purchase_fetch", "failed", client=client, tally_url=tally_url, from_date=from_date, to_date=to_date, error_count=1, message=str(exc))
        flash(str(exc), "danger")
        return redirect(url_for("excel_upload.upload_excel"))

    validation_rows = validate_tally_purchase_rows(invoice_rows, client_id, from_date, to_date)
    financial_year = financial_year_for_date(from_date)
    company_match_error = validate_tally_company_match(client.company_name, tally_company_name)
    period_locked = is_tally_period_locked(client_id, month)
    valid_count = sum(1 for validation_row in validation_rows if validation_row["is_valid"])
    invalid_count = len(validation_rows) - valid_count
    has_errors = period_locked or company_match_error is not None or invalid_count > 0
    import_blocked = period_locked or company_match_error is not None or valid_count == 0
    error_count = sum(1 for validation_row in validation_rows if not validation_row["is_valid"])
    if company_match_error:
        error_count += 1
    if period_locked:
        error_count += 1

    log_tally_sync_history("purchase_fetch", "validation_failed" if has_errors else "previewed", client=client, tally_url=tally_url, tally_company_name=tally_company_name, from_date=from_date, to_date=to_date, month=month, fetched_count=len(invoice_rows), error_count=error_count, message=company_match_error or "")
    preview_rows = [
        {
            "Status": "Error" if not validation_row["is_valid"] else "Existing" if validation_row["is_existing"] else "New",
            "Issues": "; ".join(validation_row["errors"]) if validation_row["errors"] else "Already exists; will be skipped or updated based on import mode." if validation_row["is_existing"] else "",
            "Date": validation_row["row"].get("date", ""),
            "Name": validation_row["row"].get("particulars", ""),
            "GSTIN": validation_row["row"].get("gstin_uin", ""),
            "Invoice No.": validation_row["row"].get("voucher_no.", ""),
            "Purchase": validation_row["row"].get("purchase", ""),
            "CGST": validation_row["row"].get("cgst", ""),
            "SGST": validation_row["row"].get("sgst", ""),
            "IGST": validation_row["row"].get("igst", ""),
            "Round Off": validation_row["row"].get("round_off", ""),
            "Invoice Value": validation_row["row"].get("gross_total", ""),
        }
        for validation_row in validation_rows
    ]
    return render_template(
        "admin_tally_preview.html",
        client=client,
        month=month,
        from_date=from_date,
        to_date=to_date,
        tally_url=tally_url,
        tally_company_name=tally_company_name,
        company_match_error=company_match_error,
        period_locked=period_locked,
        columns=["Status", "Issues", "Date", "Name", "GSTIN", "Invoice No.", "Purchase", "CGST", "SGST", "IGST", "Round Off", "Invoice Value"],
        rows=preview_rows,
        has_errors=has_errors,
        import_blocked=import_blocked,
        valid_count=valid_count,
        invalid_count=invalid_count,
        invoice_rows_json=json.dumps(invoice_rows, default=str),
        financial_year=financial_year,
        import_endpoint="excel_upload.import_tally_purchase",
        record_label="Purchase",
        page_title="Tally purchase preview",
        header_title=f"{month} purchase invoices",
        save_label="Pending corrections",
    )


@excel_upload_bp.route("/tally/purchase/import", methods=["POST"])
@admin_required
def import_tally_purchase():
    client_id = request.form.get("client_id", type=int)
    month = request.form.get("month", "").strip()
    financial_year = request.form.get("financial_year", "").strip() or financial_year_for_date(from_date)
    from_date = request.form.get("from_date", "")
    to_date = request.form.get("to_date", "")
    tally_company_name = request.form.get("tally_company_name", "").strip()
    import_mode = request.form.get("import_mode", "new_only")
    invoice_rows_json = request.form.get("invoice_rows", "")
    if not client_id:
        flash("Please choose a client before importing Tally purchase.", "danger")
        return redirect(url_for("excel_upload.upload_excel"))
    if db_client_missing(client_id):
        flash("Please choose a valid client.", "danger")
        return redirect(url_for("excel_upload.upload_excel"))
    try:
        invoice_rows = json.loads(invoice_rows_json)
        if not isinstance(invoice_rows, list) or not invoice_rows:
            raise ValueError("No Tally purchase rows were submitted for import.")
        client = Client.query.get(client_id)
        company_match_error = validate_tally_company_match(client.company_name, tally_company_name)
        if company_match_error:
            raise ValueError(company_match_error)
        if is_tally_period_locked(int(client_id), month):
            raise ValueError(f"{month} is locked for this client. Unlock the period before importing.")
        validation_rows = validate_tally_purchase_rows(invoice_rows, int(client_id), from_date, to_date)
        valid_invoice_rows = [validation_row["row"] for validation_row in validation_rows if validation_row["is_valid"]]
        pending_count = save_tally_pending_corrections(client, month, from_date, to_date, tally_company_name, import_mode, validation_rows)
        if not valid_invoice_rows:
            raise ValueError(f"No valid Tally purchase rows were imported. {pending_count} row(s) were saved for correction.")
        result = save_tally_purchase_rows(
            int(client_id),
            month,
            valid_invoice_rows,
            import_mode,
            financial_year,
        )
    except ValueError as exc:
        log_tally_sync_history("purchase_import", "failed", client=Client.query.get(client_id) if client_id else None, tally_company_name=tally_company_name, from_date=from_date, to_date=to_date, month=month, fetched_count=len(invoice_rows) if "invoice_rows" in locals() and isinstance(invoice_rows, list) else 0, error_count=pending_count if "pending_count" in locals() else 1, message=str(exc))
        flash(str(exc), "warning" if "pending_count" in locals() and pending_count else "danger")
    except Exception as exc:
        log_tally_sync_history("purchase_import", "failed", client=Client.query.get(client_id) if client_id else None, tally_company_name=tally_company_name, from_date=from_date, to_date=to_date, month=month, fetched_count=len(invoice_rows) if "invoice_rows" in locals() and isinstance(invoice_rows, list) else 0, error_count=1, message=str(exc))
        flash(f"Tally purchase import failed: {exc}", "danger")
    else:
        message = f"Inserted {result['inserted']}, updated {result['updated']}, skipped {result['skipped']}."
        if pending_count:
            message = f"{message} Saved {pending_count} row(s) for correction."
        log_tally_sync_history("purchase_import", "imported", client=Client.query.get(client_id), tally_company_name=tally_company_name, from_date=from_date, to_date=to_date, month=month, fetched_count=len(invoice_rows), imported_count=result["imported"], error_count=pending_count, message=message)
        flash(f"Tally purchase import completed successfully. {message}", "success")
    return redirect(url_for("excel_upload.upload_excel"))


@excel_upload_bp.route("/tally/test", methods=["POST"])
@admin_required
def test_tally():
    tally_url = tally_url_from_form()

    try:
        tally_company_name = test_tally_connection(tally_url)
    except ValueError as exc:
        log_tally_sync_history(
            "connection_test",
            "failed",
            tally_url=tally_url,
            error_count=1,
            message=str(exc),
        )
        flash(str(exc), "danger")
    else:
        message = "Tally connection successful."
        if tally_company_name:
            message = f"{message} Detected company: {tally_company_name}."

        log_tally_sync_history(
            "connection_test",
            "success",
            tally_url=tally_url,
            tally_company_name=tally_company_name,
            message=message,
        )
        flash(message, "success")

    return redirect(url_for("excel_upload.upload_excel"))
