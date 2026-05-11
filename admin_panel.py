from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from admin_auth import admin_required, is_safe_next_url
from app import (
    Admin,
    Client,
    MONTHS,
    db,
    delete_sales_invoice_rows,
    get_sales_invoice_details,
)
from excel_upload import (
    connect_upload_database,
    fetch_tally_pending_corrections,
    init_upload_database,
    is_tally_period_locked,
    set_tally_period_lock,
)
from upload_config import UPLOAD_SCHEMAS


admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def delete_uploaded_client_records(client_id):
    init_upload_database()
    with connect_upload_database() as connection:
        for schema in UPLOAD_SCHEMAS.values():
            connection.execute(
                f"DELETE FROM {schema['table']} WHERE client_id = ?",
                (client_id,),
            )


def fetch_uploaded_records(upload_type, client_id=None, limit=200):
    schema = UPLOAD_SCHEMAS[upload_type]
    init_upload_database()

    columns = ["id", *schema["columns"]]
    sql = f"SELECT {', '.join(columns)} FROM {schema['table']}"
    params = []

    if client_id:
        sql += " WHERE client_id = ?"
        params.append(client_id)

    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    with connect_upload_database() as connection:
        rows = connection.execute(sql, params).fetchall()

    return [dict(zip(columns, row)) for row in rows]


