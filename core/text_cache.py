"""Bounded memoization for pure text functions with immutable results only.

Never apply this to provider responses, artist aliases, scores or file state:
those can change while a run is in progress.
"""
from functools import lru_cache, wraps

MAX_ENTRIES = 8192
MAX_TEXT_LENGTH = 4096


def cached_text(function):
    cached = lru_cache(maxsize=MAX_ENTRIES)(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        # Preserve the original behavior for unusual/non-string inputs and
        # don't retain unexpectedly large metadata in a long library scan.
        if not kwargs and all(type(arg) is str and len(arg) <= MAX_TEXT_LENGTH for arg in args):
            return cached(*args)
        return function(*args, **kwargs)

    wrapped.cache_clear = cached.cache_clear
    wrapped.cache_info = cached.cache_info
    return wrapped
