# Orcheems

<p align="center">
  <img src="public/cheems.jpg" alt="Orcheems Framework" width="400">
</p>

**Centralized browser session orchestration for Playwright + FastAPI.**

Orcheems solves one problem and solves it well: managing shared browser sessions across concurrent tasks without login conflicts, resource leaks, or race conditions. Built for internal automation services that need to stay alive under load.

```
pip install orcheems
---
git clone https://github.com/ZesperII/Orcheems.git
```

---

## Why Orcheems

Running multiple Playwright tasks against the same authenticated site is harder than it looks. Naive implementations either login on every request (slow, rate-limited) or share browser contexts between tasks (race conditions, session corruption). Orcheems sits in the middle: one session per credential, one task at a time per session, automatic cookie reuse, and a TTL watcher that cleans up idle contexts before they leak RAM.

The core idea: tasks declare what they want to do with a page. Orcheems handles everything else.

<p align="center">
  <img src="public/flow1.svg" alt="Orcheems Framework">
</p>
---

## How it works

Three layers, each with a single responsibility:

```
BrowserManager      — one shared Chromium process per worker
    └── SessionManager  — one context per credential, PENDING → READY ↔ LOCKED
            └── BaseTask    — where you write business logic, nothing else
```

**Session states:**

| State | Meaning |
|---|---|
| `PENDING` | Login in progress — slot reserved, all requests rejected |
| `READY` | Idle, available for the next task |
| `LOCKED` | Task running — no concurrent access allowed |

When a task calls `with_page()`, Orcheems logs in if needed, locks the session, runs your code, then releases the lock. If another request arrives while the session is LOCKED, it gets a `409` immediately — no queuing, no silent waiting, no corrupted state.

---

## Installation

```bash
pip install orcheems

# Install Playwright browsers after
playwright install chromium
```

**Requirements:** Python 3.12+

---

## Quickstart

### 1. Implement a login service for your site

```python
# app/sites/vnpt.py
from orcheems import BaseLoginService, SiteLoginServiceRegister
from orcheems.login.base import cookie_incomplete_handler
from playwright.async_api import BrowserContext, Page

@SiteLoginServiceRegister.register
class VNPTLoginService(BaseLoginService):
    SITE = "vnpt"

    async def _perform_login(
        self,
        page: Page,
        context: BrowserContext,
        credential,
    ) -> Page:
        await page.goto(credential.base_url, wait_until="networkidle")
        await page.fill("#UserName", credential.data["username"])
        await page.fill("#Password", credential.data["password"])
        await page.click("button[type='submit']")
        return page

    async def _is_session_valid(self, page: Page) -> bool:
        try:
            return await page.wait_for_selector("#logted", timeout=5000) is not None
        except Exception:
            return False
```

Two methods to implement — that's it. `_perform_login` runs your login steps. `_is_session_valid` checks whether the resulting page is actually authenticated.

### 2. Write a task

```python
# app/tasks/invoice.py
from orcheems import BaseTask, Credential, task_registration
from fastapi import APIRouter
from pydantic import BaseModel

@task_registration(prefix="/vnpt", tags=["vnpt"])
class InvoiceDownloadTask(BaseTask):

    def register_route(self, router: APIRouter):

        class Body(BaseModel):
            credential: Credential
            invoice_id: str

        @router.post("/download")
        async def download(body: Body):
            result = await self.with_page(
                body.credential,
                lambda page: self._fetch_invoice(page, body.invoice_id),
                using_state=True,   # try saved cookies first
                ttl_seconds=120,    # keep context alive for 2 min after task
            )
            return {"status": "ok", "data": result}

    async def _fetch_invoice(self, page, invoice_id: str):
        await page.goto(f"/invoices/{invoice_id}")
        return await page.inner_text(".invoice-total")
```

### 3. Add auto-discovery to each app package

`import app.sites` only runs `app/sites/__init__.py` — it does **not**
automatically import `vnpt.py`, `wfx.py`, or any other file inside the
package. Without auto-discovery, the decorators in those files never run
and both registries stay empty.

Add this to `app/sites/__init__.py` and `app/tasks/__init__.py`:

```python
# app/sites/__init__.py  (repeat identically for app/tasks/__init__.py)
import importlib
import pkgutil
from pathlib import Path

for _, module_name, _ in pkgutil.iter_modules([str(Path(__file__).parent)]):
    importlib.import_module(f"{__name__}.{module_name}")
```

Now `import app.sites` triggers every `@SiteLoginServiceRegister.register`
in the package, and `import app.tasks` triggers every `@task_registration`.
Adding a new site or task is just adding a new file — no other changes needed.

### 4. Wire everything together

```python
# main.py
import app.sites  # triggers @SiteLoginServiceRegister.register
import app.tasks  # triggers @task_registration(...)

from orcheems import Orcheemstrator
from orcheems.storage import RedisStateStorage
from fastapi.middleware.cors import CORSMiddleware

operator = Orcheemstrator(
    state_storage=RedisStateStorage(),  # or LocalStateStorage(".cookies")
)
app = operator.auto_register_and_build()

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
```

