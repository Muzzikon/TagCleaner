import os
import math
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from core.singleflight import cached_singleflight

from core.cache_utils import (
    atomic_write_json,
    cache_ttl_days,
    read_json_if_fresh,
    stable_digest,
)


load_dotenv()

API_KEY = os.getenv("ACOUSTID_API_KEY")
LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache" / "acoustid_lookup"
CACHE_TTL_DAYS = cache_ttl_days("TAGCLEANER_ACOUSTID_CACHE_DAYS", 30.0)

_session = requests.Session()
_session.headers.update({
    "User-Agent": "TagCleaner/0.6 (+local audio identity cache)",
    "Accept": "application/json",
})

_request_lock = threading.RLock()
_last_request_time = 0.0
_server_pause_until = 0.0
_outage_until = 0.0
_consecutive_failures = 0
REQUEST_INTERVAL = 0.34
MAX_LOOKUP_ATTEMPTS = 3


def _wait_for_rate_limit():
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    delay = max(REQUEST_INTERVAL - elapsed, _server_pause_until - time.monotonic())
    if delay > 0:
        time.sleep(delay)


def _record_transport_failure():
    global _consecutive_failures, _outage_until
    with _request_lock:
        _consecutive_failures += 1
        if _consecutive_failures >= 3:
            _outage_until = time.monotonic() + 60



def _cache_path(duration, fingerprint):
    key = {
        "duration": int(round(float(duration))),
        "fingerprint": str(fingerprint),
    }
    return CACHE_DIR / f"{stable_digest(key)}.json"


def _load_cache(duration, fingerprint):
    try:
        data = read_json_if_fresh(
            _cache_path(duration, fingerprint),
            ttl_days=CACHE_TTL_DAYS,
        )
    except Exception:
        return None

    if not isinstance(data, dict) or data.get("status") != "ok":
        return None
    if not any(isinstance(result, dict) and result.get('recordings') for result in data.get('results') or []):
        try:
            data = read_json_if_fresh(_cache_path(duration, fingerprint),
                                      ttl_days=cache_ttl_days('TAGCLEANER_NEGATIVE_CACHE_DAYS', 1.0))
        except Exception:
            return None
    return data if isinstance(data, dict) and data.get('status') == 'ok' else None


def _save_cache(duration, fingerprint, data):
    try:
        if isinstance(data, dict) and data.get("status") == "ok":
            atomic_write_json(_cache_path(duration, fingerprint), data)
    except Exception:
        # Cache is an optimization only.
        pass


@cached_singleflight(lambda duration, fingerprint, **kwargs: (int(round(float(duration))), str(fingerprint)))
def lookup_fingerprint(duration, fingerprint, *, use_cache=True):
    if use_cache:
        cached = _load_cache(duration, fingerprint)
        if cached is not None:
            return cached

    if not API_KEY:
        raise RuntimeError(
            "Не найден ACOUSTID_API_KEY в файле .env"
        )

    global _last_request_time, _server_pause_until, _consecutive_failures, _outage_until
    for attempt in range(1, MAX_LOOKUP_ATTEMPTS + 1):
        retry_delay = min(2 ** attempt, 6)
        try:
            with _request_lock:
                if time.monotonic() < _outage_until:
                    raise RuntimeError('AcoustID временно недоступен; запрос отложен после сетевых ошибок, проверку нужно повторить.')
                if _server_pause_until - time.monotonic() > 60:
                    raise RuntimeError('AcoustID просит длительную паузу; проверку нужно повторить позже.')
                _wait_for_rate_limit()
                try:
                    response = _session.post(
                        LOOKUP_URL,
                        data={'client': API_KEY, 'format': 'json',
                              'duration': int(round(duration)), 'fingerprint': fingerprint,
                              'meta': 'recordings releasegroups releases'},
                        timeout=(10, 30),
                    )
                finally:
                    _last_request_time = time.monotonic()
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict) or data.get('status') != 'ok':
                error = data.get('error') if isinstance(data, dict) else None
                error = error if isinstance(error, dict) else {}
                # Never expose the request/client key or retry invalid credentials.
                raise RuntimeError(f"AcoustID: {error.get('message') or 'некорректный ответ API'}")
            with _request_lock:
                _consecutive_failures = 0
                _outage_until = 0
            break
        except (requests.ConnectionError, requests.Timeout) as error:
            _record_transport_failure()
            if attempt == MAX_LOOKUP_ATTEMPTS:
                raise RuntimeError('AcoustID: соединение не удалось после 3 попыток') from error
        except requests.HTTPError as error:
            if response.status_code in {500, 502, 503, 504}:
                _record_transport_failure()
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == MAX_LOOKUP_ATTEMPTS:
                raise RuntimeError(f'AcoustID HTTP {response.status_code}') from error
            try:
                required_wait = float(response.headers.get('Retry-After') or 0)
            except (TypeError, ValueError):
                required_wait = 0
            if not math.isfinite(required_wait):
                required_wait = 0
            with _request_lock:
                _server_pause_until = max(_server_pause_until, time.monotonic() + required_wait)
            if required_wait > 60:
                raise RuntimeError('AcoustID просит длительную паузу; запрос отложен до следующей проверки.') from error
            retry_delay = max(retry_delay, required_wait)
        except ValueError as error:
            if attempt == MAX_LOOKUP_ATTEMPTS:
                raise RuntimeError('AcoustID: некорректный JSON после 3 попыток') from error
        time.sleep(retry_delay)

    if use_cache:
        _save_cache(duration, fingerprint, data)

    return data


def extract_candidates(data):
    candidates = []

    for result in data.get("results", []):
        score = result.get("score", 0)

        for recording in result.get("recordings", []):
            artists = []

            for artist in recording.get("artists", []):
                name = artist.get("name")

                if name:
                    artists.append(name)

            release_groups = []

            for group in recording.get("releasegroups", []):
                release_groups.append({
                    "id": group.get("id"),
                    "title": group.get("title"),
                    "type": group.get("type"),
                })

            candidates.append({
                "score": score,
                "recording_id": recording.get("id"),
                "title": recording.get("title"),
                "artists": artists,
                "duration": recording.get("duration"),
                "release_groups": release_groups,
            })

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    return candidates