def fetch_tally_sync_history(limit=200):
    init_upload_database()
    columns = [
        "id",
        "created_at",
        "admin_name",
        "client_name",
        "action",
        "status",
        "tally_url",
        "tally_company_name",
        "from_date",
        "to_date",
        "month",
        "fetched_count",
        "imported_count",
        "error_count",
        "message",
    ]
    with connect_upload_database() as connection:
        rows = connection.execute(
            f"""
            SELECT {", ".join(columns)}
            FROM tally_sync_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [dict(zip(columns, row)) for row in rows]


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("admin_id"):
        return redirect(url_for("admin.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        admin = Admin.query.filter_by(email=email).first()

        if admin and admin.check_password(password):
            session["admin_id"] = admin.id
            session["admin_name"] = admin.name
            next_page = request.args.get("next")
            if is_safe_next_url(next_page):
                return redirect(next_page)
            return redirect(url_for("admin.dashboard"))

        flash("Invalid admin email or password.", "danger")

    return render_template("admin_login.html")


@admin_bp.route("/")
@admin_required
def dashboard():
    clients_count = Client.query.count()
    records_count = {}

    init_upload_database()
    with connect_upload_database() as connection:
        for upload_type, schema in UPLOAD_SCHEMAS.items():
            total = connection.execute(f"SELECT COUNT(*) FROM {schema['table']}").fetchone()[0]
            records_count[upload_type] = total

    return render_template(
        "admin_dashboard.html",
        clients_count=clients_count,
        upload_schemas=UPLOAD_SCHEMAS,
        records_count=records_count,
    )


@admin_bp.route("/clients")
@admin_required
def clients():
    client_list = Client.query.order_by(Client.company_name).all()
    return render_template("admin_clients.html", clients=client_list)


@admin_bp.route("/clients/new", methods=["GET", "POST"])
@admin_required
def create_client():
    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        sharepoint_folder_path = request.form.get("sharepoint_folder_path", "").strip()

        if not company_name or not email or not password:
            flash("Company name, email, and password are required.", "danger")
            return render_template("admin_client_form.html", client=None)

        if Client.query.filter_by(email=email).first():
            flash("A client with this email already exists.", "danger")
            return render_template("admin_client_form.html", client=None)

        client = Client(
            company_name=company_name,
            email=email,
            sharepoint_folder_path=sharepoint_folder_path or None,
        )
        client.set_password(password)
        db.session.add(client)
        db.session.commit()
        flash("Client account created successfully.", "success")
        return redirect(url_for("admin.clients"))

    return render_template("admin_client_form.html", client=None)


@admin_bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_client(client_id):
    client = db.session.get(Client, client_id)
    if client is None:
        flash("Client not found.", "danger")
        return redirect(url_for("admin.clients"))

    if request.method == "POST":
        company_name = request.form.get("company_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        sharepoint_folder_path = request.form.get("sharepoint_folder_path", "").strip()

        if not company_name or not email:
            flash("Company name and email are required.", "danger")
            return render_template("admin_client_form.html", client=client)

        existing_client = Client.query.filter(Client.email == email, Client.id != client.id).first()
        if existing_client:
            flash("Another client already uses this email.", "danger")
            return render_template("admin_client_form.html", client=client)

        client.company_name = company_name
        client.email = email
        client.sharepoint_folder_path = sharepoint_folder_path or None
        if password:
            client.set_password(password)

        db.session.commit()
        flash("Client account updated successfully.", "success")
        return redirect(url_for("admin.clients"))

    return render_template("admin_client_form.html", client=client)


@admin_bp.route("/clients/<int:client_id>/delete", methods=["POST"])
@admin_required
def delete_client(client_id):
    client = db.session.get(Client, client_id)
    if client is None:
        flash("Client not found.", "danger")
        return redirect(url_for("admin.clients"))

    delete_uploaded_client_records(client.id)
    db.session.delete(client)
    db.session.commit()
    flash("Client account and uploaded records deleted.", "success")
    return redirect(url_for("admin.clients"))


@admin_bp.route("/records")
@admin_required
def records():
    upload_type = request.args.get("type", "sales")
    client_id = request.args.get("client_id", type=int)

    if upload_type not in UPLOAD_SCHEMAS:
        upload_type = "sales"

    clients = Client.query.order_by(Client.company_name).all()
    records_list = fetch_uploaded_records(upload_type, client_id=client_id)

    return render_template(
        "admin_records.html",
        clients=clients,
        selected_client_id=client_id,
        selected_upload_type=upload_type,
        upload_schemas=UPLOAD_SCHEMAS,
        records=records_list,
        columns=["id", *UPLOAD_SCHEMAS[upload_type]["columns"]],
    )


@admin_bp.route("/tally-sync-history")
@admin_required
def tally_sync_history():
    return render_template(
        "admin_tally_sync_history.html",
        history=fetch_tally_sync_history(),
    )


@admin_bp.route("/tally-pending-corrections")
@admin_required
def tally_pending_corrections():
    return render_template(
        "admin_tally_pending_corrections.html",
        corrections=fetch_tally_pending_corrections(),
    )


@admin_bp.route("/clients/<int:client_id>/sales")
@admin_required
def sales_details(client_id):
    client = db.session.get(Client, client_id)
    if client is None:
        flash("Client not found.", "danger")
        return redirect(url_for("admin.records", type="sales"))

    selected_month = request.args.get("month", "April")
    if selected_month not in MONTHS:
        selected_month = "April"

    records, columns = get_sales_invoice_details(client.id, selected_month)
    return render_template(
        "sales_details.html",
        admin_view=True,
        client=client,
        period_locked=is_tally_period_locked(client.id, selected_month),
        months=MONTHS,
        selected_month=selected_month,
        records=records,
        columns=columns,
    )


@admin_bp.route("/clients/<int:client_id>/sales/<int:invoice_id>/delete", methods=["POST"])
@admin_required
def delete_sales_invoice(client_id, invoice_id):
    selected_month = request.form.get("month", "April")
    if selected_month not in MONTHS:
        selected_month = "April"

    client = db.session.get(Client, client_id)
    if client is None:
        flash("Client not found.", "danger")
        return redirect(url_for("admin.records", type="sales"))

    if is_tally_period_locked(client.id, selected_month):
        flash(f"{selected_month} is locked. Unlock the period before deleting invoices.", "warning")
        return redirect(url_for("admin.sales_details", client_id=client.id, month=selected_month))

    deleted_count = delete_sales_invoice_rows([invoice_id], client.id, selected_month)
    if deleted_count:
        flash("Sales invoice row deleted.", "success")
    else:
        flash("Sales invoice row was not found.", "warning")

    return redirect(url_for("admin.sales_details", client_id=client.id, month=selected_month))


@admin_bp.route("/clients/<int:client_id>/sales/bulk-delete", methods=["POST"])
@admin_required
def bulk_delete_sales_invoices(client_id):
    selected_month = request.form.get("month", "April")
    if selected_month not in MONTHS:
        selected_month = "April"

    client = db.session.get(Client, client_id)
    if client is None:
        flash("Client not found.", "danger")
        return redirect(url_for("admin.records", type="sales"))

    if is_tally_period_locked(client.id, selected_month):
        flash(f"{selected_month} is locked. Unlock the period before deleting invoices.", "warning")
        return redirect(url_for("admin.sales_details", client_id=client.id, month=selected_month))

    invoice_ids = request.form.getlist("invoice_ids")
    deleted_count = delete_sales_invoice_rows(invoice_ids, client.id, selected_month)

    if deleted_count:
        flash(f"{deleted_count} sales invoice row(s) deleted.", "success")
    else:
        flash("Please select at least one invoice row to delete.", "warning")

    return redirect(url_for("admin.sales_details", client_id=client.id, month=selected_month))


@admin_bp.route("/clients/<int:client_id>/sales/lock", methods=["POST"])
@admin_required
def lock_sales_period(client_id):
    selected_month = request.form.get("month", "April")
    if selected_month not in MONTHS:
        selected_month = "April"

    client = db.session.get(Client, client_id)
    if client is None:
        flash("Client not found.", "danger")
        return redirect(url_for("admin.records", type="sales"))

    set_tally_period_lock(client.id, selected_month, True, session.get("admin_name", ""))
    flash(f"{selected_month} sales period locked.", "success")
    return redirect(url_for("admin.sales_details", client_id=client.id, month=selected_month))


@admin_bp.route("/clients/<int:client_id>/sales/unlock", methods=["POST"])
@admin_required
def unlock_sales_period(client_id):
    selected_month = request.form.get("month", "April")
    if selected_month not in MONTHS:
        selected_month = "April"

    client = db.session.get(Client, client_id)
    if client is None:
        flash("Client not found.", "danger")
        return redirect(url_for("admin.records", type="sales"))

    set_tally_period_lock(client.id, selected_month, False)
    flash(f"{selected_month} sales period unlocked.", "success")
    return redirect(url_for("admin.sales_details", client_id=client.id, month=selected_month))