```bash
uvicorn main:app --reload
```

---

## Credential identity

Orcheems identifies accounts using **UUIDv5** derived deterministically from `(site, base_url, data)`:

```python
from orcheems import Credential

credential = Credential(
    site     = "vnpt",
    base_url = "https://example-tt78.vnpt-invoice.com.vn/",
    data     = {"username": "admin", "password": "secret"},
)

print(credential.credential_id)
# → "3f2a1b4c-..." — always the same for the same input
```

Same credential object from any client always maps to the same session slot. No external ID management needed.

---

## Session lifecycle

### Cookie reuse (bypass login)

Pass `using_state=True` to attempt login via saved cookies before triggering a full browser login:

```python
result = await self.with_page(
    credential,
    lambda page: do_work(page),
    using_state=True,
)
```

If the saved state is invalid, Orcheems falls back to full login automatically.

### Multi-step cookie recovery

Some sites require an extra step (OTP, captcha re-entry) when cookies are partially valid. Use `@cookie_incomplete_handler`:

```python
from orcheems.login.base import cookie_incomplete_handler

class MyLoginService(BaseLoginService):
    SITE = "mysite"

    @cookie_incomplete_handler
    async def handle_otp(self, context, page, credential):
        await page.fill("#otp", credential.data["otp"])
        await page.click("#submit")
        return page

    async def _perform_login(self, page, context, credential) -> Page:
        ...

    async def _is_session_valid(self, page) -> bool:
        ...
```

### Keep-alive and TTL

By default, the browser context is closed immediately after a task completes. Use `keep_alive` or `ttl_seconds` to hold it open for reuse:

```python
# Keep alive indefinitely until manually closed or server restart
await self.with_page(credential, work, keep_alive=True)

# Keep alive for 90 seconds, then auto-close
await self.with_page(credential, work, ttl_seconds=90)
```

The TTL watcher runs every 5 seconds in the background and only closes `READY` sessions — it never interrupts a running task.

---

## SSE streaming

For long-running tasks, use `with_page_stream()` to push progress events back to the client:

```python
@router.post("/crawl")
async def crawl(body: Body):

    async def work(page, emit):
        await emit("progress", {"step": "navigating"})
        await page.goto("/data")

        await emit("progress", {"step": "extracting"})
        rows = await page.query_selector_all("tr")

        return {"count": len(rows)}

    return self.with_page_stream(body.credential, work, using_state=True)
```

Client receives a stream of newline-delimited JSON events:

```
data: {"type": "progress", "data": {"step": "navigating"}}
data: {"type": "progress", "data": {"step": "extracting"}}
data: {"type": "done",     "data": {"count": 42}}
```

---

## Storage backends

```python
from orcheems.storage import LocalStateStorage, RedisStateStorage

# Local files — good for development
LocalStateStorage(".cookies")          # layout: .cookies/{site}/{credential_id}.json

# Redis — recommended for production
RedisStateStorage()                    # reads REDIS_URL from environment
RedisStateStorage("redis://localhost:6379/0", ttl_seconds=10800)
```

Implement `BaseStateStorage` to add your own backend (S3, database, etc.).

---

## Management API

Orcheems mounts a built-in management router on every app:

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness check + storage status |
| `GET` | `/sessions` | List all active sessions |
| `POST` | `/sessions/status` | Check one session by Credential |
| `DELETE` | `/sessions/{credential_id}` | Force-close a READY session |

**Guard pattern** — call `/sessions/status` before sending a task request to detect conflicts cheaply, before any browser resource is allocated:

```bash
POST /sessions/status
{"site": "vnpt", "base_url": "https://...", "data": {...}}

# 200 → {"action": "proceed",        "ready": true}
# 409 → {"action": "wait",           "ready": false}   # LOCKED or PENDING
# 404 → {"action": "login_required", "ready": false}   # no session yet
```

---

## Manual registration

Auto-discovery via `auto_register_and_build()` is the recommended pattern, but you can register tasks manually:

```python
from orcheems import Orcheemstrator
from orcheems.storage import LocalStateStorage
from app.tasks.invoice import InvoiceDownloadTask
from app.tasks.stock import StockTask

app = (
    Orcheemstrator(state_storage=LocalStateStorage(".cookies"))
    .register_task(InvoiceDownloadTask(), prefix="/invoice", tags=["invoice"])
    .register_task(StockTask(),           prefix="/stock",   tags=["stock"])
    .build()
)
```

---

## Project layout

```
your-project/
├── orcheems/              # the framework — don't edit
├── app/
│   ├── sites/
│   │   ├── __init__.py    # auto-discovers all site modules
│   │   ├── vnpt.py        # @SiteLoginServiceRegister.register
│   │   └── wfx.py
│   └── tasks/
│       ├── __init__.py    # auto-discovers all task modules
│       └── invoice.py     # @task_registration(...)
├── main.py                # entry point
└── pyproject.toml
```

---

## License

MIT