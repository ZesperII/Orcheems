from __future__ import annotations

import logging

from dataclasses import dataclass
from typing import List, Optional, Sequence, Type

from .base import BaseTask

 
logger = logging.getLogger(__name__)

@dataclass
class _PendingRegistration:
    """ 
    Lưu thông tin đăng ký của 1 task class trước khi AppOperator được khởi tạo.
    """
    task_cls : Type[BaseTask]
    prefix   : str
    tags     : Optional[str]
    
_pending_registrations: List[_PendingRegistration] = []

def task_register(
    prefix: str = "",
    tags: Optional[Sequence[str]] = None,
) -> Type[BaseTask]:
    """ 
    Class decorator để đánh dấu 1 class BaseTask subclass được tự động đăng ký 
    vào AppOperator khi gọi `AppOperatorInstance.auto_register_tasks_and_build()`.
    
    Decorator chỉ lưu metadata (class, prefix, tags) - không instantiate task.
    Instance được khởi tạo tại thời điểm `auto_register_tasks_and_build()` được gọi.
    
    Usage:
    
        @task_register(prefix="/wfx", tags=["wfx"])
        class WFXDownloadTask(BaseTask):
            def register_route(self, router: APIRouter):
                ...
                
        operator = TaskServiceFastAPIAppOperator(state_storage=LocalCookieStore(".cookies"))
        app = operator.auto_register_tasks_and_build()
    """
    
    def decorator(cls: Type[BaseTask]) -> Type[BaseTask]:
        if not issubclass(cls, BaseTask):
            raise TypeError(f"@task_register can only be applied to BaseTask subclasses, got {cls!r}")
        
        resolved_tags = list(tags or [cls.__name__])
        _pending_registrations.append(
            _PendingRegistration(
                task_cls = cls,
                prefix   = prefix,
                tags     = resolved_tags
            )
        )
        # logger.debug(f"[Task-Auto-Register] '{cls.__name__}' → prefix='{prefix}' tags={resolved_tags}")
        return cls
    
    return decorator