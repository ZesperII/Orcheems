from __future__ import annotations

import logging

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List, Literal, Optional, Sequence, TYPE_CHECKING

from fastapi import APIRouter, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field, field_validator

from .session.manager import SessionManager
from .login.schema import Credential
from .task.base import BaseTask
from .task.decorators import _pending_registrations

if TYPE_CHECKING:
    from .browser import BrowserManager
    from .storage import BaseStateStorage

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Response models
# ------------------------------------------------------------------

def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M %d/%m/%y")


class HealthResponse(BaseModel):
    status        : Literal["ok", "degraded"]
    storage       : Literal["ok", "unavailable", "n/a"]
    tasks         : int
    session_count : int
    timestamp     : str = Field(default_factory=_now_str)

    @field_validator("timestamp", mode="before")
    @classmethod
    def _fmt_timestamp(cls, v):
        if isinstance(v, datetime):
            return v.strftime("%H:%M %d/%m/%y")
        return v


class SessionInfo(BaseModel):
    credential_id : str
    status        : str
    current_task  : Optional[str]


class SessionsResponse(BaseModel):
    total    : int
    sessions : List[SessionInfo]


class SessionStatusResponse(BaseModel):
    """
    Tells the client what to do next:
        proceed        — session READY, send the task request now
        wait           — PENDING or LOCKED, poll again in a few seconds
        login_required — no session exists, call login first
    """
    credential_id : str
    status        : str
    current_task  : Optional[str]
    ready         : bool
    action        : Literal["proceed", "wait", "login_required"]


# ------------------------------------------------------------------
# Internal
# ------------------------------------------------------------------

@dataclass
class _TaskEntry:
    task   : BaseTask
    prefix : str
    tags   : List[str]


# ------------------------------------------------------------------
# AppOperator
# ------------------------------------------------------------------

