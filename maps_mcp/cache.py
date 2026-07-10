"""A tiny in-process TTL cache, used to keep repeated route lookups off the
Google Directions API rate limit (see build spec: cache results for 5 minutes).
"""
import time
from functools import wraps


def _freeze(value):
    """Make list/dict arguments hashable so they can be used as cache keys."""
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    return value


def ttl_cache(ttl_seconds: int = 300, maxsize: int = 256):
    """Decorator that caches a function's return value for `ttl_seconds`."""

    def decorator(func):
        store: dict = {}

        @wraps(func)
        def wrapper(*args, **kwargs):
            key = (
                tuple(_freeze(a) for a in args),
                tuple(sorted((k, _freeze(v)) for k, v in kwargs.items())),
            )
            now = time.monotonic()
            cached = store.get(key)
            if cached is not None:
                value, expires_at = cached
                if expires_at > now:
                    return value
                del store[key]
            if len(store) >= maxsize:
                store.clear()
            result = func(*args, **kwargs)
            store[key] = (result, now + ttl_seconds)
            return result

        wrapper.cache_clear = store.clear
        return wrapper

    return decorator
