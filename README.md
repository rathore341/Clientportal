# Client Portal

Flask-based client portal for admin-managed client records, sales and purchase invoice imports, Tally sync previews, KPI dashboards, sales analysis, and SharePoint document redirection.

## Features

- Admin login and client management
- Client-only dashboard with financial year and month filters
- Sales and purchase invoice views with Excel/PDF export
- Tally sales and purchase sync with validation, dry-run preview, sync history, and locked periods
- Duplicate invoice validation
- Pending correction storage for invalid Tally rows
- SharePoint redirect-based document access, so documents remain in SharePoint

## Local Setup

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Update `.env` with a strong `SECRET_KEY`.

Initialize the app databases:

```powershell
flask --app app init-db
flask --app app init-upload-db
flask --app app create-admin
```

Run the portal:

```powershell
python app.py
```

Open:

- Admin portal: `http://127.0.0.1:5000/admin/login`
- Client portal: `http://127.0.0.1:5000/login`

## Configuration

Environment variables:

- `SECRET_KEY`: Flask session secret. Required for production.
- `DATABASE_URL`: Main client/admin database URL. Defaults to local SQLite.
- `UPLOAD_SQLITE_DB`: Upload database path. Defaults to `uploads.sqlite3`.

Tally URL is entered from the admin upload screen. Default local Tally port is `http://127.0.0.1:9000`.

For SharePoint documents, paste the client's SharePoint folder sharing link in the client record. The portal redirects the client to SharePoint, and Microsoft handles document authentication.

## GitHub Safety

This repo intentionally ignores:

- SQLite databases
- Virtual environment files
- Logs and cache folders
- Excel/CSV uploads and exports
- `.env` secrets

Do not commit real client documents, database files, exported invoices, or secret keys.

## Upload To GitHub

```powershell
git init
git add .
git commit -m "Initial client portal"
git branch -M main
git remote add origin https://github.com/YOUR-USER/YOUR-REPO.git
git push -u origin main
```

Create the GitHub repository first, then replace `YOUR-USER/YOUR-REPO` with your repository path.
