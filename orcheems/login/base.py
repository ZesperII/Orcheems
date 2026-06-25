from __future__ import annotations

import asyncio
import logging

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, ClassVar, Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page

from .schema import Credential, LoginResult
from ..browser import BrowserManager
from ..storage import LocalStateStorage, BaseStateStorage

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Setup helpers
# -----------------------------------------------------------------------------

CookieIncompleteCallback = Callable[
    [BrowserContext, Page, Credential],
    Awaitable[Optional[Page]],
]

_DEFAULT_STATE_STORAGE: Any = object()


def cookie_incomplete_handler(fn: CookieIncompleteCallback) -> CookieIncompleteCallback:
    """Mark a method as the cookie-incomplete recovery handler."""
    setattr(fn, "_is_cookie_incomplete_handler", True)
    return fn


class BaseLoginService(ABC):
    """
    Base class for site-specific login services.

    Each concrete subclass must define the `SITE` class variable:

        class WFXLoginService(BaseLoginService):
            SITE = "wfx"

            def __init__(
                self,
                browser_manager: BrowserManager,
                state_storage: Optional[BaseStateStorage] = None,
            ) -> None:
                super().__init__(
                    browser_manager=browser_manager,
                    state_storage=state_storage,
                )

    `base_url` is provided by `Credential`. This allows different credentials
    for the same site to use different entry points if needed, such as a login
    page, admin page, or tenant-specific URL.

    ID conventions:
        - `credential_id` identifies the account for the target site. It is
          normally provided by the caller through `Credential` after discovery
          or registration.
        - `session_id` should be handled by the session layer, usually as a
          compound key such as `{site}:{credential_id}@{hostname}`.

    Login flow:
        1. `login()` validates the credential base URL.
        2. If `using_state=True`, it tries to restore a saved browser state
           with `bypass_login_by_using_cookie()`.
        3. If the restored state is missing or invalid, it performs a real
           login with `do_trigger_login()`.
        4. `do_trigger_login()` retries `_perform_login()` on the same browser
           context and page, then saves the browser state after a successful
           login.

    Subclasses must implement:
        - `_perform_login()` for site-specific login steps.
        - `_is_session_valid()` for site-specific session validation.

    A subclass can also define a method decorated with
    `@cookie_incomplete_handler` to recover from partially valid saved cookies,
    such as cookies that require OTP, captcha, or another verification step.
    """

    SITE: ClassVar[str]

    _NON_RETRYABLE_ERRORS: ClassVar[Tuple[str, ...]] = (
        "Too many concurrent contexts",
        "Browser has not been started",
        "Failed to start browser",
        "Target page, context or browser has been closed",
    )

    # -------------------------------------------------------------------------
    # Class setup and validation
    # -------------------------------------------------------------------------

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Validate concrete subclasses at import time."""
        super().__init_subclass__(**kwargs)

        # Skip intermediate abstract subclasses.
        if getattr(cls, "__abstractmethods__", frozenset()):
            return

        if not hasattr(cls, "SITE"):
            raise TypeError(
                f"Class '{cls.__name__}' must define the SITE class variable. "
                f"Example:\n"
                f"    class {cls.__name__}(BaseLoginService):\n"
                f"        SITE = \"{cls.__name__.lower().replace('loginservice', '')}\""
            )

        if not cls.SITE or not cls.SITE.strip():
            raise TypeError(
                f"Class '{cls.__name__}.SITE' must be a non-empty string, "
                f"got: {cls.SITE!r}"
            )

    # -------------------------------------------------------------------------
    # Static helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """
        Return whether an exception is worth retrying immediately.

        Infrastructure-level failures, such as browser crashes or context-limit
        errors, are treated as non-retryable because another immediate attempt
        will likely fail in the same way. Transient failures, such as network
        errors, wrong captcha values, or temporary page issues, are retryable.
        """
        msg = str(exc)
        return not any(marker in msg for marker in BaseLoginService._NON_RETRYABLE_ERRORS)

    @staticmethod
    async def _safe_close_context(
        browser_manager: BrowserManager,
        context: Optional[BrowserContext],
    ) -> None:
        
        """Close a browser context without leaking exceptions to the caller."""
        
        if context is None:
            return

        try:
            await browser_manager.close_context(context)
        except Exception as exc:
            logger.debug(f"Ignored error while closing context: {exc}")

    @staticmethod
    def _parse_base_url(base_url: str) -> str:
        
        """Validate and normalize a credential base URL."""
        
        if not base_url or not base_url.strip():
            raise ValueError(f"credential.base_url must be non-empty, got: {base_url!r}")

        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"credential.base_url must start with http:// or https://, got: {base_url!r}"
            )

        return base_url.rstrip("/")

    # -------------------------------------------------------------------------
    # Constructor
    # -------------------------------------------------------------------------

    def __init__(
        self,
        browser_manager: Optional[BrowserManager] = None,
        state_storage: Any = _DEFAULT_STATE_STORAGE,
    ) -> None:
        
        """
        Initialize the login service.

        Args:
            browser_manager: Browser lifecycle manager. If omitted, a new
                `BrowserManager` instance is created.
            state_storage: Storage backend for Playwright storage state.
                If omitted, `LocalStateStorage(".cookies")` is used.
                Pass `None` explicitly to disable state persistence.
        """
        
        self.browser_manager = browser_manager or BrowserManager()

        if state_storage is _DEFAULT_STATE_STORAGE:
            self.state_storage: Optional[BaseStateStorage] = LocalStateStorage(".cookies")
        else:
            self.state_storage = state_storage

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def login(
        self,
        credential: Credential,
        max_attempts: int = 3,
        retry_delay: float = 2.0,
        using_state: bool = False,
        on_cookie_incomplete: Optional[CookieIncompleteCallback] = None,
    ) -> Tuple[Optional[BrowserContext], Optional[Page], LoginResult]:
        """
        
        Log in to the target site and return the active browser resources.

        Flow:
            1. Validate `credential.base_url`.
            2. If `using_state=True`, try to bypass login with a saved
               Playwright storage state.
            3. If the saved state is missing, expired, or incomplete, perform a
               real login with retry.
            4. Return `(context, page, result)` without raising expected login
               failures to the caller.

        Args:
            credential: Login credential and target base URL.
            max_attempts: Maximum number of `_perform_login()` attempts.
            retry_delay: Delay in seconds between retryable attempts.
            using_state: Whether to try saved-state login before real login.
            on_cookie_incomplete: Optional callback used when saved cookies are
                loaded but the session is not valid yet.

        Returns:
            A tuple of `(context, page, LoginResult)`.

            On success:
                - `context` and `page` are valid Playwright objects.
                - `result.success` is `True`.

            On failure:
                - `context` and `page` are `None`.
                - `result.success` is `False`.

        Notes:
            Callers should check `result.success` before using `context` or
            `page`. Expected login failures are converted to `LoginResult`.
        """
        
        try:
            self._parse_base_url(credential.base_url)
        except ValueError as exc:
            return None, None, LoginResult(
                success         = False,
                credential_id   = credential.credential_id,
                site            = self.SITE,
                base_url        = credential.base_url,
                error           = str(exc),
            )

        if using_state and self.state_storage is not None:
            try:
                bypass_result = await self.bypass_login_by_using_cookie(
                    credential = credential,
                    on_cookie_incomplete = on_cookie_incomplete,
                )
                if bypass_result is not None:
                    return bypass_result
            except Exception as exc:
                logger.warning(
                    f"[{self.SITE}] Cookie bypass failed. Falling back to real login: {exc}"
                )

        try:
            return await self.do_trigger_login(
                credential   = credential,
                max_attempts = max_attempts,
                retry_delay  = retry_delay,
            )
        except Exception as exc:
            error_msg = str(exc)
            logger.error(f"[{self.SITE}] Login failed: {error_msg}")

            return None, None, LoginResult(
                success         = False,
                credential_id   = credential.credential_id,
                site            = self.SITE,
                base_url        = credential.base_url,
                metadata        = {"max_attempts": max_attempts},
                error           = error_msg,
            )

    # -------------------------------------------------------------------------
    # Internal flow methods
    # -------------------------------------------------------------------------

    async def bypass_login_by_using_cookie(
        self,
        credential: Credential,
        on_cookie_incomplete: Optional[CookieIncompleteCallback] = None,
    ) -> Optional[Tuple[BrowserContext, Page, LoginResult]]:
        
        """
        Try to bypass real login by restoring a saved Playwright storage state.

        Args:
            credential: Login credential used to locate the saved state.
            on_cookie_incomplete: Optional callback used when the cookie state
                exists but does not yet produce a valid session.

        Returns:
            `(context, page, LoginResult)` if the saved state is valid or can be
            recovered by the cookie-incomplete handler. Returns `None` when no
            state exists or when the state is stale.

        Raises:
            Unexpected exceptions are propagated to `login()`, where they are
            logged before falling back to real login.
        """
        
        if self.state_storage is None:
            return None

        state = await self.state_storage.load(self.SITE, credential.credential_id)
        if not state:
            return None

        context = await self.browser_manager.new_context(storage_state=state)

        try:
            page = await context.new_page()
            await page.goto(credential.base_url, wait_until="domcontentloaded")

            if await self._is_session_valid(page):
                return context, page, LoginResult(
                    success         = True,
                    credential_id   = credential.credential_id,
                    site            = self.SITE,
                    base_url        = credential.base_url,
                    metadata        = {"using_cookie": True},
                )

            handler = on_cookie_incomplete or self._resolve_cookie_incomplete_handler()
            if handler is not None:
                logger.debug(
                    f"[{self.SITE}] Cookie state loaded but session is invalid. "
                    f"Invoking cookie-incomplete handler."
                )

                recovered_page = await handler(context, page, credential)

                if recovered_page is not None and await self._is_session_valid(recovered_page):
                    return context, recovered_page, LoginResult(
                        success         = True,
                        credential_id   = credential.credential_id,
                        site            = self.SITE,
                        base_url        = credential.base_url,
                        metadata        = {
                            "using_cookie": True,
                            "cookie_incomplete_handled": True,
                        },
                    )

                logger.debug(
                    f"[{self.SITE}] Cookie-incomplete handler could not validate "
                    f"the session. Falling back to real login."
                )
            else:
                logger.debug(
                    f"[{self.SITE}] Cookie state loaded but session is invalid, "
                    f"and no cookie-incomplete handler is available."
                )

        except Exception:
            await self._safe_close_context(self.browser_manager, context)
            raise

        try:
            await self.state_storage.delete(self.SITE, credential.credential_id)
            logger.debug(
                f"[{self.SITE}] Stale cookie state cleared for "
                f"credential_id={credential.credential_id}"
            )
        except Exception as exc:
            logger.warning(f"[{self.SITE}] Failed to clear stale cookie state: {exc}")

        await self._safe_close_context(self.browser_manager, context)
        return None

    def _resolve_cookie_incomplete_handler(self) -> Optional[CookieIncompleteCallback]:
        """Return the first method marked with `@cookie_incomplete_handler`."""
        for cls in type(self).__mro__:
            for attr in vars(cls).values():
                if callable(attr) and getattr(attr, "_is_cookie_incomplete_handler", False):
                    return getattr(self, attr.__name__)
        return None

    async def do_trigger_login(
        self,
        credential: Credential,
        max_attempts: int = 3,
        retry_delay: float = 2.0,
    ) -> Tuple[BrowserContext, Page, LoginResult]:
        
        """
        Perform a real login with retry on the same browser context and page.

        The browser context and page are created once. `_perform_login()` is
        retried on that same page until the login succeeds, the retry budget is
        exhausted, or a non-retryable error is raised.

        This is useful for login flows that involve captcha, OTP, or temporary
        page errors because cookies and intermediate browser state are preserved
        across attempts.

        Args:
            credential: Login credential and target base URL.
            max_attempts: Maximum number of login attempts. Must be at least 1.
            retry_delay: Delay in seconds between retryable attempts.

        Returns:
            `(context, page, LoginResult)` after a successful login.

        Raises:
            ValueError: If `max_attempts` is less than 1.
            RuntimeError: If all attempts fail or a non-retryable error occurs.

        Lifecycle:
            On success, the caller owns the returned context and must close it
            later. On failure, this method closes the context before raising.
        """
        
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        context: Optional[BrowserContext] = None
        page: Optional[Page] = None
        last_error: Optional[Exception] = None

        try:
            context = await self.browser_manager.new_context()
            page = await context.new_page()

            for attempt in range(1, max_attempts + 1):
                try:
                    active_page = await self._perform_login(page, context, credential)
                    page = active_page or page

                    if not await self._is_session_valid(page):
                        raise RuntimeError(
                            f"[{self.SITE}] Session is invalid after attempt "
                            f"{attempt}/{max_attempts}"
                        )

                    login_result = LoginResult(
                        success         = True,
                        credential_id   = credential.credential_id,
                        site            = self.SITE,
                        base_url        = credential.base_url,
                        metadata        = {
                            "using_cookie": False,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                        },
                    )

                    if self.state_storage is not None:
                        try:
                            state = await context.storage_state()
                            await self.state_storage.save(
                                self.SITE,
                                credential.credential_id,
                                state,
                            )
                        except Exception as exc:
                            logger.warning(
                                f"[{self.SITE}] Failed to save state after successful login "
                                f"for credential_id={credential.credential_id}: {exc}"
                            )

                    return context, page, login_result

                except Exception as exc:
                    last_error = exc

                    if not self._is_retryable(exc):
                        logger.error(
                            f"[{self.SITE}] Non-retryable login error on attempt "
                            f"{attempt}/{max_attempts}: {exc}"
                        )
                        break

                    logger.warning(
                        f"[{self.SITE}] Login attempt {attempt}/{max_attempts} failed: {exc}"
                    )

                    if attempt < max_attempts:
                        await asyncio.sleep(retry_delay)

            raise RuntimeError(
                f"[{self.SITE}] Login failed after {max_attempts} attempt(s). "
                f"Last error: {last_error}"
            )

        except Exception:
            await self._safe_close_context(self.browser_manager, context)
            raise

    # -------------------------------------------------------------------------
    # Abstract methods
    # -------------------------------------------------------------------------

    @abstractmethod
    async def _perform_login(
        self,
        page: Page,
        context: BrowserContext,
        credential: Credential,
    ) -> Page:
        
        """
        Execute the site-specific login steps.

        This method is called by `do_trigger_login()` on each attempt. The same
        browser context and page are reused across retry attempts, so subclasses
        should reset the page state as needed before filling the login form.

        Recommended pattern:

            async def _perform_login(
                self,
                page: Page,
                context: BrowserContext,
                credential: Credential,
            ) -> Page:
                await page.goto(credential.base_url, wait_until="domcontentloaded")
                await page.fill("#username", credential.username)
                await page.fill("#password", credential.password)
                captcha = self.ocr.classification(
                    await page.locator(".captcha").screenshot()
                )
                await page.fill("#captcha", captcha)
                await page.click("#submit")
                return page

        Args:
            page: Current Playwright page reused across attempts.
            context: Browser context reused across attempts.
            credential: Login credential and target base URL.

        Returns:
            The active page after submitting the login form. Usually this is the
            same `page` argument, but a subclass may return a new page if the
            login flow opens one.

        Raises:
            Any exception that represents a failed login attempt. The context
            lifecycle is handled by `do_trigger_login()`.
        """
        ...

    @abstractmethod
    async def _is_session_valid(self, page: Page) -> bool:
        """
        Check whether the current page represents a valid authenticated session.

        Subclasses must implement site-specific validation, such as checking for
        a logout button, account indicator, dashboard URL, or authenticated API
        response. A subclass may call `super()._is_session_valid(page)` to reuse
        the basic URL-based fallback below.

        Args:
            page: Playwright page to validate.

        Returns:
            `True` if the session is authenticated and still valid; otherwise
            `False`.

        Notes:
            Prefer returning `False` over raising. A `False` result allows the
            login flow to re-login or retry cleanly.
        """
        try:
            return "login" not in page.url.lower()
        except Exception:
            return False
