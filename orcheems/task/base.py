from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from playwright.async_api import Page
from typing import Any, Optional, TYPE_CHECKING, Callable, Awaitable
from abc import ABC, abstractmethod

from ..events import SseEvent

if TYPE_CHECKING:
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
        
    # ------------------------------------------------------------------
    # Helpers for task implementations
    # ------------------------------------------------------------------
        
    async def with_page(
        self,
        credential  : Credential,
        work        : Callable[[Page], Awaitable[Any]],
        using_state : bool          = False,
        keep_alive  : bool          = False,
        ttl_seconds : Optional[int] = None,
    ) -> Any:
        
        """
        Run `work(page)` in a pre-authenticated session (page is at base_url, already authenticated).

        SessionManager handles: login (with cookie bypass + retry), lock session
        while `work` is running, and unregister/close context when done.

        Args:
            credential : Credential used for login.
            work       : Async function that receives `page` and returns the task result.
            using_state: If True, the session will use the saved state.
            keep_alive : If True, the session will not be unregistered after `work` completes.
            ttl_seconds: If set, the session will remain alive for this duration (in seconds).

        Raises:
            RuntimeError: if the task has not been registered via AppOperator.register_task()
                          (session_manager not bound).
        """
        self._assert_bound()
        return await self._session_manager.run_session(
            credential,
            task_name   = self.__class__.__name__,
            callback    = lambda resources: work(resources.page),
            using_state = using_state,
            keep_alive  = keep_alive,
            ttl_seconds = ttl_seconds
        )

    def with_page_stream(
        self,
        credential  : Credential,
        work        : Callable[[Page, Emitter], Awaitable[Any]],
        using_state : bool          = False,
        keep_alive  : bool          = False,
        ttl_seconds : Optional[int] = None
    ) -> StreamingResponse:
        
        """
        Similar to with_page() but returns a StreamingResponse (SSE) immediately.

        `work` receives an additional `emit` parameter to push progress events to the client while running.
        The result returned by `work` is automatically sent as the final `done` event.

        Each request creates its own queue → different users are completely independent, running in parallel.

        Args:
            credential : Credential used for login.
            work       : Async callable receiving (page, emit). Call emit(type, data) to push events.
            using_state: If True, the session will use the saved state.
            keep_alive : If True, the session will not be closed after work completes.
            ttl_seconds: If set, the session will remain alive for this duration (in seconds).
        Usage:
            @router.post("/my-endpoint")
            async def my_endpoint(body: Body):
                async def work(page, emit):
                    await emit("progress", {"step": "crawling"})
                    result = await do_something(page)
                    return result

                return self.with_page_stream(body.credential, work, using_state=True)

        SSE events client receives:
            {"type": "progress", "data": {...}}        — from emit() inside work
            {"type": "done",     "data": <return>}     — return value of work
            {"type": "error",    "data": {"message"}}  — if an exception occurs
        """
        
        self._assert_bound()

        queue: asyncio.Queue = asyncio.Queue()

        async def emit(event_type: str, data: Any = None) -> None:
            await queue.put(SseEvent(type=event_type, data=data))

        async def _run() -> None:
            try:
                result = await self._session_manager.run_session(
                    credential  = credential,
                    task_name   = self.__class__.__name__,
                    callback    = lambda resources: work(resources.page, emit),
                    using_state = using_state,
                    keep_alive  = keep_alive,
                    ttl_seconds = ttl_seconds
                )
                await queue.put(SseEvent(type="done", data=result))
            except Exception as e:
                await queue.put(SseEvent(type="error", data={"message": str(e)}))
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

        return StreamingResponse(_stream(), media_type="text/event-stream")
    
    def _assert_bound(self) -> None:
        """Raise early if the task was not registered through Operator."""
        
        if self._session_manager is None:
            raise RuntimeError(
                f"{self.__class__.__name__} has no session_manager. "
                f"Register this task via AppOperator.register_task() before use."
            )