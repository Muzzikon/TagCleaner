"""Optional keyless audio source. No default dependency on ShazamIO/native Rust."""
import asyncio
import importlib.metadata
import importlib.util
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
from functools import lru_cache
from email.utils import parsedate_to_datetime
from pathlib import Path

from core.cache_utils import atomic_write_json, cache_ttl_days, file_signature, read_json_if_fresh, stable_digest
from core.fingerprint import _pcm_duration, _probe_duration
from core.dns_recovery import DNSRecovery, is_dns_error
from core.network_settings import http_proxy_for_url

CACHE_DIR = Path(__file__).resolve().parent.parent / 'cache' / 'shazam_samples'
SAMPLE_SECONDS = 12.0
REQUEST_INTERVAL = 3.0
_request_lock = threading.Lock()
_last_request_time = 0.0
_circuit_until = 0.0
_failures = 0
_dependency_error = None
_request_interval = REQUEST_INTERVAL
_clean_samples = 0
_recovery_since = None
_rate_limit_streak = 0
_rate_limited = False
_rate_limit_exhausted = False
_stop_event = threading.Event()
_progress_callback = None
_stats_lock = threading.Lock()
_stats = {}
_dns_recovery = DNSRecovery('amp.shazam.com')


def _reset_pacing_recovery():
    global _clean_samples, _recovery_since
    _clean_samples = 0
    _recovery_since = time.monotonic()


def _note_network_success():
    """Recover cautiously after 120 clean samples AND twenty quiet minutes.

    Cached samples never count. A single success cannot undo a server limit.
    Each reduction needs a fresh quiet window; every error restarts the window.
    """
    global _clean_samples, _recovery_since, _request_interval
    if _request_interval <= REQUEST_INTERVAL:
        return
    now = time.monotonic()
    if _recovery_since is None:
        _recovery_since = now
    _clean_samples += 1
    if _clean_samples < 120 or now - _recovery_since < 1200:
        return
    previous = _request_interval
    _request_interval = max(REQUEST_INTERVAL, round(previous * .8, 3))
    _reset_pacing_recovery()
    _count('pacing_recoveries')
    _progress(event='pacing_recovered', previous_interval_seconds=previous,
              request_interval_seconds=_request_interval)


def _count(name, amount=1):
    with _stats_lock:
        _stats[name] = _stats.get(name, 0) + amount


def shazam_transport_stats():
    with _stats_lock:
        return {**_stats, 'request_interval_seconds': _request_interval,
                'rate_limit_exhausted': _rate_limit_exhausted, 'dns': _dns_recovery.stats()}


def configure_progress(callback=None):
    global _progress_callback
    _progress_callback = callback


def cancel_pending_waits():
    _stop_event.set()


def _progress(**details):
    if _progress_callback is not None:
        try:
            _progress_callback(**details)
        except Exception:
            pass  # Reporting cannot turn a valid audio response into an error.


def _retry_after_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


def _deferred(sample, reason):
    _count('deferred_samples')
    return {'status': 'ERROR', 'error': reason, 'sample': sample,
            'cooldown_skipped': True, 'deferred': True,
            'error_kind': 'rate_limit' if _rate_limited else 'network_pause',
            'retry_after_seconds': max(0, math.ceil(_circuit_until - time.monotonic()))}


def _await_rate_limit(sample):
    """Wait on the single provider lane, not skip thousands of queued files.

    Three consecutive 429 responses end network work for this run; caches
    remain available. A server pause over five minutes is deferred unchanged.
    """
    remaining = max(0, _circuit_until - time.monotonic())
    if _rate_limit_exhausted:
        return _deferred(sample, 'ShazamIO: сервис продолжает ограничивать запросы; проверка отложена до следующего запуска.')
    if remaining > 300:
        return _deferred(sample, 'ShazamIO: длительная пауза сервера; фрагмент отложен для повторной проверки.')
    while remaining > 0:
        _progress(event='rate_limit_wait', remaining_seconds=math.ceil(remaining),
                  request_interval_seconds=_request_interval, sample=sample)
        chunk = min(30, remaining)
        if _stop_event.wait(chunk):
            return _deferred(sample, 'ShazamIO: ожидание прервано; проверка не завершена.')
        _count('wait_seconds', chunk)
        remaining = max(0, _circuit_until - time.monotonic())
    return None


