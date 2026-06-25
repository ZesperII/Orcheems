from __future__ import annotations

import os
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, BrowserContext, Playwright
from typing import Any, Optional, Set

from .config import BROWSER

load_dotenv()


class BrowserManager:
    """ 
    Managing the Playwright browser lifecycle.
    
    Design Principles:
        - Browser can be shared within a worker/process to save resources.
        - BrowserContext is NOT shared between users/tasks/sessiones.
        - Pages are NOT shared between users/tasks/sessions.
        - A new context should be created using `new_context()` each time a task runs.
        - If the old session needs to be loaded, pass `storage_state`.
        - After the task is complete, save the storage_state if necessary and close the context.
        
    Recommendations:
        BrowserManager
                └── Browser singleton for worker
                        ├── BrowserContext separated for task A
                        ├── BrowserContext separated for task B
                        └── BrowserContext separated for task C
    """
    
    def __init__(self, max_concurrent_contexts: int = 50):
        self._browser_cfg: dict[str, Any] = BROWSER
        
        # Playwright runtime
        # Only start 1 time on BrowserManager lifecycle
        self._playwright: Optional[Playwright] = None
        
        # Browser Instance
        self._browser: Optional[Browser] = None
        
        # track active contexts for cleanup
        self._contexts: Set[BrowserContext] = set()
        self._max_contexts = max_concurrent_contexts
        
    @property
    def is_dev_env(self) -> bool:
        return os.getenv("APP_ENV", "DEV") == "DEV"
        
    @property
    def is_started(self):
        """ 
        Check if Instance is already started or not.
        """
        return self._browser is not None and self._browser.is_connected()
    
    @property
    def browser(self) -> Browser:
        """ 
        return current browser
        
        Only property, no setter, to ensure the lifecycle is managed by BrowserManager.
        """
        
        if not self._browser or not self._browser.is_connected():
            raise RuntimeError(
                "Browser has not been started. Call `await browser_manager.start()` first."
            )
            
        return self._browser
    
    async def start(self):
        """ 
        Start Playwright and launch the browser.
        
        If browser is already started, do nothing. Avoid calling start() multiple times.
        """
        if self.is_started:
            return
        
        try:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
                
            self._browser = await self._launch_browser()
        except Exception as e:
            raise RuntimeError(f"Failed to start browser: {e}") from e
        
    async def close(self):
        """ 
        Close browser and stop Playwright runtime.
        
        Should be called when shutdown worker/app.
        Not necessary to call after each task, as browser can be reused.
        """
        # Close all active contexts first
        for context in list(self._contexts):
            await self.close_context(context)
        
        self._contexts.clear()
        
        # Then close browser
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as e:
                print(f"Warning: Error closing browser: {e}")
            finally:
                self._browser = None
            
        # Finally stop playwright
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                print(f"Warning: Error stopping playwright: {e}")
            finally:
                self._playwright = None
            
    async def __aenter__(self):
        """ 
        Allow using:
        
            async with BrowserManager() as browser_manager:
                ...
                
        When entering the block -> start the browser.
        """
        await self.start()
        return self
    
    async def __aexit__(self, *args: object):
        """ 
        When exiting the block -> close the browser.
        """
        await self.close()
        
    async def _launch_browser(self) -> Browser:
        """ 
        Launch Chromium browser.
        
        Note:
            - Shared worker/process
            - Not shared cookie/session 
            - Each session will create/use a BrowserContext, which is separated from each other.
        """
        if self._playwright is None:
            raise RuntimeError("Playwright is not started. Call `await browser_manager.start()` first.")
        
        launch_args = self._browser_cfg.get("launch_args", [])
        
        # If HEADLESS is set in config, use it.
        # If not, DEV will show browser (headless=False), other environments run headless.
        headless = self._browser_cfg.get("HEADLESS")
        if headless is None:
            headless = not self.is_dev_env
        
        try:
            return await self._playwright.chromium.launch(
                headless=headless,
                args=launch_args,
            )
        except Exception as e:
            raise RuntimeError(f"Failed to launch browser: {e}") from e
        
    async def new_context(self, **kwargs: Any) -> BrowserContext:
        """ 
        Create a new BrowserContext.

        This is the most important point for session isolation.

        Each new context will have its own environment:
        - its own cookie
        - its own localStorage
        - its own sessionStorage
        - its own permissions
        - its own context-specific cache
        - its own pages

        If you want to load a saved session, pass:

            context = await browser_manager.new_context(
                storage_state="sessions/vnpt/user_001.json"
            )

        Do not share a context between multiple tasks/users.
        """
        if not self.is_started:
            await self.start()
        
        # Check concurrency limit
        if len(self._contexts) >= self._max_contexts:
            raise RuntimeError(
                f"Too many concurrent contexts: {len(self._contexts)}/{self._max_contexts}"
            )
        
        # Default params for browser context
        defaults: dict[str, Any] = {
            "viewport": {"width": 1280, "height": 800},
            "ignore_https_errors": True,
            "accept_downloads": True,
        }
        
        user_agent = self._browser_cfg.get("USER_AGENT")
        if user_agent:
            defaults["user_agent"] = user_agent
            
        context_options = {**defaults, **kwargs}
        
        try:
            context = await self.browser.new_context(**context_options)
            self._contexts.add(context)
            return context
        except Exception as e:
            raise RuntimeError(f"Failed to create new context: {e}") from e
    
    async def close_context(self, context: BrowserContext):
        """ 
        Close a specific BrowserContext and remove it from tracking.
        
        Should be called immediately after a task/session is complete 
        to free up concurrent slots and memory.
        """
        
        if not context:
            return
        
        try:
            await context.close()
        except Exception as e:
            print(f"Warning: Error closing context: {e}")
        finally:
            self._contexts.discard(context)