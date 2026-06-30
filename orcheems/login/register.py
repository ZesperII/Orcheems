from __future__ import annotations
from typing import Dict, Optional, Type, TYPE_CHECKING

from .base import BaseLoginService
from .schema import Credential
from ..browser import BrowserManager
from ..storage import BaseStateStorage

class SiteLoginServiceRegister:
    """ 
    Quản lý đăng ký các dịch vụ login theo site.
    
    Cung cấp method đăng kí dịch vụ login (decorator) và method khởi tạo LoginService instance từ Credential.
    """
    
    _registry: Dict[str, Type[BaseLoginService]] = {}
    
    @classmethod
    def register(cls, service_cls: Type[BaseLoginService]) -> Type[BaseLoginService]:
        site = getattr(service_cls, "SITE", None)
        
        if not site or not site.strip():
            raise TypeError(
                f"Cannot register {service_cls.__name__!r}: "
                f"missing or empty class variable SITE."
            )
            
        if site in cls._registry and cls._registry[site] is not service_cls:
            raise ValueError(
                f"Site {site!r} is already registered by "
                f"{cls._registry[site].__name__!r}. "
                f"Cannot re-register with {service_cls.__name__!r}."
            )
            
        cls._registry[site] = service_cls
        return service_cls

    @classmethod
    def get(cls, site: str) -> Type[BaseLoginService]:
        
        if site not in cls._registry:
            available = ", ".join(sorted(cls._registry)) or "(none)"
            raise KeyError(
                f"Login service for site {site!r} not found. "
                f"Available: {available}"
            )
        return cls._registry[site]
    
    @classmethod
    def from_credential(
        cls,
        credential: Credential,
        browser_manager: Optional[BrowserManager] = None,
        state_storage: Optional[BaseStateStorage] = None,
    ) -> BaseLoginService:
        """ 
        Khởi tạo LoginService instance từ Credential.
        site được lấy từ trường Credential.site → tự động map sang đúng LoginService đã đăng ký.
        
        Nếu không truyền browser_manager / state_storage thì dùng default
        của từng LoginService (thường là BrowserManager() và LocalStateStorage).
        
        Usage:
        
            service = SiteLoginServiceRegister.from_credential(credential)
            context, page, result = await service.login(credential)
        """
        
        service_cls = cls.get(credential.site)
        
        kwargs = {}
        
        if browser_manager is not None:
            kwargs["browser_manager"] = browser_manager
        if state_storage is not None:
            kwargs["state_storage"] = state_storage
            
        return service_cls(**kwargs)