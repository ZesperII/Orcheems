from __future__ import annotations

import os
import asyncio
import logging

from dataclasses import dataclass, field
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any, Optional, AsyncGenerator, AsyncIterator

from dotenv import load_dotenv
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Playwright,
)

from .config import BROWSER

load_dotenv()
logger = logging.getLogger(__name__)

###-------------------------------------- Exceptions --------------------------------------

class BrowserManagerError(RuntimeError):
    """Base class for exceptions raised by the BrowserManager."""
    
class BrowserNotStartedError(BrowserManagerError):
    """Raised when the browser is required but has not been started yet."""

class BrowserRestartingError(BrowserManagerError):
    """Raised when the browser is restarting and cannot accept new contexts."""

class BrowserContextLimitError(BrowserManagerError):
    """Raised when the active context limit is reached."""


class BrowserContextAcquireTimeoutError(BrowserManagerError):
    """Raised when waiting for an available context slot times out."""


class BrowserStartError(BrowserManagerError):
    """Raised when Playwright or browser startup fails."""


class BrowserRestartError(BrowserManagerError):
    """Raised when browser restart fails."""


class BrowserContextCreateError(BrowserManagerError):
    """Raised when creating a new browser context fails."""

###-------------------------------------- Context meta --------------------------------------

@dataclass(slots=True)
class ContextInfo:
    """ 
    Metadata for a tracked BrowserContext.
    
    This is useful for debugging context leaks and 
    mapping active contexts back to the task/session/user/credential that created them.
    """
    
    context: BrowserContext
    task_id: Optional[str] = None
    credential_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "credential_id": self.credential_id,
            "created_at": self.created_at.isoformat(),
            "age_seconds": round(self.age_seconds(), 3),
        }
        
###-------------------------------------- Browser Manager --------------------------------------

