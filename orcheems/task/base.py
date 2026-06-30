from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from playwright.async_api import Page, BrowserContext
from typing import Any, Optional, TYPE_CHECKING, Callable, Awaitable
from abc import ABC, abstractmethod
from contextlib import suppress

from ..events import SseEvent
from ..session.schema import SessionResources

from ..session import SessionManager
from ..login import Credential


Emitter = Callable[[str, Any], Awaitable[None]]



class BaseTask(ABC):
    """
    Abstract base for all application tasks.

    Subclasses declare their API endpoints in register_route() and use
    with_page() / with_page_stream() to run work inside a managed session.

    Everything else — login, locking, retries, context cleanup — is handled
    by SessionManager and AppOperator. Tasks never touch browser lifecycle.

    Usage:
    
        class InvoiceDownloadTask(BaseTask):

            async def on_startup(self):
                self.ocr = ddddocr.DdddOcr()

            def register_route(self, router: APIRouter):

                class Body(BaseModel):
                    credential: Credential
                    invoice_id: str

                @router.post("/download")
                async def download(body: Body):
                    return await self.with_page(
                        body.credential,
                        lambda page: self._do_download(page, body.invoice_id),
                    )
    """
    
    _session_manager: Optional[SessionManager] = None
    
    @abstractmethod
    def register_route(self, router: APIRouter) -> None:
        """
        Declare all task endpoints to the router.

        Args:

            router: APIRouter created and passed in by AppOperator.
                    Can be shared with other tasks with the same prefix.

        Notes:
            Use `self.with_page(...)` for regular tasks.
            Use `self.with_page_stream(...)` to return a direct SSE stream.
        """
        ...
    
    async def on_startup(self) -> None:
        """
        Hook to initialize resources when the app starts.
        Override if the task needs setup (OCR engine, HTTP client, DB connection...).
        """
 
    async def on_shutdown(self) -> None:
        """
        Hook to cleanup resources when the app shuts down.
        Override if the task needs teardown.
        """
        
    def _bind_session_manager(self, session_manager: SessionManager) -> None:
        """Called by AppOperator.register_task(). Do not call directly."""
        self._session_manager = session_manager
        
    def _assert_bound(self) -> None:
        """Raise early if the task was not registered through Operator."""
        
        if self._session_manager is None:
            raise RuntimeError(
                f"{self.__class__.__name__} has no session_manager. "
                f"Register this task via AppOperator.register_task() before use."
            )
        
    # ------------------------------------------------------------------
    # Helpers for task implementations
    # ------------------------------------------------------------------
        
    def _stream_runner(
            self,
            runner: Callable[[Emitter], Awaitable[Any]],
        ) -> StreamingResponse:
            """
            Shared SSE wrapper.
    
            If the client disconnects, the background task is cancelled and awaited
            so SessionManager has a chance to run its cleanup finally-block.
            """
            queue: asyncio.Queue[Optional[SseEvent]] = asyncio.Queue()
    
            async def emit(event_type: str, data: Any = None) -> None:
                await queue.put(SseEvent(type=event_type, data=data))
    
            async def _run() -> None:
                try:
                    result = await runner(emit)
                    await queue.put(SseEvent(type="done", data=result))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await queue.put(SseEvent(type="error", data={"message": str(exc)}))
                finally:
                    await queue.put(None)
    
            async def _stream():
                bg = asyncio.create_task(_run())
    
                try:
                    while True:
                        event = await queue.get()
                        if event is None:
                            break
    
                        yield event.encode()
    
                finally:
                    if not bg.done():
                        bg.cancel()
                        with suppress(asyncio.CancelledError):
                            await bg
    
            return StreamingResponse(_stream(), media_type="text/event-stream")
    
    async def with_page(
        self,
        credential: Credential,
        work: Callable[[Page], Awaitable[Any]],
        using_state: bool = False,
        keep_alive: bool = False,
        ttl_seconds: Optional[int] = None,
    ) -> Any:
        """
        Run work(page) inside an authenticated session.

        The task receives only Page. SessionManager handles login, lock,
        release, TTL, and cleanup.
        """
        self._assert_bound()

        return await self._session_manager.run_session(
            credential      = credential,
            task_name       = self.__class__.__name__,
            callback        = lambda resources: work(resources.page),
            using_state     = using_state,
            keep_alive      = keep_alive,
            ttl_seconds     = ttl_seconds,
        )

    async def with_context(
        self,
        credential: Credential,
        work: Callable[[BrowserContext, Page], Awaitable[Any]],
        using_state: bool = False,
        keep_alive: bool = False,
        ttl_seconds: Optional[int] = None,
    ) -> Any:
        """
        Run work(context, page) inside an authenticated session.

        Use this when the task needs context-level APIs, popup handling,
        storage_state(), permissions, or multiple tabs.
        """
        self._assert_bound()

        return await self._session_manager.run_session(
            credential      = credential,
            task_name       = self.__class__.__name__,
            callback        = lambda resources: work(resources.context, resources.page),
            using_state     = using_state,
            keep_alive      = keep_alive,
            ttl_seconds     = ttl_seconds,
        )

    async def with_resources(
        self,
        credential: Credential,
        work: Callable[[SessionResources], Awaitable[Any]],
        using_state: bool = False,
        keep_alive: bool = False,
        ttl_seconds: Optional[int] = None,
    ) -> Any:
        """
        Run work(resources) with full SessionResources.

        Use this when the task needs context, page, login result, credential_id,
        base_url, or other session-level metadata.
        """
        self._assert_bound()

        return await self._session_manager.run_session(
            credential   = credential,
            task_name    = self.__class__.__name__,
            callback     = work,
            using_state  = using_state,
            keep_alive   = keep_alive,
            ttl_seconds  = ttl_seconds,
        )

    def with_page_stream(
        self,
        credential: Credential,
        work: Callable[[Page, Emitter], Awaitable[Any]],
        using_state: bool = False,
        keep_alive: bool = False,
        ttl_seconds: Optional[int] = None,
    ) -> StreamingResponse:
        """
        Streaming variant of with_page().

        work receives (page, emit). The emit function sends SSE progress events.
        Login progress events are streamed automatically before work() runs.
        """
        self._assert_bound()

        async def runner(emit: Emitter) -> Any:
            return await self._session_manager.run_session(
                credential   = credential,
                task_name    = self.__class__.__name__,
                callback     = lambda resources: work(resources.page, emit),
                using_state  = using_state,
                keep_alive   = keep_alive,
                ttl_seconds  = ttl_seconds,
                emitter      = emit,
            )

        return self._stream_runner(runner)

    def with_context_stream(
        self,
        credential: Credential,
        work: Callable[[BrowserContext, Page, Emitter], Awaitable[Any]],
        using_state: bool = False,
        keep_alive: bool = False,
        ttl_seconds: Optional[int] = None,
    ) -> StreamingResponse:
        """
        Streaming variant of with_context().

        work receives (context, page, emit). The emit function sends SSE progress events.
        Login progress events are streamed automatically before work() runs.
        """
        self._assert_bound()

        async def runner(emit: Emitter) -> Any:
            return await self._session_manager.run_session(
                credential   = credential,
                task_name    = self.__class__.__name__,
                callback     = lambda resources: work(resources.context, resources.page, emit),
                using_state  = using_state,
                keep_alive   = keep_alive,
                ttl_seconds  = ttl_seconds,
                emitter      = emit,
            )

        return self._stream_runner(runner)

    def with_resources_stream(
        self,
        credential: Credential,
        work: Callable[[SessionResources, Emitter], Awaitable[Any]],
        using_state: bool = False,
        keep_alive: bool = False,
        ttl_seconds: Optional[int] = None,
    ) -> StreamingResponse:
        """
        Streaming variant of with_resources().

        work receives (resources, emit). The emit function sends SSE progress events.
        Login progress events are streamed automatically before work() runs.
        """
        self._assert_bound()

        async def runner(emit: Emitter) -> Any:
            return await self._session_manager.run_session(
                credential   = credential,
                task_name    = self.__class__.__name__,
                callback     = lambda resources: work(resources, emit),
                using_state  = using_state,
                keep_alive   = keep_alive,
                ttl_seconds  = ttl_seconds,
                emitter      = emit,
            )

        return self._stream_runner(runner)