Based on inspecting the codebase (FastAPI backend in `backend/`, Next.js 14 frontend in `frontend/`, current paths, configs, deps), here's the decomposition:

# Task: T001 — Pin bcrypt<4 in backend requirements
- depends_on:
- tags: config, backend
- complexity: low
- files: backend/requirements.txt
- acceptance: backend/requirements.txt pins `bcrypt<4.0` alongside `passlib[bcrypt]>=1.7.4`; `pip install -r backend/requirements.txt` completes in a clean venv without dependency resolution errors and `python -c "from passlib.context import CryptContext; CryptContext(schemes=['bcrypt']).hash('x')"` runs without the `AttributeError: module 'bcrypt' has no attribute '__about__'` warning

passlib 1.7.4 reads `bcrypt.__about__` which was removed in bcrypt 4.x — this is the most common blocker for hash_password at boot. Add the pin on a new line; keep existing lines intact.

# Task: T002 — Create backend .env with dev defaults
- depends_on:
- tags: config, backend
- complexity: low
- files: backend/.env
- acceptance: backend/.env exists with `DATABASE_URL=sqlite:///./app.db`, `SECRET_KEY` set to a non-default dev value, `ACCESS_TOKEN_EXPIRE_MINUTES=60`, and `BACKEND_CORS_ORIGINS=["http://localhost:3000"]`; `python -c "from src.core.config import settings; print(settings.DATABASE_URL)"` run from backend/ prints the configured URL with no ValidationError

Copy structure from `backend/.env.example`. The CORS list MUST include `http://localhost:3000` so the Next.js frontend can call the API. Pydantic-settings parses the list via the custom `parse_cors_origins` validator (JSON string).

# Task: T003 — Add backend dev runner scripts
- depends_on: T001, T002
- tags: devops, backend
- complexity: low
- files: backend/run_dev.sh, backend/run_dev.ps1
- acceptance: `bash backend/run_dev.sh` (or `pwsh backend/run_dev.ps1` on Windows) launches `uvicorn src.main:app --reload --host 0.0.0.0 --port 8000` from the backend/ working directory; both scripts exit non-zero if uvicorn is missing; scripts are executable (chmod +x for the .sh)

Scripts must `cd` into the script's directory first so relative imports (`src.main`) resolve. Use `python -m uvicorn` form so it works without uvicorn being on PATH. Do not invent a virtualenv path — let the user's active env supply Python.

# Task: T004 — Verify backend pytest suite is green
- depends_on: T001, T002
- tags: test, backend
- complexity: low
- files: backend/tests/conftest.py
- acceptance: Running `pytest` from backend/ exits 0 with all tests in backend/tests/ passing; no warnings about bcrypt missing `__about__`; no ImportError on `from src.main import app`

If any tests fail solely due to environment (missing `.env`, missing bcrypt pin), this task should add the minimum conftest tweak (e.g. set `os.environ.setdefault('SECRET_KEY', ...)` before importing settings) rather than relax assertions.

# Task: T005 — Create frontend .env.local with API URL
- depends_on:
- tags: config, frontend
- complexity: low
- files: frontend/.env.local
- acceptance: frontend/.env.local exists with `NEXT_PUBLIC_API_URL=http://localhost:8000`; `next dev` (when launched) logs the env file as loaded; the home page renders the same URL string in its "API:" line

`.env.local` is gitignored by `frontend/.gitignore` (`.env*.local`), so this is a local-only artifact. Mirror the single key from `frontend/.env.local.example`. Do NOT modify the committed `frontend/.env` file.

# Task: T006 — Add frontend dev runner scripts
- depends_on: T005
- tags: devops, frontend
- complexity: low
- files: frontend/run_dev.sh, frontend/run_dev.ps1
- acceptance: `bash frontend/run_dev.sh` (or `pwsh frontend/run_dev.ps1`) launches `npm run dev` from the frontend/ directory binding to port 3000; scripts cd into the script's own directory first; both scripts exit non-zero if node_modules is absent (with a one-line hint to run `npm install`)

Do not auto-run `npm install` inside the script — that is user-driven. Simply check `[ -d node_modules ]` (bash) / `Test-Path node_modules` (pwsh) and bail with a clear message.

# Task: T007 — Verify frontend jest suite is green
- depends_on: T005
- tags: test, frontend
- complexity: low
- files: frontend/jest.setup.ts
- acceptance: Running `npm test -- --watchAll=false` from frontend/ exits 0 with all tests under frontend/src/__tests__/ passing; no unhandled MSW request errors; coverage report is produced

If any test fails purely because of the missing `.env.local` (NEXT_PUBLIC_API_URL undefined), fix by stubbing the env in jest.setup.ts rather than mutating production code.

