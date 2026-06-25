from __future__ import annotations

import asyncio
import logging

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable, Dict, Optional, Awaitable

from ..login.schema import Credential, LoginResult
from ..login.register import SiteLoginServiceRegister
from .schema import SessionEntry, SessionResources, SessionStatus

if TYPE_CHECKING:
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
        self._ttl_watcher_task  : Optional[asyncio.Task]    = None
        
    async def initialize_session(
        self, 
        credential  : Credential,
        max_attempts: int   = 3,
        retry_delay : float = 1.0,
        using_state : bool  = True,
    ) -> LoginResult:
        
        """
        Login and register a new session slot.

        Reserves a PENDING slot before the network call so concurrent
        requests for the same credential are rejected immediately.

        Raises:
            RuntimeError: credential already registered, or login failed.
        """
        
        credential_id = credential.credential_id
        
        if credential_id in self._registry:
            status = self._registry[credential_id].status
            
            raise RuntimeError(
                f"Session {credential_id!r} already registered "
                f"(site={credential.site!r}, status={status.name}). "
                f"Call unregister() before attempting to login again."
            )
            
        # Reserve PENDING slot — blocks concurrent duplicate requests
        self._registry[credential_id] = SessionEntry(credential_id = credential_id)
        
        try:
            # get login service for the site and perform login
            service = SiteLoginServiceRegister.from_credential(
                credential,
                browser_manager = self._browser_manager,
                state_storage = self._state_storage
            )
            
            context, page, result = await service.login(
                credential      = credential,
                max_attempts    = max_attempts,
                retry_delay     = retry_delay,
                using_state     = using_state
            )
            
            if not result.success:
                raise RuntimeError(
                    f"Login failed for site={credential.site!r}: {result.error}"
                )
                
            entry = self._registry[credential_id]
            entry.context = context
            entry.page = page
            entry.result = result
            entry.status = SessionStatus.READY
            return result
        
        except Exception as e:
            self._registry.pop(credential_id, None)
            raise e
        
    @asynccontextmanager
    async def _acquire_session(self, credential: Credential, task_name: str) -> AsyncGenerator[SessionResources, None]:
        """
        Lock the session for exclusive task use, yield resources, then release.
        Internal — all tasks must go through run_session().
        """
        
        cred_id = credential.credential_id
        entry = self._registry.get(cred_id)
        
        if entry is None:
            raise RuntimeError(
                f"Session '{cred_id}' not found "
                f"(site={credential.site!r}). Call `initialize_session()` trước."
            )
            
        if entry.status == SessionStatus.PENDING:
            raise RuntimeError(
                f"Session {cred_id!r} is PENDING (login in progress). "
                f"Try again shortly."
            )

        if entry.status == SessionStatus.LOCKED:
            raise RuntimeError(
                f"Session {cred_id!r} is LOCKED by task={entry.current_task!r}. "
                f"Only one task may run per session at a time."
            )
            
        entry.status = SessionStatus.LOCKED
        entry.current_task = task_name
        
        try:
            yield SessionResources(
                context         = entry.context,
                page            = entry.page,
                result          = entry.result,
                credential_id   = cred_id
            )
            
        except Exception as exc:
            if self._is_context_crashed(entry.context):
                self._registry.pop(cred_id, None)
            
            else:
                logger.warning(f"Session {cred_id!r}: task={task_name!r} failed: {exc}")
                
            raise exc
        
        finally:
            if cred_id in self._registry:
                entry.status = SessionStatus.READY
                entry.current_task = None
                logger.info(f"Session {cred_id!r}: READY (released from task={task_name!r})")
    
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
            
            expired = [
                cid for cid, entry in self._registry.items()
                if entry.ttl is not None
                and entry.status == SessionStatus.READY
                and now >= entry.ttl
            ]
            
            for cid in expired:
                entry = self._registry.pop(cid, None)
                
                if entry.context:
                    try:
                        await entry.context.close()
                    except Exception as exc:
                        logger.debug(f"Error closing expired context {cid!r}: {exc}")
                        
                logger.info(f"Session {cid!r}: TTL expired — context closed.")

    async def force_close(self, credential_id: str):
        """
        Forcibly remove a READY session without a Credential object.
        Used by the management API (DELETE /sessions/{credential_id}).

        Raises:
            KeyError:    session not found.
            RuntimeError: session is LOCKED — use override only in emergencies.
        """
        
        entry = self._registry.get(credential_id)
        if entry is None:
            raise KeyError(credential_id)
        
        if entry.status == SessionStatus.LOCKED:
            raise RuntimeError(
                f"Session '{credential_id}' is LOCKED by task='{entry.current_task}'. "
                f"Cannot force-close while task is running."
            )
        self._registry.pop(credential_id, None)
        if entry.context:
            try:
                await entry.context.close()
            except Exception as exc:
                logger.debug(f"Error closing force-closed context '{credential_id}': {exc}")
        logger.info(f"Session '{credential_id}': force-closed.")
        
                
    async def run_session(
        self,
        credential  : Credential,
        task_name   : str,
        callback    : Callable[[SessionResources], Awaitable[Any]],
        using_state : bool          = False,
        keep_alive  : bool          = False,
        ttl_seconds : Optional[int] = None
    ) -> Any:
        """ 
        Full session lifecycle in one call: login → lock → run → release.

        Post-task behaviour (mutually exclusive, evaluated in order):
            ttl_seconds set  → keep context alive, auto-close after N seconds
            keep_alive=True  → keep context alive indefinitely (no TTL)
            neither          → unregister and close context immediately

        Args:
            callback:    Async function receiving SessionResources, returns any value.
            using_state: Attempt cookie bypass before full login.
            keep_alive:  Keep context alive after task completes.
            ttl_seconds: Seconds to keep context alive (max 300).
        """
        
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be > 0.")
        
        cred_id = credential.credential_id
        
        if cred_id not in self._registry:
            await self.initialize_session(credential, using_state=using_state)
        
        result: Any = None
        try:
            async with self._acquire_session(credential, task_name) as resources:
                result =  await callback(resources)
            
        finally:
            entry = self._registry.get(cred_id) 
            if entry is not None:
                if ttl_seconds is not None and ttl_seconds > 300 and not keep_alive:
                    logger.warning(f"Session '{cred_id}': TTL {ttl_seconds}s exceeds 300s limit — unregistering now.",)
                    await self.unregister(credential)
                
                # if ttl_seconds -> set TTL (keep_alive=True/False)
                elif ttl_seconds is not None and ttl_seconds < 300:
                    entry.ttl = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
                    logger.info(f"Session '{cred_id}': TTL set to {ttl_seconds}s, expires_at={entry.ttl.isoformat()}",)
                    
                elif keep_alive:
                    entry.ttl = None
                    logger.info(f"Session '{cred_id}': keep_alive=True, no TTL.")
                    
                # no ttl / keep_alive -> unregister immediately
                else:
                    await self.unregister(credential)
                    
        return result
    
    async def unregister(
        self,
        credential      : Credential,
        close_context   : bool = True
    ):
        """
        Remove session from registry and optionally close its browser context.

        Raises:
            RuntimeError: session is LOCKED (task still running).
        """
        
        cred_id = credential.credential_id
        entry = self._registry.get(cred_id)
        
        if entry is None:
            logger.warning(f"unregister: session '{cred_id}' not found, skip.")
            return
        
        if entry.status == SessionStatus.LOCKED:
            raise RuntimeError(
                f"Cannot unregister session '{cred_id}': "
                f"currently LOCKED by task='{entry.current_task}'."
            )
            
        self._registry.pop(cred_id)
        
        if close_context and entry.context:
            try:
                await entry.context.close()
            except Exception as exc:
                logger.debug("Ignored error closing context on unregister: %s", exc)
                raise exc
                
        logger.info(f"Session '{cred_id}': unregistered (site={credential.site})")
        
    ### Instrospection
    def is_registered(self, credential: Credential) -> bool:
        return credential.credential_id in self._registry
 
    def get_status(self, credential: Credential) -> Optional[SessionStatus]:
        entry = self._registry.get(credential.credential_id)
        return entry.status if entry else None
 
    def list_sessions(self) -> Dict[str, SessionStatus]:
        return {cid: e.status for cid, e in self._registry.items()}
    
    ### Internal
    @staticmethod
    def _is_context_crashed(context: Optional[BrowserContext]) -> bool:
        """Return True if the BrowserContext is no longer usable."""
        if context is None:
            return True
        try:
            _ = context.pages
            return False
        except Exception:
            return True