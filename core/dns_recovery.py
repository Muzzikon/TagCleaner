"""Reactive DNS recovery, scoped to one provider; no global socket patches."""
import math
import socket
import threading
import time


def is_dns_error(error):
    pending, seen = [error], set()
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, socket.gaierror) or type(value).__name__ in {'ClientConnectorDNSError', 'NameResolutionError'}:
            return True
        if isinstance(value, BaseException):
            pending.extend(v for v in (value.__cause__, value.__context__, getattr(value, 'reason', None),
                                      getattr(value, 'os_error', None), *value.args) if isinstance(v, BaseException))
    return False


class DNSRecovery:
    """Only activated by actual provider failures, never by speculative probes.

    The system resolver is retried at 30/60/120/240/300-second intervals.
    Restored DNS merely permits another real HTTPS request; it is not evidence
    that the API works, nor that a track was found or absent.
    """
    def __init__(self, host):
        self.host = host
        self._lock = threading.Lock()
        self._active = False
        self._next_probe = 0.0
        self._failed_probes = 0
        self._stats = {'failures': 0, 'probes': 0, 'recoveries': 0, 'deferred': 0}

    def failed(self):
        with self._lock:
            self._stats['failures'] += 1
            if not self._active:
                self._active = True
                self._failed_probes = 0
                self._next_probe = time.monotonic() + 30

    def succeeded(self):
        with self._lock:
            if self._active:
                self._stats['recoveries'] += 1
            self._active = False
            self._next_probe = 0.0
            self._failed_probes = 0

    def deferred(self):
        with self._lock:
            if not self._active:
                return None
            now = time.monotonic()
            if now >= self._next_probe:
                self._stats['probes'] += 1
                try:
                    addresses = socket.getaddrinfo(self.host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)
                    restored = bool(addresses)
                except OSError:
                    restored = False
                if restored:
                    self._active = False
                    self._failed_probes = 0
                    self._next_probe = 0.0
                    self._stats['recoveries'] += 1
                    return None
                self._failed_probes += 1
                self._next_probe = time.monotonic() + min(30 * 2 ** min(self._failed_probes, 4), 300)
            self._stats['deferred'] += 1
            return {'status': 'ERROR', 'error_kind': 'dns', 'deferred': True,
                    'verification_incomplete': True, 'host': self.host,
                    'retry_after_seconds': max(1, math.ceil(self._next_probe - time.monotonic())),
                    'error': f'DNS: адрес {self.host} недоступен; проверка отложена до восстановления разрешения имени.'}

    def stats(self):
        with self._lock:
            return {**self._stats, 'host': self.host, 'outage_active': self._active,
                    'retry_after_seconds': max(0, math.ceil(self._next_probe - time.monotonic()))}