class BrowserManager:
    """ 
    Manage the Playwright browser lifecycle.
    
    Design principles:
        - Browser instance can be shared inside one worker/process.
        - `BrowserContext` must NOT be shared between users/tasks/sessions.
        - `Page` must NOT be shared between users/tasks/sessions.
        - Each task should create a new BrowserContext.
        - If an old session needs to be restored, pass `storage_state`.
        - After a task is complete, save storage_state if needed and close context.
        
    Recommended structure:
    
        BrowserManager
            └── shared Browser instance per worker/process
                    ├── isolated BrowserContext for task A
                    ├── isolated BrowserContext for task B
                    └── isolated BrowserContext for task C

    Public API:
    
        await browser_manager.start()
        await browser_manager.close()
        await browser_manager.restart(force=False)

        context = await browser_manager.new_context(...)
        await browser_manager.close_context(context)

        async with browser_manager.managed_context(...) as context:
            ...

        browser_manager.stats()
        await browser_manager.healthcheck()
    """
    
    def __init__(
        self,
        max_concurrent_contexts: int = 30,
        context_acquire_timeout: float = 30.0,
        close_timeout: float = 10.0,
    ):
        
        if max_concurrent_contexts <= 0:
            raise ValueError("max_concurrent_contexts must be a positive integer.")
        if context_acquire_timeout <= 0:
            raise ValueError("context_acquire_timeout must be a positive number.")
        if close_timeout <= 0:
            raise ValueError("close_timeout must be a positive number.")
        
        self._browser_cfg: dict[str, Any] = BROWSER
        
        # playwright runtime
        self._playwright: Optional[Playwright] = None
        
        # shared browser instance
        self._browser: Optional[Browser] = None
        
        # active contexts tracking
        self._contexts: dict[BrowserContext, ContextInfo] = {}
        
        # context capacity control
        self._max_contexts = max_concurrent_contexts
        self._context_semaphore = asyncio.Semaphore(max_concurrent_contexts)
        self._context_acquire_timeout = context_acquire_timeout
        
        # Close timeout for browser/context cleanup
        self._close_timeout = close_timeout
        
        # locks
        self._lifecycle_lock = asyncio.Lock()  # for start/close/restart
        self._context_lock = asyncio.Lock()
        
        # runtime flags
        self._is_restarting = False
        
    # Property
    
    @property
    def is_dev_env(self) -> bool:
        """Check if the environment is a development environment."""
        return os.getenv("APP_ENV", "DEV") == "DEV"
    
    @property
    def is_started(self) -> bool:
        """ 
        Return True if the shared browser exists and is still connected.
        """
        return self._browser is not None and self._browser.is_connected()
    
    @property
    def browser(self) -> Browser:
        """
        Return the current shared browser.

        This property intentionally has no setter. The browser lifecycle must be
        controlled only by BrowserManager.
        """
        if not self._browser or not self._browser.is_connected():
            raise BrowserNotStartedError(
                "Browser has not been started. "
                "Call `await browser_manager.start()` first."
            )

        return self._browser
    
    @property
    def is_restarting(self) -> bool:
        return self._is_restarting

    @property
    def max_contexts(self) -> int:
        return self._max_contexts

    @property
    def active_contexts_count(self) -> int:
        return len(self._contexts)

    @property
    def available_context_slots(self) -> int:
        return max(self._max_contexts - len(self._contexts), 0)
    
    ###-------------------------------------- runtime / stats / health --------------------------------------
    
    def stats(self, included_contexts: bool = True) -> dict[str, Any]:
        """ 
        Return browser runtime statistics.
        """
        
        data: dict[str, Any] = {
            "started": self.is_started,
            "restarting": self.is_restarting,
            "active_contexts": len(self._contexts),
            "max_contexts": self._max_contexts,
            "available_context_slots": self.available_context_slots,
            "browser_connected": self._browser.is_connected() if self._browser else False,
            "playwright_started": self._playwright is not None,
            "context_acquire_timeout": self._context_acquire_timeout,
            "close_timeout": self._close_timeout,
        }
        
        if included_contexts:
            data["contexts"] = [
                info.to_dict()
                for info in self._contexts.values()
            ]
            
        return data
    
    async def healthcheck(self) -> dict[str, Any]:
        """ 
        Perform a real browser health check.
        
        `is_started` only tell us the browser object exists and is connected.
        This method verifies that a context and page can actually be created.
        """
        
        result = {
            "healthy": False,
            "started": self.is_started,
            "restarting": self.is_restarting,
            "browser_connected": self._browser.is_connected() if self._browser else False,
            "playwright_started": self._playwright is not None,
            "can_create_context": False,
            "can_create_page": False,
            "error": None,
        }
        
        if self._is_restarting:
            result["error"] = "Browser is restarting."
            return result
        
        if not self.is_started:
            result["error"] = "Browser is not started."
            return result
        
        context = None
        
        try:
            context = await self.browser.new_context(
                viewport={"width": 800, "height": 600},
                ignore_https_errors=True,
            )
            result["can_create_context"] = True
            
            page = await context.new_page()
            await page.goto("about:blank")
            result["can_create_page"] = True
            
            result["healthy"] = True
            return result
        
        except Exception as e:
            result["error"] = str(e)
            return result
        
        finally:
            if context is not None:
                try:
                    await asyncio.wait_for(
                        context.close(),
                        timeout = self._close_timeout,
                    )
                except Exception as e:
                    logger.debug(f"Ignored healthcheck cleanup error: {e}")
                    
    ###-------------------------------------- lifecycle management --------------------------------------
    
    async def start(self):
        """ 
        Start Playwright and launch the shared browser.
        
        Safe to call multiple times. If the browser is already started, this method dose nothing.
        """
        
        async with self._lifecycle_lock:
            if self.is_started:
                return
            
            try:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                    
                self._browser = await self._launch_browser()
                logger.info("Browser started.")
                
            except Exception as e:
                await self._cleanup_failed_start()
                raise BrowserStartError(
                    f"Browser failed to start browser: {str(e)}"
                ) from e
                
    async def close(self):
        """ 
        Close all active contexts, close the browser and stop Playwright.
        
        Should be called when shutting down the worker/app.
        Do not call this after each task. After each task, close only the context
        """
        
        async with self._lifecycle_lock:
            async with self._context_lock:
                await self._close_all_contexts()
                
            await self._close_browser_only()
            await self._stop_playwright_only()
            
            logger.info("Browser closed.")
            
    async def restart(self, force: bool = False):
        """ 
        Restart Playwright and the shared browser.
        
        Args:
            force:
                - False: refuse to restart if active contexts exist.
                - True: force restart even if active contexts exist. All active contexts will be closed.
                
        This is useful for admin/emergency recovery when Playwright or browser
        runtime becomes inconsistent.
        """
        
        async with self._lifecycle_lock:
            if self._contexts and not force:
                raise BrowserRestartError(
                    f"Browser cannot restart while {len(self._contexts)} "
                    f"contexts are active. Use force=True to force restart and close all contexts."
                )
                
            self._is_restarting = True
            
            try:
                async with self._context_lock:
                    await self._close_all_contexts()
                    
                await self._close_browser_only()
                await self._stop_playwright_only()
                
                self._playwright = await async_playwright().start()
                self._browser = await self._launch_browser()
                
                logger.warning("Browser restarted successfully.")
                
            except Exception as e:
                await self._cleanup_failed_start()
                raise BrowserRestartError(
                    f"Browser failed to restart: {str(e)}"
                )
                
            finally:
                self._is_restarting = False
                
    async def __aenter__(self) -> BrowserManager:
        await self.start()
        return self
    
    async def __aexit__(self, *args: object):
        await self.close()

    ###-------------------------------------- context API --------------------------------------
    
    async def new_context(
        self,
        *,
        task_name: Optional[str] = None,
        credential_id: Optional[str] = None,
        **kwargs: Any,
    ) -> BrowserContext:
        """ 
        Create a new isolated BrowserContext.

        Each new context has its own:
            - cookies
            - localStorage
            - sessionStorage
            - permissions
            - cache
            - pages

        Args:
            task_id:
                Optional task identifier for debugging/tracking.

            user_id:
                Optional user identifier for debugging/tracking.

            credential_id:
                Optional credential identifier for debugging/tracking.

            **kwargs:
                Playwright browser.new_context options.

        Example:
            context = await browser_manager.new_context(
                task_id="invoice_download",
                user_id="user_001",
                credential_id="vnpt_account_001",
                storage_state="sessions/vnpt/user_001.json",
            )

        Important:
            Do not share one context between multiple users/tasks/sessions.
        """
        
        if self._is_restarting:
            raise BrowserRestartingError(
                "Browser - Restarting. Cannot create new context. Try later."
            )
            
        if not self.is_started:
            await self.start()
            
        semaphore_acquired = False
        
        try:
            await self._acquire_context_slot()
            semaphore_acquired = True
            
            async with self._context_lock:
                if self._is_restarting:
                    raise BrowserRestartingError(
                        "Browser is restarting. Cannot create new context. Try later."
                    )
                    
                self._discard_closed_contexts_best_effort()
                
                if len(self._contexts) >= self._max_contexts:
                    raise BrowserContextLimitError(
                        f"Browser - Too many concurrent contexts: "
                        f"{len(self._contexts)}/{self._max_contexts}"
                    )
                    
                context_options = self._build_context_options(**kwargs)
                context = await self.browser.new_context(**context_options)
                
                self._track_context(
                    context = context,
                    task_name = task_name,
                    credential_id = credential_id
                )
                
                semaphore_acquired = False
                return context
            
        except BrowserManagerError as e:
            if semaphore_acquired:
                self._release_context_slot_safely()
            raise
                
        except Exception as exc:
            if semaphore_acquired:
                self._release_context_slot_safely()

            raise BrowserContextCreateError(
                f"Browser - Failed to create new context: {exc}"
            ) from exc
            
    @asynccontextmanager
    async def managed_context(
        self,
        *,
        task_name: Optional[str] = None,
        credential_id: Optional[str] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[BrowserContext, None]:
        """
        Create and automatically close a BrowserContext.

        Recommended usage for task/session runner:

            async with browser_manager.managed_context(
                task_id="invoice_download",
                user_id="user_001",
                credential_id="vnpt_account_001",
                storage_state="sessions/vnpt/user_001.json",
            ) as context:
                page = await context.new_page()
                await page.goto("https://example.com")

        This helps prevent context leaks.
        """
        
        context = await self.new_context(
            task_name = task_name,
            credential_id = credential_id,
            **kwargs
        )
        
        try:
            yield context
        finally:
            await self.close_context(context)
            
    async def close_context(self, context: BrowserContext):
        if context is None:
            return
        
        try:
            await asyncio.wait_for(
                context.close(),
                timeout = self._close_timeout,
            )
            
        except asyncio.TimeoutError:
            logger.warning(f"Browser - Timeout closing context.")
            
        except Exception as e:
            logger.debug(f"Browser - Ignored error closing context: {e}")
            
        finally:
            self._untrack_context(context)
            
        ### -------------------------------------- internal lifecycle helpers --------------------------------------
        
    async def _cleanup_failed_start(self):
        try:
            async with self._context_lock:
                await self._close_all_contexts()
        finally:
            await self._close_browser_only()
            await self._stop_playwright_only()
                
    async def _close_browser_only(self):
        if self._browser is None:
            return
        
        try:
            await asyncio.wait_for(
                self._browser.close(),
                timeout = self._close_timeout
            )
            
        except asyncio.TimeoutError:
            logger.warning(f"Browser - Timeout closing browser.")

        except Exception as exc:
            logger.warning(f"Browser - Error closing browser: {exc}")

        finally:
            self._browser = None
                
    async def _stop_playwright_only(self):
        if self._playwright is None:
            return
        
        try:
            await asyncio.wait_for(
                self._playwright.stop(),
                timeout = self._close_timeout
            )
            
        except asyncio.TimeoutError:
            logger.warning(f"Browser - Timeout stopping Playwright.")

        except Exception as exc:
            logger.warning(f"Browser - Error stopping Playwright: {exc}")

        finally:
            self._playwright = None
            
    async def _launch_browser(self) -> Browser:
        """ 
        Launch Chromium browser
        
        Browser is shared inside the worker/process.
        Cookies/sessions are not shared because each task should create its own BrowserContext.
        """
        
        if self._playwright is None:
            raise BrowserNotStartedError(
                "Playwright is not started. "
                "Call `await browser_manager.start()` first."
            )
            
        launch_args = self._browser_cfg.get("launch_args", [])
        
        headless = self._browser_cfg.get("HEADLESS")
        if headless is None:
            headless = not self.is_dev_env
            
        try:
            return await self._playwright.chromium.launch(
                headless = headless,
                args = launch_args,
            )
            
        except Exception as exc:
            raise BrowserStartError(
                f"Browser failed to launch: {exc}"
            )
            
    ### -------------------------------------- internal context helpers --------------------------------------
    
    async def _acquire_context_slot(self):
        """ 
        Wait for an available context slot.

        This provides backpressure instead of immediately rejecting tasks when
        all context slots are busy.
        """
        
        try:
            await asyncio.wait_for(
                self._context_semaphore.acquire(),
                timeout = self._context_acquire_timeout
            )
            
        except asyncio.TimeoutError as e:
            raise BrowserContextAcquireTimeoutError(
                f"Browser - Timeout waiting for available context slot "
                f"({len(self._contexts)}/{self._max_contexts})."
            ) from e
            
    def _release_context_slot_safely(self):
        """ 
        Release one context slot safely.

        This should only be called when a tracked/acquired context slot is being
        released. `_untrack_context()` is idempotent and prevents double release.
        """
        
        try:
            self._context_semaphore.release()
        except ValueError:
            logger.warning("Browser - Context semaphore release failed.")
            
    async def _close_all_contexts(self):
        contexts = list(self._contexts.keys())
        
        for c in contexts:
            await self.close_context(c)
            
        self._contexts.clear()
        
    def _track_context(
        self,
        *,
        context: BrowserContext,
        task_name: Optional[str] = None,
        credential_id: Optional[str] = None,
    ):
        """ 
        Track a context and attach metadata for debugging/tracking.
        """
        
        self._contexts[context] = ContextInfo(
            context = context,
            task_id = task_name,
            credential_id = credential_id
        )
        
        try:
            context.on("close", lambda *args: self._untrack_context(context))
        except Exception as e:
            logger.debug(f"Browser - Could not register context close hook: {e}")
            
    def _untrack_context(self, context: BrowserContext):
        """
        Remove a context from tracking.

        Returns:
            True if the context was tracked and removed.
            False if it was already removed.

        This method is intentionally idempotent to avoid double semaphore
        release when both `context.close()` and Playwright's close event fire.
        """
        
        existed = context in self._contexts
        if existed:
            self._contexts.pop(context, None)
            self._release_context_slot_safely()
            
        return existed
    
    def _discard_closed_contexts_best_effort(self):
        
        for c in list(self._contexts.keys()):
            try:
                _ = c.pages
            except Exception:
                self._untrack_context(c)
                
    def _build_context_options(self, **kwargs: Any) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "viewport": self._browser_cfg.get(
                "VIEWPORT",
                {"width": 1280, "height": 800},
            ),
            "ignore_https_errors": self._browser_cfg.get(
                "IGNORE_HTTPS_ERRORS",
                True,
            ),
            "accept_downloads": self._browser_cfg.get(
                "ACCEPT_DOWNLOADS",
                True,
            ),
            "locale": self._browser_cfg.get(
                "LOCALE",
                "vi-VN",
            ),
            "timezone_id": self._browser_cfg.get(
                "TIMEZONE_ID",
                "Asia/Ho_Chi_Minh",
            ),
        }

        user_agent = self._browser_cfg.get("USER_AGENT")
        if user_agent:
            defaults["user_agent"] = user_agent

        return {**defaults, **kwargs}