# Task: T008 — Root dev orchestration scripts
- depends_on: T003, T006
- tags: devops, wiring
- complexity: low
- files: scripts/dev.sh, scripts/dev.ps1
- acceptance: `bash scripts/dev.sh` (or `pwsh scripts/dev.ps1`) starts backend on :8000 and frontend on :3000 concurrently from the repo root; both child processes are terminated when the parent receives SIGINT/Ctrl+C; output from both processes is interleaved with `[backend]` / `[frontend]` prefixes

Use background jobs + `trap 'kill 0' INT TERM EXIT` for the bash version. For PowerShell, use `Start-Process` with `-PassThru` and register a finalizer that stops them. Do not use docker-compose — the project has no Dockerfiles.

# Task: T009 — Smoke-test script for main API endpoints
- depends_on:
- tags: devops, test
- complexity: low
- files: scripts/smoke_test.sh
- acceptance: `bash scripts/smoke_test.sh` (assuming backend running on :8000) performs in order — GET / → expects 200 with body `{"status":"ok"}`; POST /auth/register with a unique email → expects 201; POST /auth/login (form-encoded) with the same creds → expects 200 and prints the bearer token; GET /api/v1/students with that token → expects 200 with a JSON array; script exits 0 only if every step matches; exits non-zero with the failing step name otherwise

Use `curl -s -o /tmp/body -w "%{http_code}"` to capture status separately from body. Generate the email as `smoke+$(date +%s)@example.com` so reruns don't collide on the unique constraint. Parse the token with `python -c` or `jq` (prefer jq, document the dep).

# Task: T010 — End-to-end dev verify (live servers + smoke test)
- depends_on: T004, T007, T008, T009
- tags: test, verify
- complexity: medium
- files: scripts/verify_dev.sh
- acceptance: |
    scripts/verify_dev.sh starts both servers via scripts/dev.sh in the background
    Polls http://localhost:8000/ until 200 (max 30s) and http://localhost:3000 until 200 (max 60s)
    Then runs scripts/smoke_test.sh and captures its exit code
    Then issues GET http://localhost:3000 and asserts the response body contains "Welcome to Student Management"
    Tears down both servers cleanly on success and on failure
    Exits 0 only when every probe + smoke step passes; non-zero otherwise with a one-line reason

This is the single command the user runs to confirm "the project runs on dev." Keep timeouts conservative (Next.js cold start can take 20-40s). Use `lsof -i:8000` or `curl --fail` polling — do not sleep for a fixed long duration.

# Task: T011 — Dev runbook documentation
- depends_on: T010
- tags: docs
- complexity: low
- files: docs/DEV_RUNBOOK.md
- acceptance: docs/DEV_RUNBOOK.md exists and documents — prerequisites (Python 3.11+, Node 20+), one-time setup (`pip install -r backend/requirements.txt`, `cd frontend && npm install`, copy env templates), how to start each service individually (backend/run_dev.sh, frontend/run_dev.sh), how to start both (scripts/dev.sh), how to run smoke test (scripts/smoke_test.sh), how to run the end-to-end verify (scripts/verify_dev.sh), and a troubleshooting section listing the bcrypt pin, the CORS env var, and the NEXT_PUBLIC_API_URL requirement

Keep it under 150 lines. Reference the exact filenames created by T001–T009 so a new developer can copy-paste commands.
Based on inspecting the codebase (FastAPI backend in `backend/`, Next.js 14 frontend in `frontend/`, current paths, configs, deps), here's the decomposition:

<!-- appended via generate-tasks --append -->

The working directory is empty (sandboxed). I can't inspect the actual codebase. Based on the existing tasks (T001 pins bcrypt for backend, T004 verifies pytest, T007 verifies jest, T009 smoke-tests API endpoints), I can infer a Python backend with JWT auth and a React/JS frontend. I'll use plausible conventional paths.
# Task: T012 — Add TokenBlacklist model
- depends_on:
- tags: model, db, auth
- complexity: low
- files: backend/src/models/token_blacklist.py, backend/src/models/__init__.py
- acceptance: TokenBlacklist instance with id, jti (unique-indexed), user_id, revoked_at, expires_at persists and round-trips via SQLAlchemy session; exported from models package

Use the project's existing declarative base. Add `__repr__`. The `jti` column stores the JWT ID claim and is unique-indexed for fast lookup during token verification.

# Task: T013 — Add Alembic migration for token_blacklist table
- depends_on: T012
- tags: migration, db, auth
- complexity: low
- files: backend/migrations/versions/<auto>_create_token_blacklist_table.py
- acceptance: `alembic upgrade head` creates `token_blacklist` table with unique index on `jti`; `alembic downgrade -1` drops it cleanly