def shazam_capability():
    """Also part of decision keys: installing/enabling the source reruns search."""
    if (os.getenv('TAGCLEANER_SHAZAM') or '1').strip().casefold() in {'0', 'off', 'false', 'no'}:
        return 'disabled'
    return _installed_capability()


@lru_cache(maxsize=1)
def _installed_capability():
    # Installation is picked up on restart; never scan installed distributions
    # tens of thousands of times while constructing per-file checkpoint keys.
    try:
        if importlib.util.find_spec('shazamio') is None:
            return 'not-installed'
        version = importlib.metadata.version('shazamio')
        core_version = importlib.metadata.version('shazamio-core')
    except (ImportError, ValueError, importlib.metadata.PackageNotFoundError):
        return 'not-installed'
    return f'{version}/core-{core_version}'


def shazam_available():
    return shazam_capability() not in {'disabled', 'not-installed'}


def sample_windows(duration):
    """Return up to three non-overlapping windows, including for short tracks."""
    duration = float(duration)
    if not math.isfinite(duration) or duration < 8:
        return []
    length = min(SAMPLE_SECONDS, duration)
    count = min(3, max(1, int(duration // length)))
    positions = (0.20, 0.50, 0.75) if count == 3 else (0.20, 0.75) if count == 2 else (0.5,)
    windows, previous_end = [], 0.0
    for index, position in enumerate(positions):
        latest = max(previous_end, duration - (count - index) * length)
        start = min(max(previous_end, duration * position), latest)
        windows.append({'start_seconds': round(start, 3), 'sample_seconds': length,
                        'source_duration': duration, 'position': position})
        previous_end = start + length
    return windows


def audio_windows(file_path):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ShazamIO: ffmpeg не найден.')
    duration = _probe_duration(Path(file_path), ffmpeg)
    return sample_windows(duration)


def _create_sample(file_path, sample, output):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ShazamIO: ffmpeg не найден.')
    command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-xerror',
               '-ss', str(sample['start_seconds']), '-i', str(file_path),
               '-t', str(sample['sample_seconds']), '-map', '0:a:0', '-vn',
               '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(output)]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=60)
    if result.returncode or result.stderr.strip():
        raise RuntimeError(result.stderr.strip() or 'ShazamIO: фрагмент не декодирован.')
    measured = _pcm_duration(output)
    if measured < 8 or abs(measured - sample['sample_seconds']) > .75:
        raise RuntimeError('ShazamIO: длительность декодированного фрагмента не подтверждена.')


class _HTTPFailure(RuntimeError):
    def __init__(self, status, pause=0):
        super().__init__(f'ShazamIO HTTP {status}')
        self.status = status
        self.pause = pause


class _BoundedHTTPClient:
    """ShazamIO 0.8.1's default 20 retries would make a large run too slow."""
    async def request(self, method, url, *args, **kwargs):
        import aiohttp
        global _last_request_time
        if 'proxy' not in kwargs:
            kwargs['proxy'] = http_proxy_for_url(url)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            for attempt in range(2):
                pacing_started = time.monotonic()
                await asyncio.sleep(max(0, _request_interval - (time.monotonic() - _last_request_time)))
                _count('pacing_wait_seconds', max(0, time.monotonic() - pacing_started))
                request_started = time.monotonic()
                try:
                    _count('http_requests')
                    async with session.request(method, url, **kwargs) as response:
                        if response.status >= 400:
                            pause = _retry_after_seconds(response.headers.get('Retry-After'))
                            raise _HTTPFailure(response.status, pause)
                        payload = await response.json()
                        if not isinstance(payload, dict):
                            raise ValueError('ShazamIO: ответ не является объектом JSON.')
                        return payload
                except _HTTPFailure as error:
                    _reset_pacing_recovery()
                    # 429 is handled outside the 85-second recognition timeout:
                    # respect the whole server pause before retrying this WAV.
                    if attempt or error.status not in {500, 502, 503, 504} or error.pause > 6:
                        raise
                    await asyncio.sleep(max(2, error.pause))
                except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as error:
                    _reset_pacing_recovery()
                    if attempt or is_dns_error(error):
                        raise
                    await asyncio.sleep(2)
                finally:
                    _count('http_and_retry_seconds', max(0, time.monotonic() - request_started))
                    _last_request_time = time.monotonic()


async def _recognize_payload(wav):
    from shazamio import Shazam
    shazam = Shazam(segment_duration_seconds=12, http_client=_BoundedHTTPClient())
    return await asyncio.wait_for(shazam.recognize(str(wav)), timeout=85)


def normalize_response(payload, sample):
    if not isinstance(payload, dict):
        raise ValueError('ShazamIO: некорректный ответ.')
    track = payload.get('track')
    if not track:
        if isinstance(payload.get('matches'), list) and not payload['matches']:
            return {'status': 'NOT_FOUND', 'sample': sample, 'raw': payload}
        raise ValueError('ShazamIO: ответ не содержит ни трека, ни подтверждённого NOT_FOUND.')
    if not isinstance(track, dict) or not track.get('title') or not track.get('subtitle'):
        raise ValueError('ShazamIO: неполная Artist/Title identity.')
    metadata = {}
    for section in track.get('sections') or []:
        if isinstance(section, dict) and section.get('type') == 'SONG':
            for item in section.get('metadata') or []:
                if isinstance(item, dict):
                    metadata[str(item.get('title') or '').casefold()] = item.get('text')
    return {'status': 'FOUND', 'provider': 'shazam', 'shazam_id': str(track.get('key') or ''),
            'artist': track['subtitle'], 'title': track['title'], 'isrc': track.get('isrc'),
            'album': metadata.get('album'), 'release_date': metadata.get('released'),
            # The service supplies no comparable probability. Do not invent one.
            'score': None, 'sample': sample, 'raw': payload}


def _lookup_sample_network(wav, sample):
    global _circuit_until, _failures, _dependency_error
    global _request_interval, _rate_limit_streak, _rate_limited, _rate_limit_exhausted
    with _request_lock:
        if _stop_event.is_set():
            return _deferred(sample, 'ShazamIO: обработка остановлена; проверка не завершена.')
        if _dependency_error:
            return {'status': 'ERROR', 'error': _dependency_error, 'sample': sample}
        dns = _dns_recovery.deferred()
        if dns:
            _count('deferred_samples')
            return {**dns, 'sample': sample}
        for attempt in range(2):
            if _rate_limited:
                deferred = _await_rate_limit(sample)
                if deferred:
                    return deferred
            elif time.monotonic() < _circuit_until:
                return _deferred(sample, 'ShazamIO: временная пауза после сетевых ошибок; повторите проверку позже.')
            if wav is None:
                # The pause may expire between the fast precheck and taking
                # this lock. Never submit the string "None" to the SDK.
                return _deferred(sample, 'ShazamIO: фрагмент не подготовлен во время паузы; требуется повторная проверка.')
            try:
                try:
                    from shazamio import Shazam
                except (ImportError, OSError) as error:
                    _dependency_error = f'ShazamIO: не удалось загрузить дополнительную библиотеку ({type(error).__name__}); проверьте её установку.'
                    return {'status': 'ERROR', 'error': _dependency_error, 'sample': sample}
                payload = asyncio.run(_recognize_payload(wav))
                result = normalize_response(payload, sample)
                _dns_recovery.succeeded()
                _failures = _rate_limit_streak = 0
                _rate_limited = False
                _circuit_until = 0.0
                _count('network_samples_completed')
                _note_network_success()
                return {**result, 'rate_limit_retried': bool(attempt)}
            except Exception as error:
                import aiohttp
                _reset_pacing_recovery()
                rate_error = isinstance(error, _HTTPFailure) and error.status == 429
                dns_error = is_dns_error(error)
                if dns_error:
                    _dns_recovery.failed()
                    _count('dns_errors')
                    _progress(event='dns_unavailable', host=_dns_recovery.host, sample=sample)
                elif rate_error:
                    _count('http_429')
                    _rate_limited = True
                    _rate_limit_streak += 1
                    _request_interval = min(15.0, _request_interval * 2)
                    pause = max(60 * 2 ** min(_rate_limit_streak - 1, 2), error.pause)
                    _circuit_until = time.monotonic() + pause
                    _rate_limit_exhausted = _rate_limit_streak >= 3
                    _progress(event='rate_limited', retry_after_seconds=pause,
                              request_interval_seconds=_request_interval,
                              exhausted=_rate_limit_exhausted, sample=sample)
                    if not attempt and not _rate_limit_exhausted:
                        continue
                elif isinstance(error, (_HTTPFailure, asyncio.TimeoutError, ValueError, aiohttp.ClientError)):
                    _failures += 1
                    _rate_limited = False
                    if _failures >= 3 or getattr(error, 'pause', 0) > 0:
                        _circuit_until = time.monotonic() + max(60, getattr(error, 'pause', 0))
                _count('network_sample_errors')
                return {'status': 'ERROR', 'error': f'ShazamIO: {type(error).__name__}: {error}',
                        'sample': sample, 'error_kind': 'dns' if dns_error else 'rate_limit' if rate_error else 'network',
                        'http_status': getattr(error, 'status', None),
                        'retry_after_seconds': max(0, math.ceil(_circuit_until - time.monotonic()))}


def recognize_sample(file_path, *, sample, refresh_generation=None):
    if not shazam_available():
        return {'status': 'SKIPPED', 'reason': 'ShazamIO отключён или не установлен.', 'sample': sample}
    cache_scope = 'current_check' if refresh_generation else 'shared'
    context = {'file': file_signature(file_path), 'sample': sample, 'schema': 1}
    shared_cache_path = CACHE_DIR / (stable_digest(context) + '.json')
    if refresh_generation:
        context['refresh_generation'] = refresh_generation
    key = stable_digest(context)
    cache_path = CACHE_DIR / (key + '.json')
    try:
        cached = read_json_if_fresh(cache_path, ttl_days=30)
        if isinstance(cached, dict) and cached.get('status') == 'NOT_FOUND':
            cached = read_json_if_fresh(cache_path, ttl_days=cache_ttl_days('TAGCLEANER_NEGATIVE_CACHE_DAYS', 1))
        if isinstance(cached, dict) and cached.get('status') in {'FOUND', 'NOT_FOUND'}:
            _count('cache_hits')
            _count('current_check_cache_hits' if refresh_generation else 'shared_cache_hits')
            return {**cached, 'cache_hit': True, 'cache_scope': cache_scope}
    except OSError:
        pass
    if refresh_generation:
        _count('fresh_cache_misses')
    dns = _dns_recovery.deferred()
    if dns:
        _count('deferred_samples')
        return {**dns, 'sample': sample, 'cache_hit': False, 'cache_scope': cache_scope}
    # Only replies in the selected scope stay usable during an outage. A
    # requested fresh check never silently falls back to an older reply.
    if _dependency_error or _stop_event.is_set() or _rate_limit_exhausted or (
            _rate_limited and _circuit_until - time.monotonic() > 300) or (
            not _rate_limited and time.monotonic() < _circuit_until):
        return {**_lookup_sample_network(None, sample), 'cache_hit': False, 'cache_scope': cache_scope}
    try:
        with tempfile.TemporaryDirectory(prefix='tagcleaner_shazam_') as folder:
            wav = Path(folder) / 'sample.wav'
            _create_sample(file_path, sample, wav)
            result = _lookup_sample_network(wav, sample)
    except Exception as error:
        # A damaged local file does not trip the provider-wide network circuit.
        return {'status': 'ERROR', 'error': f'ShazamIO: фрагмент не подготовлен: {error}',
                'sample': sample, 'cache_hit': False, 'cache_scope': cache_scope}
    if result.get('status') in {'FOUND', 'NOT_FOUND'}:
        try:
            atomic_write_json(cache_path, result)
            if refresh_generation:
                # Later ordinary cached rechecks should see the latest valid
                # reply, while each fresh check keeps its own durable copy.
                atomic_write_json(shared_cache_path, result)
        except OSError:
            pass
    return {**result, 'cache_hit': False, 'cache_scope': cache_scope}
