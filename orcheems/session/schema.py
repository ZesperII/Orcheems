from __future__ import annotations

from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from ..login import LoginResult

from playwright.async_api import BrowserContext, Page


class SessionStatus(Enum):
    PENDING = auto()  # login in progress — slot reserved, not usable yet
    READY   = auto()  # idle, available for the next task
    LOCKED  = auto()  # task is running — no concurrent access allowed
    

@dataclass
class SessionEntry:
    """ 
    Session slot registry - gắn với 1 credential_id
    
    Fields:
    
        credential_id   : UUID v5 từ Credential - unique key
        status          : PENDING -> READY -> LOCKED -> READY
        context         : BrowserContext sau khi login thành công
        page            : Page sau khi login thành công
        result          : LoginResult trả về từ login()
        current_task    : Tên task đang chạy, None nếu READY
    """
    
    credential_id  : str
    status         : SessionStatus              = SessionStatus.PENDING
    context        : Optional[BrowserContext]   = None
    page           : Optional[Page]             = None
    result         : Optional[LoginResult]      = None
    current_task   : Optional[str]              = None
    ttl            : Optional[datetime]         = None
    
    
    
@dataclass
class SessionResources:
    """ 
    Read-only view of session resources handed to a task.
    Tasks use these directly - they never manage context/page lifecycle.
    """
    
    context      : BrowserContext
    page         : Page
    result       : LoginResult
    credential_id: str