class Orcheemstrator:
    """
    Wires tasks, session management, and FastAPI together.

    Usage — auto-discovery (recommended):

        # main.py
        import app.sites   # triggers @SiteLoginServiceRegister.register
        import app.tasks   # triggers @task_registration(...)

        from orcheems import Orcheemstrator
        from orcheems.storage import RedisStateStorage

        operator = Orcheemstrator(state_storage=RedisStateStorage())
        app = operator.auto_register_and_build()

    Usage — manual registration:

        app = (
            Orcheemstrator(state_storage=LocalStateStorage())
            .register_task(InvoiceTask(), prefix="/invoice", tags=["invoice"])
            .register_task(StockTask(),   prefix="/stock",   tags=["stock"])
            .build()
        )
    """

    def __init__(
        self,
        browser_manager : Optional[BrowserManager]   = None,
        state_storage   : Optional[BaseStateStorage] = None,
        **fastapi_kwargs,
    ) -> None:
        self._session_manager = SessionManager(browser_manager, state_storage)
        self._state_storage   = state_storage
        self._entries         : List[_TaskEntry]     = []
        self._routers         : Dict[str, APIRouter] = {}
        self._app             : Optional[FastAPI]    = None
        self._fastapi_kwargs  = fastapi_kwargs

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_task(
        self,
        task   : BaseTask,
        prefix : str                     = "",
        tags   : Optional[Sequence[str]] = None,
    ) -> Orcheemstrator:
        """
        Register a task and bind it to the shared SessionManager.
        Tasks with the same prefix share one APIRouter.
        Returns self for chaining.
        """
        if self._app is not None:
            raise RuntimeError("Cannot register tasks after build() has been called.")

        if not prefix.startswith("/"):
            raise ValueError(f"Prefix must start with '/': got {prefix!r}")

        task._bind_session_manager(self._session_manager)

        router_tags = list(tags or [task.__class__.__name__])
        self._entries.append(_TaskEntry(task=task, prefix=prefix, tags=router_tags))

        if prefix not in self._routers:
            self._routers[prefix] = APIRouter(prefix=prefix, tags=router_tags)

        return self

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> FastAPI:
        """
        Finalise the app: register all task routes and mount the management router.
        No further task registration is allowed after this call.
        """
        if self._app is not None:
            raise RuntimeError("build() has already been called.")

        app = FastAPI(lifespan=self._build_lifespan(), **self._fastapi_kwargs)

        for entry in self._entries:
            entry.task.register_route(self._routers[entry.prefix])
            logger.debug(
                "Registered task=%r → prefix=%r tags=%s",
                entry.task.__class__.__name__, entry.prefix, entry.tags,
            )

        for prefix, router in self._routers.items():
            app.include_router(router)
            logger.debug("Mounted router: %s", prefix)

        app.include_router(self._build_management_router())
        logger.debug("Mounted management router.")

        self._app = app
        return app

    def auto_register_and_build(self) -> FastAPI:
        """
        Instantiate all tasks collected by @task_registration, register them,
        then call build().

        All modules containing @task_registration decorators must be imported
        before this is called so their decorators have a chance to run.

        Can be combined with manual register_task() calls — manually registered
        tasks are preserved and decorator-registered tasks are appended after.
        """
        if not _pending_registrations:
            logger.warning(
                "No tasks found in the decorator registry. "
                "Did you import your app.tasks modules before calling auto_register_and_build()?"
            )

        for reg in _pending_registrations:
            instance = reg.task_cls()
            self.register_task(instance, prefix=reg.prefix, tags=reg.tags)
            logger.debug(
                "Auto-registered task=%r → prefix=%r tags=%s",
                instance.__class__.__name__, reg.prefix, reg.tags,
            )

        return self.build()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def session_manager(self) -> SessionManager:
        return self._session_manager

    def registered_prefixes(self) -> List[str]:
        return list(self._routers.keys())

    def registered_tasks(self) -> List[str]:
        return [e.task.__class__.__name__ for e in self._entries]

    # ------------------------------------------------------------------
    # Internal: lifespan
    # ------------------------------------------------------------------

    def _build_lifespan(self):
        entries         = self._entries
        storage         = self._state_storage
        session_manager = self._session_manager

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:

            # Ping storage on startup (catches misconfigured Redis early)
            if hasattr(storage, "ping"):
                try:
                    await storage.ping()
                    logger.info("Storage OK (%s).", storage.__class__.__name__)
                except Exception as exc:
                    logger.error("Storage connection failed: %s", exc)
                    raise

            # Run task on_startup hooks in registration order
            for entry in entries:
                try:
                    await entry.task.on_startup()
                    logger.info("%s: on_startup complete.", entry.task.__class__.__name__)
                except Exception as exc:
                    logger.error("%s: on_startup failed: %s", entry.task.__class__.__name__, exc)
                    raise

            await session_manager.start()
            logger.info("SessionManager: TTL watcher started.")

            try:
                yield
            finally:
                await session_manager.stop()
                logger.info("SessionManager: TTL watcher stopped.")

                # Reverse order on shutdown — mirror of startup
                for entry in reversed(entries):
                    try:
                        await entry.task.on_shutdown()
                        logger.info("%s: on_shutdown complete.", entry.task.__class__.__name__)
                    except Exception as exc:
                        logger.warning(
                            "%s: on_shutdown error (ignored): %s",
                            entry.task.__class__.__name__, exc,
                        )

                if hasattr(storage, "close"):
                    try:
                        await storage.close()
                        logger.info("Storage closed (%s).", storage.__class__.__name__)
                    except Exception as exc:
                        logger.warning("Storage close error (ignored): %s", exc)

        return lifespan

    # ------------------------------------------------------------------
    # Internal: management router
    # ------------------------------------------------------------------

    def _build_management_router(self) -> APIRouter:
        """
        Built-in endpoints (no prefix, tagged "Management"):
            GET    /health            — liveness + storage check
            GET    /sessions          — list all active sessions
            POST   /sessions/status   — check one session by Credential
            DELETE /sessions/{id}     — force-close a READY session
        """
        router          = APIRouter(tags=["Management"])
        session_manager = self._session_manager
        state_storage   = self._state_storage
        entries         = self._entries

        @router.get("/health", response_model=HealthResponse)
        async def health() -> HealthResponse:
            storage_status: Literal["ok", "unavailable", "n/a"] = "n/a"
            if state_storage is not None and hasattr(state_storage, "ping"):
                try:
                    await state_storage.ping()
                    storage_status = "ok"
                except Exception:
                    storage_status = "unavailable"

            return HealthResponse(
                status        = "ok" if storage_status != "unavailable" else "degraded",
                storage       = storage_status,
                tasks         = len(entries),
                session_count = len(session_manager.list_sessions()),
                timestamp     = datetime.now(timezone.utc),
            )

        @router.get("/sessions", response_model=SessionsResponse)
        async def list_sessions() -> SessionsResponse:
            raw = session_manager.list_sessions()
            sessions = [
                SessionInfo(
                    credential_id = cid,
                    status        = status.name,
                    current_task  = session_manager._registry[cid].current_task,
                )
                for cid, status in raw.items()
            ]
            return SessionsResponse(total=len(sessions), sessions=sessions)

        @router.post(
            "/sessions/status",
            response_model=SessionStatusResponse,
            responses={
                200: {"description": "READY — send task request now"},
                404: {"description": "Not found — login first"},
                409: {"description": "LOCKED or PENDING — retry later"},
            },
        )
        async def session_status(credential: Credential, response: Response) -> SessionStatusResponse:
            """
            Guard layer — call before sending a task request to cheaply reject
            duplicate or conflicting requests before any browser resource is allocated.
            """
            credential_id = credential.credential_id
            entry = session_manager._registry.get(credential_id)

            if entry is None:
                response.status_code = 404
                return SessionStatusResponse(
                    credential_id = credential_id,
                    status        = "NOT_FOUND",
                    current_task  = None,
                    ready         = False,
                    action        = "login_required",
                )

            is_ready = entry.status.name == "READY"
            response.status_code = 200 if is_ready else 409

            return SessionStatusResponse(
                credential_id = credential_id,
                status        = entry.status.name,
                current_task  = entry.current_task,
                ready         = is_ready,
                action        = "proceed" if is_ready else "wait",
            )

        @router.delete("/sessions/{credential_id}", status_code=200)
        async def force_delete_session(credential_id: str):
            """Force-close a READY session. Returns 409 if the session is currently LOCKED."""
            try:
                await session_manager.force_close(credential_id)
                return {"status": "closed", "credential_id": credential_id}
            except KeyError:
                raise HTTPException(status_code=404, detail="Session not found.")
            except RuntimeError as exc:
                raise HTTPException(status_code=409, detail=str(exc))

        return router