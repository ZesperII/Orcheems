import json
import asyncio
from pathlib import Path
from typing import Optional, Any

from .base import BaseStateStorage


class LocalStateStorage(BaseStateStorage):
    """ 
    Save storage_state to a local file.

    Layout:

        {base_dir}/{site}/{credential_id}.json

    Usage:

        Storage = LocalStateStorage(".cookies")
        Await storage.save("vnpt", "0000-0000", state_dict)
        State = Await storage.load("vnpt", "0000-0000")

    Note:
        File I/O runs in a thread pool (asyncio.to_thread) to avoid blocking
        the event loop when multiple logins occur simultaneously.
    """
    
    def __init__(self, base_dir: str = ".cookies"):
        self._base = Path(base_dir)
        
    def _path(self, site: str, credential_id: str) -> Path:
        return self._base / self._sanitize(site) / f"{self._sanitize(credential_id)}.json"
        
    async def save(self, site: str, credential_id: str, state: dict[str, Any]) -> None:
        path = self._path(site, credential_id)
        await asyncio.to_thread(self._save_sync, path, state)
        
    async def load(self, site: str, credential_id: str) -> Optional[dict[str, Any]]:
        path = self._path(site, credential_id)
        return await asyncio.to_thread(self._load_sync, path)
    
    async def delete(self, site: str, credential_id: str) -> bool:
        path = self._path(site, credential_id)
        return await asyncio.to_thread(self._delete_sync, path)
    
    async def exists(self, site: str, user_id: str) -> bool:
        path = self._path(site, user_id)
        return await asyncio.to_thread(path.exists)
    
    async def list_credentials(self, site: str) -> list[str]:
        site_dir = self._base / self._sanitize(site)
        return await asyncio.to_thread(self._list_credentials_sync, site_dir)
    
    ### sync workers
    
    def _save_sync(self, path: Path, state: dict[str, Any]) -> None:
        path.parent.mkdir(parents = True, exist_ok = True)
        tmp = path.with_suffix(".tmp")
        
        try:
            tmp.write_text(
                json.dumps(state, ensure_ascii = False, indent = 2),
                encoding = "utf-8",
            )
            
            tmp.replace(path)
            
        except Exception as e:
            tmp.unlink(missing_ok = True)
            raise IOError(f"Failed to save state {path}: {e}") from e
        
    def _load_sync(self, path: Path) -> Optional[dict[str, Any]]:
        if not path.exists():
            return None
        
        try:
            data = json.loads(path.read_text(encoding = "utf-8"))
            return data
        
        except Exception as e:
            path.unlink(missing_ok = True)
            return None
        
    def _delete_sync(self, path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        
    @staticmethod
    def _list_credentials_sync(site_dir: Path) -> list[str]:
        if not site_dir.exists():
            return []
        
        return [p.stem for p in site_dir.glob("*.json")]
    