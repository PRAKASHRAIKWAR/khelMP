# KHELMP

**Discover. Develop. Select. Succeed.**

KhelMP currently combines a Django web application for player profiles, community posts, selector-reviewed results and Gemini guidance with a versioned FastAPI service for role-aware APIs, authentication, player search and private media uploads.

## Start the Existing Website

Copy `.env.example` to `.env`, replace the secret placeholders, then run the Django app:

```powershell
Set-Location .\khelmp_frontend_django
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py check
python manage.py createsuperuser
python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/`.

## Deploy the Django Frontend to Render

Create a new Blueprint in Render using this repository and its root `render.yaml`. It provisions a free Django web service and a free PostgreSQL database; the web service builds static assets and applies Django migrations during deployment. The web service may spin down while idle. Render's free PostgreSQL database expires 30 days after creation and its data is eventually deleted, so this is only for a temporary demo. Do not use it for important user data.

Configure `GEMINI_API_KEY`, Twilio credentials, and email settings in the Render dashboard if those features are needed. Email delivery uses `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` (use a Gmail App Password, not your Google account password), and optionally `DEFAULT_FROM_EMAIL`; the default SMTP host is Gmail. Do not copy real credentials into `.env.example` or commit them. Existing local SQLite data and uploaded media are not migrated by this setup; configure persistent object storage before relying on uploads.

### Make the public site discoverable on Google

The public homepage includes search metadata, and `/robots.txt` links to `/sitemap.xml`. After deployment, add `https://khelmp-frontend.onrender.com` as a URL-prefix property in Google Search Console and submit `https://khelmp-frontend.onrender.com/sitemap.xml`. If Search Console requests HTML-tag verification, put only the tag's `content` value in a Render environment variable named `GOOGLE_SITE_VERIFICATION`, then redeploy. Google decides when and whether to index a site; submitting a sitemap does not guarantee or instantly trigger search results. A custom domain you own is preferable for a long-term public launch.

The staff-only account dashboard is available at `/admin/users/` to Django superusers. It can create player and selector accounts after verifying the supplied mobile OTP, and can activate or suspend non-staff accounts. The public Admin button leads to Django admin sign-in, which accepts a staff account's username or unique email address; there is no public administrator signup. For a Render deployment without Shell access, set a strong, temporary `ADMIN_BOOTSTRAP_TOKEN` of at least 32 characters in the service's Environment settings, deploy, then open `/admin/initial-setup/` over HTTPS. Enter the token, administrator email, username and a new password directly in the browser; never put the token or password in source control or chat. The page is disabled after the first superuser is created. Remove the token from Render immediately afterward, then sign in through `/admin/` with the administrator email and new password. For deployments with secure shell access, `python manage.py createsuperuser` remains available.

## Start the API and PostgreSQL

Docker Compose runs PostgreSQL and the FastAPI service. Set `POSTGRES_PASSWORD`, `JWT_SECRET_KEY`, and `DJANGO_SECRET_KEY` in `.env` before starting:

```powershell
docker compose up --build
```

The API is at `http://127.0.0.1:8001`; interactive API documentation is at `http://127.0.0.1:8001/docs`. For local development without Docker, install `khelmp_backend_fastapi/requirements.txt`, then from that folder run:

```powershell
python -m alembic upgrade head
uvicorn main:app --host 127.0.0.1 --port 8001
```

After applying migrations, create the first FastAPI administrator from the backend folder with `python -m app.admin_cli`; credentials are entered interactively and are never accepted through public signup.

The API reads `DATABASE_URL` when provided and otherwise uses its own ignored SQLite development database. Gemini is accessed only by the backend. Uploaded bytes go to a local storage adapter and only metadata is stored in SQL; the adapter can be replaced by an object-storage implementation.

## Current Feature Comparison

| Requested area | Current implementation |
| --- | --- |
| Player and selector workflows | Django supports profiles, posts, selector applications, protected records, results and player connections. |
| Coach, academy and organizer roles | FastAPI signup schemas and account roles exist; complete role-specific dashboards and workflows are not yet built. |
| Authentication and authorization | Django uses session authentication; FastAPI now has JWT and API-side RBAC. These are currently separate identity stores and must be unified before production. |
| Database | Django still uses SQLite. FastAPI supports SQLAlchemy, Alembic and PostgreSQL through Compose; existing Django data has not been migrated. |
| Teams, tournaments, grounds and matches | Not implemented yet. |
| File storage | Django uses local media; the FastAPI service has a pluggable local adapter and stores file metadata, not file contents, in its database. |
| AI | Django's Gemini features remain available; the FastAPI sports Q&A endpoint is authenticated and calls Gemini server-side. AI output is not an official selection or verification. |
| Admin | Django superuser/admin flows exist. FastAPI public signup rejects `ADMIN`, and its verification route requires an API admin account. |

The current website and new API are useful working foundations, but they are not yet one integrated application: the Django templates do not yet use the FastAPI JWT identity, the Django database is not on PostgreSQL, and several requested sports-management modules remain to be built. Avoid deploying the two account stores as if they were already synchronized.

## Existing Django Workflows

- Anonymous visitors land on the sign-in screen; account creation and password recovery remain available from its links. Feed, profiles, player search, chat, records and Gemini questions require sign-in.
- New player and selector accounts verify their phone by OTP; existing accounts sign in with their password and a one-time image code. The image code is an anti-automation check, not proof of phone or email ownership. OTP sign-in is disabled.
- For local DEBUG mode without Twilio, the signup OTP is shown on the form for testing. Never enable this outside development. Production SMS requires `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` in the deployment environment; signup OTPs are hashed in the database, expire after five minutes, and allow at most five guesses.
- Feed posts can be shared through the device's share sheet or by copying a post link; only the post's author can delete it, including from the admin interface.
- The feed runs probabilistic Gemini media screening and labels uncertainty; cricket run/ball captions become unverified claims for selector review and are not written to the official result ledger until approved.
- Players can edit profiles, publish photos/videos, like/comment, connect with teammates and view results.
- Accepted friends can exchange private chat messages; unread counts and 3-second thread polling keep conversations current. Chat endpoints reject users who are not accepted friends.
- Selectors apply for access; a Django superuser reviews applications and issues single-use, expiring invitation codes.
- Run `python manage.py check_gemini` from `khelmp_frontend_django` to test a real Gemini request and see which model responds without exposing the API key.
- Selector-only records and private athlete documents are protected by Django views.
- Django tests: `python manage.py test` from `khelmp_frontend_django`.
- FastAPI tests: `python -m unittest discover -s tests -v` from `khelmp_backend_fastapi`.

## Security Notes

- Keep `.env` private, use distinct randomly generated `DJANGO_SECRET_KEY` and `JWT_SECRET_KEY` values, and rotate credentials exposed in logs, screenshots or conversations.
- Restrict `CORS_ORIGINS`, use HTTPS in production, and set exact Django allowed hosts.
- Before production, unify authentication and data ownership, add account email verification, configure real Twilio SMS credentials, private cloud storage, backups, shared rate limiting/cache, monitoring and a migration plan for existing Django data.