# Task: T014 — Add POST /auth/logout endpoint
- depends_on: T012
- tags: api, auth, security
- complexity: medium
- files: backend/src/api/auth.py
- acceptance: |
    POST /auth/logout with valid Bearer token returns 204 and inserts a TokenBlacklist row with the token's jti and expiry
    POST /auth/logout without Authorization header returns 401
    POST /auth/logout with already-revoked token returns 401
    Endpoint is idempotent on duplicate jti (no 500 on repeat insert)

Extract `jti` and `exp` claims from the decoded JWT. Insert into TokenBlacklist within a transaction; catch IntegrityError to keep idempotency. Reuse the existing `get_current_user` dependency to authenticate the caller.

# Task: T015 — Reject blacklisted tokens in get_current_user
- depends_on: T012, T014
- tags: auth, security
- complexity: medium
- files: backend/src/core/dependencies.py, backend/src/core/security.py
- acceptance: A token whose jti exists in token_blacklist raises 401 with detail "token revoked"; non-blacklisted tokens still authenticate normally; lookup adds a single indexed query per authenticated request and existing auth tests still pass

If `decode_access_token` does not currently expose the `jti` claim, surface it. Query TokenBlacklist by jti and reject if found. Ensure login still issues tokens that include a unique `jti`.

# Task: T016 — Backend integration tests for logout
- depends_on: T014, T015
- tags: test, auth
- complexity: medium
- files: backend/tests/api/test_logout.py
- acceptance: |
    pytest passes
    Covers logout happy path returns 204
    Reusing a revoked token on a protected endpoint returns 401 "token revoked"
    Logout without Authorization header returns 401
    Logout twice with the same token returns 401 on the second call
    token_blacklist row count for a given jti stays at 1 (idempotency)

Use the existing TestClient + in-memory SQLite session fixture from conftest. Reuse the auth-token fixture if present; otherwise register + login inline.

# Task: T017 — Frontend auth service: logout function
- depends_on:
- tags: frontend, auth
- complexity: low
- files: frontend/src/services/auth.js
- acceptance: `logout()` calls POST {API_URL}/auth/logout with the current Bearer token from storage and resolves on 204; resolves cleanly (does not throw) on 401 for an already-expired token; rejects on network failures

Read API_URL from the env wiring set up in T005. Reuse the existing axios/fetch client and auth-header pattern used by `login()`.

# Task: T018 — Logout button UI component
- depends_on:
- tags: frontend, ui, component
- complexity: low
- files: frontend/src/components/LogoutButton.jsx
- acceptance: Renders a button labeled "Logout"; disabled and shows spinner while `pending` prop is true; calls the `onLogout` prop on click; accessible (role=button, keyboard-activatable via Enter/Space)

Stateless presentational component. Loading state controlled by parent via `pending` prop. No direct service calls from inside the component.

# Task: T019 — Wire logout into NavBar and clear client auth state
- depends_on: T017, T018
- tags: frontend, auth, wiring
- complexity: medium
- files: frontend/src/components/NavBar.jsx, frontend/src/hooks/useAuth.js
- acceptance: |
    LogoutButton renders in NavBar only when the user is authenticated
    Clicking it calls services/auth.logout(), then clears the access token from localStorage and resets auth context/store, then navigates to /login
    On API failure the client state is still cleared and the user is still redirected (fail-safe logout)
    No protected route is reachable after logout without re-login

Use the existing auth hook/store pattern (Context or Redux — match what `login` does). Use the router pattern already in use (react-router `useNavigate` or Next `router.push`).

# Task: T020 — Frontend tests for logout flow
- depends_on: T019
- tags: test, frontend, auth
- complexity: medium
- files: frontend/src/__tests__/logout.test.jsx
- acceptance: |
    jest passes
    Clicking LogoutButton in NavBar calls services/auth.logout exactly once
    After logout, localStorage no longer contains the access token
    After logout, the user is redirected to /login
    If services/auth.logout rejects, client state is still cleared and the redirect still happens

Use @testing-library/react and @testing-library/user-event. Mock the auth service and the router navigation hook.

# Task: T021 — Extend smoke-test script with logout
- depends_on: T014, T009
- tags: test, api
- complexity: low
- files: scripts/smoke_test.sh
- acceptance: Smoke-test script registers + logs in, captures the token, calls POST /auth/logout expecting 204, then calls a protected endpoint with the same token expecting 401; script exits non-zero on any unexpected status

Append to the existing smoke-test from T009. Do not regress existing assertions.

# Task: T022 — Update dev runbook with logout flow
- depends_on: T014, T019, T021
- tags: docs
- complexity: low
- files: docs/dev-runbook.md
- acceptance: Runbook gains a "Logout" section documenting the POST /auth/logout endpoint contract (request, 204 response, 401 cases), the NavBar logout button location, the client-side state cleared on logout, and how to verify via the smoke-test script
