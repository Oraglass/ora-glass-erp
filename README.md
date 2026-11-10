# Ora Glass ERP

Flask app for quoting insulated glass units. Quotes, customers, users, and tier markups are JSON files. There is no database.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export APP_ENV=development
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export ADMIN_EMAIL="you@oraglass.us"
export ADMIN_PASSWORD="choose a long password 1"
flask --app app run
```

Unset `ADMIN_PASSWORD` after the first successful sign-in. The password is only used to create the first administrator, and it is not kept in the repo.

`APP_ENV=development` (or running `python app.py`) allows an ephemeral `SECRET_KEY` when you do not set one. Sessions then reset on every restart. Production refuses to boot without a `SECRET_KEY` of at least 32 characters.

```bash
pytest
```

## Where data is stored

| Variable | Production value | Used for |
| --- | --- | --- |
| `DATA_DIR` | `/data` | Default directory for the JSON files |
| `QUOTES_PATH` | `/data/quotes.json` | Quote history |
| `CUSTOMERS_PATH` | `/data/customers.json` | Customer companies |
| `USERS_PATH` | `/data/users.json` | Login accounts |
| `TIERS_PATH` | `/data/tiers.json` | Tier names and markups |
| `PRICING_PATH` | unset | Optional override for `pricing_base.xlsx` |

If `DATA_DIR` is unset, it is the directory that contains `QUOTES_PATH`. On Render that means a service that only sets `QUOTES_PATH=/data/quotes.json` still stores customers, users, and tiers on the `/data` disk.

If neither variable is set, files go to `./data/` next to the app. A legacy `quotes.json` in the app directory is copied to the new path the first time it is read, and later writes go only to the new path.

Writes create the parent directory and replace the file atomically (temp file in the same directory, then `os.replace`).

`pricing_base.xlsx` is loaded from, in order: `PRICING_PATH`, the file next to `app.py`, `DATA_DIR/pricing_base.xlsx`, then `data/pricing_base.xlsx`. The workbook ships in the repo root, so it does not need to be copied onto the disk. Rows with a zero price are skipped, which currently leaves Technoform spacers out of the list because those rows are $0 in the workbook. Temper add-on prices still come from the code table (`4mm` $0.50, `5mm` $0.55, `6mm` $0.65, `8mm` $0.75, `10mm` $0.85, `12mm` $0.95). The workbook has different temper rows; confirm those before switching the calculator over to them.

## Accounts

Sign-in is by email. Passwords are salted with `werkzeug.security` (scrypt). The old demo accounts are gone.

Session cookies are `HttpOnly` and `SameSite=Lax`. In production they are also `Secure`. State-changing forms and API calls require a CSRF token. Login allows 5 failures per email and 30 per IP in 15 minutes, tracked in memory, so the process is started with one gunicorn worker.

Only an administrator can create or edit customers, users, tier assignments, and tier markups. Sales can view customers. A customer user can quote and can see quote history, orders, invoices, and the financial page, limited to their own company. They cannot pick a price tier. The calculator uses their user override if one is set, otherwise the company tier.

### First administrator

Either set `ADMIN_EMAIL` and `ADMIN_PASSWORD` before the first request (the app creates the admin only when no admin password exists yet), or run:

```bash
export ADMIN_PASSWORD="choose a long password 1"
flask --app app create-admin --email you@oraglass.us
```

The password is read from `ADMIN_PASSWORD` or from a prompt. It is not accepted as a committed value, and weak passwords are refused. Remove `ADMIN_PASSWORD` from the environment after you have signed in.

### Open Windows seed

On startup (`AUTO_SEED` defaults to `1`) the app creates, if missing:

- Customer **Open Windows** (`open-windows.ca`), company tier **End User**
- `alex@oraglass.us` as a customer user with a **T2** override
- `kiu@open-windows.ca`, `vlad@open-windows.ca`, and `support@open-windows.ca` on the company tier (End User)

No password is stored for them. To activate someone:

1. Sign in as an administrator.
2. Open **Users**.
3. Click **Setup link** and send that URL to the person. It works once and expires in 7 days. The raw token is not saved.
4. Or click **Temp password**, tell them the password, and they will be forced to change it at first sign-in.

The same links can be printed from the shell. This replaces any unused link for users who still have no password:

```bash
flask --app app seed --links --base-url https://your-public-host
```

Display names are taken from the email (Alex, Kiu, Vlad, Open Windows Support) because no legal names were provided.

## Tier markups

T1–T5 keep their previous markups. **End User** (`EU`) is a new tier. Its starting markup is a **placeholder of 100% on glass and 100% on spacers** (the customer pays 2× base). The Pricing page is admin-only and saves markups to `tiers.json`. Saving clears the placeholder flag.

The previous quote screen labeled T5 as "End User". T5 is still Tier 5 at 425% glass and 425% spacer. It is not the new End User tier. Set the real End User percentages on the Pricing page.

## Render

`render.yaml` sets `APP_ENV=production`, the `/data` paths, `AUTO_SEED=1`, and a generated `SECRET_KEY`. Set `ADMIN_EMAIL` and `ADMIN_PASSWORD` in the dashboard for the first boot, deploy, sign in, then delete `ADMIN_PASSWORD`.

The blueprint also declares a 1 GB disk at `/data`. If the service already has that disk, remove the `disk` block from `render.yaml` before syncing so Render does not try to attach a second one.

Optional `BASE_URL` (for example `https://ora-glass-erp.onrender.com`) is used when an admin generates a setup link. If it is empty, the link uses the request host.

Quote history that was previously written to `quotes.json` in the app directory was not on the persistent disk, because `QUOTES_PATH` was ignored. If a copy of that file still exists, place it at `/data/quotes.json`. New reads and writes use `QUOTES_PATH`.
