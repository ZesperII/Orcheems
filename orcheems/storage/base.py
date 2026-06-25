from typing import Any, Optional
from abc import ABC, abstractmethod

class BaseStateStorage(ABC):
    """ 
    Base storage interface Playwright storage_state (cookies + localStorage). 

    implement: 
        LocalStateStorage - Save files locally 
        RedisStateStorage - Save on Redis
    """
    
    @abstractmethod
    async def save(
        self,
        site: str,
        credential_id: str,
        state: dict[str, Any]
    ):
        """Save storage_state, overwrite if it already exists."""
        ...
        
    @abstractmethod
    async def load(
        self,
        site: str,
        credential_id: str
    ) -> Optional[dict[str, Any]]:
        """Load storage_state. Return None if it does not exist or is corrupt."""
        ...
        
    @abstractmethod
    async def delete(
        self,
        site: str,
        credential_id: str
    ) -> bool:
        """Delete state. Return True if the file exists and has been deleted."""
        ...
        
    @abstractmethod
    async def exists(
        self,
        site: str,
        credential_id: str
    ) -> bool:
        """Check if the state exists."""
        ...
        
    @abstractmethod
    async def list_credentials(
        self,
        site: str
    ) -> list[str]:
        """List all credential_id that have state for the site."""
        ...
        
    @staticmethod
    def _sanitize(name: str) -> str:
        """Remove unsafe characters from file/directory names."""
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)