"""Serialize identical cache misses without serializing unrelated identities."""
from functools import wraps
import threading


def cached_singleflight(key_function):
    def decorate(function):
        guard = threading.RLock()
        flights = {}

        @wraps(function)
        def wrapped(*args, **kwargs):
            key = key_function(*args, **kwargs)
            with guard:
                entry = flights.get(key)
                if entry is None:
                    entry = [threading.RLock(), 0]
                    flights[key] = entry
                entry[1] += 1
            try:
                # Function checks its cache again inside this per-key lock.
                with entry[0]:
                    return function(*args, **kwargs)
            finally:
                with guard:
                    entry[1] -= 1
                    if entry[1] == 0:
                        flights.pop(key, None)
        return wrapped
    return decorate
