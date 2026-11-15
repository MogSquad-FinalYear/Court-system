# Render deployment and frontend authentication handoff

## Render backend configuration

Add these environment variables to the Render web service:

```env
DATABASE_URL=<Neon PostgreSQL connection string>
CLOUDINARY_URL=<Cloudinary connection string>
JWT_PRIVATE_KEY=<private RSA key>
JWT_PUBLIC_KEY=<matching public RSA key>
JWT_ISSUER=veritas-court
JWT_AUDIENCE=veritas-court-api
JWT_ACCESS_TTL_MINUTES=10
JWT_REFRESH_TTL_DAYS=30
CORS_ORIGINS=https://yournyaya.vercel.app
AUTH_COOKIE_SECURE=true
AUTH_COOKIE_SAMESITE=none
AUTH_ALLOW_REGISTRATION=false
AUTH_REGISTRATION_JURISDICTION=KA-BLR
```

PostgreSQL stores relational data and Cloudinary stores document blobs, so a
Render persistent disk is not required. Keep `DATA_DIR` and `DB_PATH` only if
you want a local cache or local development fallback.

The backend rejects wildcard CORS origins. `CORS_ORIGINS` must contain the
exact frontend origin, including `https://` and excluding any trailing path.

The Render start command is:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

To migrate the existing local database and blobs once before deploying, run:

```bash
python migrate_sqlite_to_postgres.py
```

## Frontend integration prompt

```text
Update the React/Vite frontend to use the deployed Court API authentication
flow. Inspect the existing API client, auth provider, route guards, login
screen, and all Court API calls before editing.

Use the deployed Court API base URL from the existing Vite environment
configuration. Never hard-code it.

Authentication:

1. POST /api/auth/login with:
   { "user_id": "<user id>", "password": "<password>" }

2. Always use credentials: "include" for login, refresh, logout, and all
   Court API requests.

3. Store the returned access_token only in memory. Do not store it in
   localStorage, sessionStorage, IndexedDB, cookies, URLs, or logs.

4. The backend stores the refresh token in an HttpOnly cookie. JavaScript must
   never read or create that cookie.

5. Add a single shared request client that sends:
   Authorization: Bearer <access_token>
   credentials: include

6. On one 401 response, call POST /api/auth/refresh with credentials included,
   replace the in-memory access token, and retry the original request once.
   Coordinate concurrent 401 responses through one shared refresh promise.

7. If refresh returns 401 or fails, clear the user and access token and route
   to login. Do not show registration as a substitute for session refresh.

8. Call GET /api/auth/me after successful login and after restoring a session.
   The authenticated user response is:
   {
     "id": "...",
     "display_name": "...",
     "roles": ["..."],
     "permissions": ["..."],
     "jurisdictions": ["..."],
     "active": true
   }

9. Logout with POST /api/auth/logout and credentials included, then clear all
   in-memory auth state.

10. Remove the demo principal provider, role selector, jurisdiction selector,
    localStorage key veritas.court.principal.v1, and all X-User-Id, X-Role, and
    X-Jurisdiction headers.

11. Never let the user choose a role or jurisdiction. Display the values from
    /api/auth/me as read-only data.

12. Use backend permissions only for UI convenience:
    documents:upload
    documents:sign
    documents:audit
    filing:submit
    scheduling:write
    resources:manage
    integrations:calls
    The backend remains authoritative.

13. Handle statuses as follows:
    401: refresh once, then logout and redirect to login.
    403: display access denied and do not retry.
    409: preserve and display existing conflict details.
    429: display a retry-later message.

14. Add tests for login, invalid credentials, refresh after browser reload,
    refresh failure, logout, no legacy identity headers, Authorization header
    injection, permission visibility, 403 behavior, and 409 preservation.

The deployed backend uses a cross-site refresh cookie:
SameSite=None; Secure=true.
The frontend must use credentials: include and must be served over HTTPS.
Return the changed files and exact test commands.
```
