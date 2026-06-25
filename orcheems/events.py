from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class SseEvent:
    type: str
    data: Any = None

    def encode(self) -> str:
        payload = json.dumps({"type": self.type, "data": self.data}, ensure_ascii=False)
        return f"data: {payload}\n\n"