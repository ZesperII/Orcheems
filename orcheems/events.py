from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

Emitter = Callable[[str, Any], Awaitable[None]]

@dataclass
class SseEvent:
    type: str
    data: Any = None

    def encode(self) -> str:
        payload = json.dumps({"type": self.type, "data": self.data}, ensure_ascii=False)
        return f"data: {payload}\n\n"

async def safe_emit(emitter: Optional[Emitter], event_type: str, data: Any = None) -> None:
    """Gọi emitter nếu có; no-op nếu None hoặc emit raise."""
    if emitter is None:
        return
    try:
        await emitter(event_type, data)
    except Exception:
        pass