from __future__ import annotations

import asyncio
import logging

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Dict, Optional, Awaitable

from ..login.schema import Credential, LoginResult
from ..login.register import SiteLoginServiceRegister
from ..events import Emitter, safe_emit
from .schema import SessionEntry, SessionResources, SessionStatus

from playwright.async_api import BrowserContext
from ..browser import BrowserManager
from ..storage import BaseStateStorage

logger = logging.getLogger(__name__)
    
class SessionManager:
    """ 
    Manage the session lifecycle after a successful login. Each session is associated with a unique credential_id.
    Requires resources to use in the current session, including BrowserContext, Page, and LoginResult.
    
    Flow:
    
        # 1. Login & register for session
        await session_manager.login_and_session_register(credential)
        
        # 2. Acquire resources to run the task
        async with session_manager.acquire_session(credential, "XMLInvoinceDownloadTask") as resources:
            await task.
            
    Each credential maps to exactly one SessionEntry in the registry.
    State machine per entry: PENDING → READY ↔ LOCKED.

    Typical flow via run_session():
    
        1. Login if no session exists  → PENDING → READY
        2. Acquire lock                → LOCKED
        3. Run task callback
        4. Release lock                → READY
        5. Apply TTL / keep_alive / unregister
    """
    
    def __init__(
        self,
        browser_manager: Optional[BrowserManager] = None,
        state_storage  : Optional[BaseStateStorage] = None
    ):
        
        self._browser_manager   = browser_manager
        self._state_storage     = state_storage
        
        self._registry          : Dict[str, SessionEntry] = {}
        self._ttl_watcher_task  : Optional[asyncio.Task]  = None
        
        self._credential_locks  : Dict[str, asyncio.Lock] = {}
        
    def _get_credential_lock(self, credential_id: str) -> asyncio.Lock:
        lock = self._credential_locks.get(credential_id)
        if lock is None:
            lock = asyncio.Lock()
            self._credential_locks[credential_id] = lock
        return lock
        
    # async def initialize_session(
    #     self, 
    #     credential  : Credential,
    #     max_attempts: int   = 3,
    #     retry_delay : float = 1.0,
    #     using_state : bool  = True,
    # ) -> LoginResult:
        
    #     """
    #     Login and register a new session slot.

    #     Reserves a PENDING slot before the network call so concurrent
    #     requests for the same credential are rejected immediately.

    #     Raises:
    #         RuntimeError: credential already registered, or login failed.
    #     """
        
    #     cred_id = credential.credential_id
    #     lock = self._get_credential_lock(cred_id)
        
    #     async with lock:
    #         if cred_id in self._registry:
    #             status = self._registry[cred_id].status
    #             raise RuntimeError(
    #                 f"Session '{cred_id}' already registered "
    #                 f"(site={credential.site!r}, status={status.name}). "
    #                 f"Call unregister() before logging in again."
    #             )
                
    #         return await self._session_unlocked(
    #             credential   = credential,
    #             max_attempts = max_attempts,
    #             retry_delay  = retry_delay,
    #             using_state  = using_state
    #         )
            
    async def _session_register_unlocked(
        self,
        credential: Credential,
        max_attempts: int = 3,
        retry_delay: float = 1.0,
        using_state: bool = True,
        emitter: Optional[Emitter] = None,
    ) -> LoginResult:
        """ 
        Internal login/register flow.
        """
        
        cred_id = credential.credential_id
        context: Optional[BrowserContext] = None
        
        entry = SessionEntry(credential_id = cred_id)
        entry.status = SessionStatus.PENDING
        self._registry[cred_id] = entry
        
        try:
            service = SiteLoginServiceRegister.from_credential(
                credential,
                browser_manager = self._browser_manager,
                state_storage   = self._state_storage if using_state else None
            )
            
            context, page, login_result = await service.login(
                credential   = credential,
                max_attempts = max_attempts,
                retry_delay  = retry_delay,
                using_state  = using_state,
                emitter      = emitter
            )
            
            if not login_result.success:
                await self._close_context(context)
                raise RuntimeError(
                    f"Login failed for credential '{cred_id}' "
                    f"(site={credential.site!r}): {login_result.error}"
                )
                
            if context is None or page is None:
                raise RuntimeError(
                    f"Login succeeded but NO context/page returned for credential '{cred_id}' "
                    f"(site={credential.site!r})."
                )
                
            entry.context = context
            entry.page = page
            entry.result = login_result
            entry.status = SessionStatus.READY
            
            logger.info(f"[Manager] Session '{cred_id}' READY from login (site={credential.site!r}).")
            return login_result
        
        except Exception:
            self._registry.pop(cred_id, None)
            await self._close_context(context)
            raise
        
    def _build_resources_from_entry(
        self,
        *,
        credential_id: str,
        entry: SessionEntry,
    ) -> SessionResources:
        
        return SessionResources(
            context        = entry.context,
            page           = entry.page,
            result         = entry.result,
            credential_id  = credential_id
        )
        
    async def _lock_session_unlocked(
        self,
        *,
        credential: Credential,
        task_name: str
    ) -> SessionResources:
        """ 
        Move a READY session to LOCKED state anđ return its resources. Raises if not READY.
        Must be called while Credential lock is held.
        """
        
        cred_id = credential.credential_id
        entry = self._registry.get(cred_id)
        
        if entry is None:
            raise RuntimeError(
                f"Session '{cred_id!r}' not found "
                f"(site={credential.site!r}). Call login_and_session_register() first."
            )
            
        if entry.status == SessionStatus.PENDING:
            raise RuntimeError(
                f"Session '{cred_id!r}' is PENDING "
                f"(site={credential.site!r}, login in progress). Try again later."
            )
            
        if entry.status == SessionStatus.LOCKED:
            raise RuntimeError(
                f"Session '{cred_id!r}' is LOCKED "
                f"(site={credential.site!r}, task={entry.current_task!r}). "
                f"Session is busy, try again later."
            )
            
        if entry.context is None or entry.page is None or entry.result is None:
            removed = self._registry.pop(cred_id, None)
            await self._close_context(removed.context if removed else None)
            raise RuntimeError(
                f"[Manager] Session '{cred_id!r}' is corrupted and has been removed."
            )
            
        entry.status = SessionStatus.LOCKED
        entry.current_task = task_name
        entry.ttl = None
        
        return self._build_resources_from_entry(
            credential_id = cred_id,
            entry = entry
        )
        
    ### Acquire / Release
        
    @asynccontextmanager
    async def _acquire_session(self, credential: Credential, task_name: str) -> AsyncGenerator[SessionResources, None]:
        """
        Lock the session for exclusive task use, yield resources, then release.
        Internal — all tasks must go through run_session().
        """
        
        cred_id = credential.credential_id
        lock = self._get_credential_lock(cred_id)
        
        async with lock:
            resources = await self._lock_session_unlocked(
                credential = credential,
                task_name  = task_name
            )
            
        try:
            yield resources
            
        finally:
            await self._release_after_task(
                credential_id = cred_id,
                task_name     = task_name,
                keep_alive   = False,
                ttl_seconds  = None,
            )
            
    async def _release_after_task(
        self,
        *,
        credential_id: str,
        task_name: str,
        keep_alive: bool,
        ttl_seconds: Optional[int]
    ): 
        """ 
        Cleanup after task completion: unlock session, apply TTL or unregister.
        """
        
        lock = self._get_credential_lock(credential_id)
        entry_to_close: Optional[SessionEntry] = None
        
        async with lock:
            entry = self._registry.get(credential_id)
            
            if entry is None:
                return
            
            if entry.status != SessionStatus.LOCKED:
                logger.warning(
                    f"[Manager] Session '{credential_id}' release skipped, not LOCKED: "
                    f"status={entry.status.name}, task={entry.current_task!r}"
                )
                return
            
            if entry.current_task != task_name:
                logger.warning(
                    f"[Manager] Session '{credential_id}' release skipped: owner task mismatch "
                    f"(current={entry.current_task!r}, releasing={task_name!r})"
                )
                return
            
            entry.current_task = None
            
            if self._is_context_crashed(entry.context):
                entry_to_close = self._registry.pop(credential_id, None)
                logger.warning(
                    f"[Manager] Session '{credential_id}' context crashed, removed."
                )
                
            elif ttl_seconds is not None and ttl_seconds > 0:
                entry.status = SessionStatus.READY
                entry.ttl = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
                logger.info(
                    f"[Manager] Session '{credential_id}' READY with TTL={ttl_seconds}s, expires_at={entry.ttl.isoformat()}"
                )
                
            elif keep_alive:
                entry.status = SessionStatus.READY
                entry.ttl = None
                logger.info(
                    f"[SessionManager] Session '{credential_id}' READY with keep_alive=True."
                )
                
            else:
                entry_to_close = self._registry.pop(credential_id, None)
                logger.info(
                    f"[SessionManager] Session '{credential_id}' removed after task completion."
                )
                
        if entry_to_close is not None:
            await self._close_context(entry_to_close.context)
    
    async def run_session(
        self,
        credential: Credential,
        task_name: str,
        callback: Callable[[SessionResources], Awaitable[Any]],
        using_state: bool = False,
        keep_alive: bool = False,
        ttl_seconds: Optional[int] = None,
        emitter: Optional[Emitter] = None,
    ) -> Any:

        """
        High-level task session lifecycle.

        Flow:
            1. Ensure a session exists for credential. If missing, login.
               Login progress events are streamed via `emitter` when provided.
            2. Atomically move READY → LOCKED.
            3. Run callback with resources (SessionResources) without holding the credential lock.
            4. Release, keep alive, set TTL, or unregister according to policy.
        """

        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0 or None.")

        cred_id = credential.credential_id
        lock = self._get_credential_lock(cred_id)

        async with lock:
            if cred_id not in self._registry:
                await self._session_register_unlocked(
                    credential  = credential,
                    using_state = using_state,
                    emitter     = emitter,
                )
                
            resources = await self._lock_session_unlocked(
                credential = credential,
                task_name  = task_name
            )
            
        try:
            return await callback(resources)
        
        finally:
            await self._release_after_task(
                credential_id = cred_id,
                task_name     = task_name,
                keep_alive    = keep_alive,
                ttl_seconds   = ttl_seconds,
            )
            
    async def unregister(self, credential: Credential, close_context: bool = True):
        cred_id = credential.credential_id
        await self._unregister_by_credential_id(
            credential_id = cred_id,
            close_context = close_context,
            reason = f"manual unregister site={credential.site!r}, credential_id={cred_id!r}"
        )
        
    async def _unregister_by_credential_id(
        self,
        credential_id: str,
        close_context: bool = True,
        reason: str = "unregister",
    ) -> None:
        lock = self._get_credential_lock(credential_id)
        entry_to_close: Optional[SessionEntry] = None

        async with lock:
            entry = self._registry.get(credential_id)

            if entry is None:
                return

            if entry.status == SessionStatus.LOCKED:
                raise RuntimeError(
                    f"Cannot unregister session {credential_id!r}: "
                    f"currently LOCKED by task={entry.current_task!r}."
                )

            entry_to_close = self._registry.pop(credential_id, None)
            logger.info(
                f"[SessionManager] Session {credential_id!r} unregistered ({reason})."
            )

        if close_context and entry_to_close is not None:
            await self._close_context(entry_to_close.context)
            
    async def force_close(self, credential_id: str) -> None:
        """ 
        Force close a session only if the session is kept alive or cooldown TTL.
        This is useful NOT ONLY for admin operations to free up resources BUT ALSO for automated cleanup and keep session alive to preview.
        """
        await self._unregister_by_credential_id(
            credential_id=credential_id,
            close_context=True,
            reason="force_close",
        )
    
    ### TTL Watcher
    
    async def start(self):
        """Start the background TTL watcher. Called by AppOperator lifespan."""
        
        if self._ttl_watcher_task and not self._ttl_watcher_task.done():
            return

        self._ttl_watcher_task = asyncio.create_task(self._ttl_watcher())
        
    async def stop(self):
        """Cancel the TTL watcher. Called by AppOperator lifespan on shutdown."""
        
        if not self._ttl_watcher_task:
            return

        self._ttl_watcher_task.cancel()

        try:
            await self._ttl_watcher_task
        except asyncio.CancelledError:
            pass
        finally:
            self._ttl_watcher_task = None
            
    async def _ttl_watcher(self, interval: float = 5.0):
        """
        Background loop that closes sessions whose TTL has elapsed.
        Only READY sessions are eligible — LOCKED sessions are never touched.
        """
        
        while True:
            await asyncio.sleep(interval)
            now = datetime.now(timezone.utc)

            expired_ids = [
                cid
                for cid, entry in list(self._registry.items())
                if entry.ttl is not None
                and entry.status == SessionStatus.READY
                and now >= entry.ttl
            ]

            for credential_id in expired_ids:
                await self._expire_if_ready(credential_id, now)
                
    async def _expire_if_ready(
        self,
        credential_id: str,
        now: datetime,
    ) -> None:
        lock = self._get_credential_lock(credential_id)
        entry_to_close: Optional[SessionEntry] = None

        async with lock:
            entry = self._registry.get(credential_id)

            if entry is None:
                return

            if entry.status != SessionStatus.READY:
                return

            if entry.ttl is None or now < entry.ttl:
                return

            entry_to_close = self._registry.pop(credential_id, None)
            logger.info(
                "[SessionManager] Session %r TTL expired; removed.",
                credential_id,
            )

        if entry_to_close is not None:
            await self._close_context(entry_to_close.context)

        
        
    ### Stats and utilities

    def stats(self) -> dict[str, Any]:
        return {
            "total": len(self._registry),
            "locks": len(self._credential_locks),
            "sessions": [
                {
                    "credential_id": cid,
                    "status": entry.status.name,
                    "current_task": entry.current_task,
                    "has_context": entry.context is not None,
                    "has_page": entry.page is not None,
                    "ttl": entry.ttl.isoformat() if entry.ttl else None,
                }
                for cid, entry in list(self._registry.items())
            ],
        }

    async def _close_context(
        self,
        context: Optional[BrowserContext],
    ) -> None:
        if context is None:
            return

        try:
            await self._browser_manager.close_context(context)
        except Exception as exc:
            logger.debug(
                "[SessionManager] Ignored error closing context through BrowserManager: %s",
                exc,
            )

    def is_registered(self, credential: Credential) -> bool:
        return credential.credential_id in self._registry

    def get_status(self, credential: Credential) -> Optional[SessionStatus]:
        entry = self._registry.get(credential.credential_id)
        return entry.status if entry else None

    def list_sessions(self) -> Dict[str, SessionStatus]:
        return {cid: entry.status for cid, entry in list(self._registry.items())}

    @staticmethod
    def _is_context_crashed(context: Optional[BrowserContext]) -> bool:
        if context is None:
            return True

        try:
            _ = context.pages
            return False
        except Exception:
            return True
