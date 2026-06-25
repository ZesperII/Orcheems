from __future__ import annotations

import os, json, logging

from dotenv import load_dotenv
from redis.asyncio import Redis
from redis.exceptions import RedisError
from typing import Any, Optional
from .base import BaseStateStorage
load_dotenv()

logger = logging.getLogger(__name__)

class RedisStateStorage(BaseStateStorage):
    """
    Storing storage state in Redis.

    Key layout:

        {prefix}:{site}:{credential_id}

    `list_users` uses Redis's auxiliary SET to avoid running SCAN/KEYS (which blocks
    Redis servers and is unsafe in production):

        {prefix}:__index__:{site} -> SETs the user_ids that have state

    Usage:

        storage = RedisStateStorage("redis://localhost:6379/0")
        await storage.save("vnpt", "alice", state_dict)
        state = await storage.load("vnpt", "alice")
        await storage.close()  # closes the connection pool when shutting down

    Can be used as an async context manager:

        async with RedisStateStorage(url) as storage:
            await storage.save(...)

    Note:
        If you want the state to expire automatically (e.g., based on login sessions), pass `ttl_seconds`.
        TTL This only applies to the key containing the state, not to the index set — when
        the key state expires, the user_id will remain in the index until it is explicitly deleted (`delete()`) or cleared by `_prune_missing` in the `list_users`.
    """
    
    def __init__(
        self,
        url: str = os.getenv("REDIS_URL"),
        *,
        prefix: str = "state_storage",
        ttl_seconds: Optional[int] = None,
        redis_client: Optional[Redis] = None,
        **redis_kwargs: Any
    ): 
        """ 
        Args:

            url: Redis connection URL (omit if passing redis_client).
            prefix: Namespace prefix for all keys, to avoid conflicts with other keys 
                    on the same Redis instance.
            ttl_seconds: If set, each state will expire after N seconds from the last save. 
                    None = no expiration (default).
            redis_client: Allows injecting a `redis.asyncio.Redis` instance 
                    (e.g., to share a connection pool, or test with fakeredis).
            **redis_kwargs: Forward additional values ​​to `Redis.from_url` 
                    (e.g., password, socket_timeout, decode_responses should not be overridden — keep False, this class handles encoding/decoding automatically).
        """
        
        if redis_client is None and Redis is None:
            raise ImportError("redis.asyncio.Redis is required but not installed.")
        
        self._prefix = prefix
        self._ttl = ttl_seconds
        self._owns_client = redis_client is None
        self._redis: Redis = redis_client or Redis.from_url(
            url,
            decode_responses = False,  # lưu raw bytes, class này tự encode/decode JSON
            **redis_kwargs
        )
        
    ### Context manager
    
    async def __aenter__(self) -> RedisStateStorage:
        return self
    
    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()
        
    async def ping(self) -> None:
        try:
            await self._redis.ping()
        except RedisError as e:
            raise IOError(f"Redis ping failed: {e}") from e
        
    async def close(self) -> None:
        if self._owns_client:
            await self._redis.aclose()
            
    ### Internal
    
    async def _prune_missing(self, site: str, credential_ids: set[str]) -> None:
        """Remove `credential_id` from the index if the corresponding key state is no longer present."""
        
        if not credential_ids:
            return []
        
        keys = [self._key(site, cid) for cid in credential_ids]
        
        try:
            exists = await self._redis.mget(keys)
        except RedisError:
            return credential_ids
        
        stale = {cid for cid, val in zip(credential_ids, exists) if val is None}
        
        if stale:
            try:
                await self._redis.srem(self._index_key(site), *stale)
            except RedisError:
                pass
            
        return [cid for cid, val in zip(credential_ids, exists) if val is not None]
    
    def _key(self, site: str, credential_id: str) -> str:
        return f"{self._prefix}:{self._sanitize(site)}:{self._sanitize(credential_id)}"
    
    def _index_key(self, site: str) -> str:
        return f"{self._prefix}:__index__:{self._sanitize(site)}"
    
    ### Public API
    
    async def save(self, site: str, credential_id: str, state: dict[str, Any]) -> None:
        key = self._key(site, credential_id)
        payload = json.dumps(state, ensure_ascii = False).encode("utf-8")
        
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.set(key, payload, ex = self._ttl)
                pipe.sadd(self._index_key(site), credential_id)
                await pipe.execute()
            logger.debug(f"Saved state: {key} (TTL: {self._ttl}s)")
                
        except RedisError as e:
            raise IOError(f"Failed to save state for {site}:{credential_id}: {e}") from e
        
    async def load(self, site: str, credential_id: str) -> Optional[dict[str, Any]]:
        key = self._key(site, credential_id)
        
        try:
            raw = await self._redis.get(key)
            
        except RedisError as e:
            logger.warning(f"Redis error loading '{key}': {e}")
            return None
        
        if raw is None:
            return None
        
        try:
            data = json.loads(raw)
            logger.debug(f"Loaded state: {key}")
            return data
        
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"Corrupt state key '{key}', removing: {e}")
            await self.delete(site, credential_id)
            return None

    async def delete(self, site: str, credential_id: str) -> bool:
        key = self._key(site, credential_id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                pipe.srem(self._index_key(site), credential_id)
                deleted, _ = await pipe.execute()
            if deleted:
                logger.debug(f"Deleted state: {key}")
                
            return bool(deleted)
        
        except RedisError as e:
            raise IOError(f"Failed to delete state {key}: {e}") from e
        
    async def exists(self, site: str, credential_id: str) -> bool:
        key = self._key(site, credential_id)
        try:
            return bool(await self._redis.exists(key))
        
        except RedisError as e:
            logger.warning(f"Redis error checking exists {key}: {e}")
            return False
        
    async def list_credentials(self, site: str) -> list[str]:
        index_key = self._index_key(site)
        try:
            members = await self._redis.smembers(index_key)
        except RedisError as e:
            logger.warning(f"Redis error listing users for {site}: {e}")
            return []
        user_ids = sorted(m.decode("utf-8") for m in members)
        return await self._prune_missing(site, user_ids)