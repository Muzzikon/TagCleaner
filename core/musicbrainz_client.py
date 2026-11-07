import json
import os
import time
import threading
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests
import ssl
import urllib.error
import urllib.parse
import urllib.request

from core.cache_utils import atomic_write_json
from core.singleflight import cached_singleflight
from core.dns_recovery import DNSRecovery, is_dns_error
from core.network_settings import curl_environment

from core.naming import canonical_artist_credit_from_items
from core.release_context import (
    build_release_contexts,
)


BASE_URL = "https://musicbrainz.org/ws/2"

ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache" / "musicbrainz"

TRACK_TITLES_CACHE_DIR = (
    ROOT_DIR
    / "cache"
    / "musicbrainz_track_titles"
)

OFFICIAL_RELEASES_CACHE_DIR = (
    ROOT_DIR
    / "cache"
    / "musicbrainz_official_releases"
)

RELEASE_CONTEXTS_CACHE_DIR = (
    ROOT_DIR
    / "cache"
    / "musicbrainz_release_contexts"
)

USER_AGENT = os.getenv("TAGCLEANER_MUSICBRAINZ_USER_AGENT", "TagCleaner/0.13 (local music metadata audit)")

REQUEST_INTERVAL = 1.1
MAX_ATTEMPTS = 6
PROVIDER_COOLDOWN_SECONDS = 20.0
URLLIB_FALLBACK_FROM_ATTEMPT = 4

_last_request_time = 0.0
_request_lock = threading.RLock()
_entity_locks_guard = threading.Lock()
_entity_locks = {}
_provider_cooldown_until = 0.0
_force_fresh_connections = False
_cooldown_failures = 0
_transport_stats_lock = threading.Lock()
_transport_stats = {}
_preferred_fallback = None
_dns_recovery = DNSRecovery('musicbrainz.org')


def musicbrainz_dns_stats():
    return _dns_recovery.stats()


def _record_transport(name, *, success=False, error=None):
    with _transport_stats_lock:
        counts = _transport_stats.setdefault(name, {"attempts": 0, "successes": 0})
        if error is not None:
            counts['failures'] = counts.get('failures', 0) + 1
            counts['last_error'] = _short_transport_error(error)
        else:
            counts["successes" if success else "attempts"] += 1


def musicbrainz_transport_stats():
    with _transport_stats_lock:
        return {name: dict(counts) for name, counts in _transport_stats.items()}


def _native_curl_path():
    if platform.system() != "Windows":
        return None
    # Prefer Windows' own build; a Git installation may put an OpenSSL curl
    # ahead of it in PATH, which would not give us an independent TLS stack.
    system_root = os.getenv("SystemRoot")
    if system_root:
        executable = Path(system_root) / "System32" / "curl.exe"
        if executable.is_file():
            return str(executable)
    return shutil.which("curl.exe")


def _curl_request_json(executable, url, params, *, compatibility=False):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "musicbrainz.org":
        raise ValueError("Резервный транспорт предназначен только для HTTPS MusicBrainz.")
    query = urllib.parse.urlencode(params or {}, doseq=True)
    full_url = f"{url}?{query}" if query else url
    headers = {}
    try:
        with tempfile.TemporaryDirectory(prefix="tagcleaner_mb_") as directory:
            header_file = Path(directory) / "headers.txt"
            command = [executable, "--disable", "--silent", "--show-error",
                       "--proto", "=https", "--connect-timeout", "10", "--max-time", "40",
                       "--header", f"User-Agent: {USER_AGENT}", "--header", "Accept: application/json",
                       "--dump-header", str(header_file), "--write-out", "\n%{http_code}", "--url", full_url]
            if compatibility:
                # A different negotiation path for broken IPv6/TLS 1.3/ALPN
                # routes. HTTPS, host verification and certificate checks stay
                # enabled. Do not retry the identical Schannel handshake.
                command[2:2] = ['--ipv4', '--http1.1', '--tlsv1.2', '--tls-max', '1.2']
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=45, check=False, env=curl_environment(url))
            if header_file.exists():
                for line in header_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    name, separator, value = line.partition(":")
                    if separator and name.casefold() == "retry-after":
                        headers["Retry-After"] = value.strip()
    except (OSError, subprocess.TimeoutExpired) as error:
        raise urllib.error.URLError(f"curl: {error}") from error
    if result.returncode:
        # stderr may include an authenticated proxy URL. Report code/category,
        # never the raw diagnostic string from a child process.
        categories = {5: 'PROXY_DNS_FAILED', 6: 'DNS_FAILED', 7: 'CONNECTION_FAILED',
                      28: 'TIMEOUT', 35: 'TLS_HANDSHAKE_FAILED',
                      60: 'TLS_CERTIFICATE_VERIFY_FAILED', 97: 'PROXY_HANDSHAKE_FAILED'}
        error = urllib.error.URLError(f"curl код {result.returncode}: "
                                     + categories.get(result.returncode, 'TRANSPORT_ERROR'))
        error.curl_code = result.returncode
        raise error
    body, separator, status_text = result.stdout.rpartition(b"\n")
    if not separator or not status_text.strip().isdigit():
        raise urllib.error.URLError("curl не вернул код HTTP")
    status = int(status_text.strip())
    if status < 200 or 300 <= status < 400:
        raise urllib.error.URLError(f"curl вернул неожиданный HTTP {status}")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        if status >= 400:
            payload = {}
        else:
            raise urllib.error.URLError("curl: ответ MusicBrainz не является JSON") from error
    return status, headers, payload


def _entity_lock(key):
    with _entity_locks_guard:
        lock = _entity_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _entity_locks[key] = lock
        return lock


def _create_session():
    session = requests.Session()

    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })

    return session


_session = _create_session()


def _wait_for_rate_limit():
    global _last_request_time

    elapsed = (
        time.monotonic()
        - _last_request_time
    )

    if elapsed < REQUEST_INTERVAL:
        time.sleep(
            REQUEST_INTERVAL - elapsed
        )


def _reset_session():
    global _session

    try:
        _session.close()
    except Exception:
        pass

    _session = _create_session()


def _short_transport_error(error):
    if error is None:
        return "неизвестная сетевая ошибка"

    name = type(error).__name__
    message = str(error).replace("\n", " ").strip()

    # requests/urllib3 can include the full URL and several nested exceptions.
    # Keep terminal/report output readable without losing the useful reason.
    lowered = message.casefold()
    if "unexpected_eof_while_reading" in lowered or "eof occurred" in lowered:
        return f"{name}: TLS-соединение было неожиданно закрыто удалённой стороной"
    if "timed out" in lowered or "timeout" in lowered:
        return f"{name}: тайм-аут соединения"
    if "name resolution" in lowered or "failed to resolve" in lowered:
        return f"{name}: ошибка DNS"
    if len(message) > 220:
        message = message[:217] + "..."
    return f"{name}: {message}" if message else name


def _urllib_request_json(url, params, *, tls12=False):
    query = urllib.parse.urlencode(params or {}, doseq=True)
    full_url = f"{url}?{query}" if query else url
    request = urllib.request.Request(
        full_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Connection": "close",
        },
        method="GET",
    )
    context = ssl.create_default_context()
    if tls12:
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        status = int(getattr(response, "status", 200) or 200)
        payload = response.read()
        return status, dict(response.headers.items()), json.loads(payload.decode("utf-8"))


def _request_json(url, params):
    global _last_request_time
    global _provider_cooldown_until
    global _force_fresh_connections
    global _cooldown_failures
    global _preferred_fallback

    # MusicBrainz asks clients to stay at roughly one request/second. Multiple
    # TagCleaner pipeline workers may call this function concurrently, but the
    # provider itself must remain serialized. Other providers continue in
    # parallel while this lock waits/sleeps.
    with _request_lock:
        dns = _dns_recovery.deferred()
        if dns:
            raise RuntimeError(dns['error'] + f" Повтор DNS через ~{dns['retry_after_seconds']} с.")
        now = time.monotonic()
        if now < _provider_cooldown_until:
            remaining = max(1, int(round(_provider_cooldown_until - now)))
            raise RuntimeError(
                "MusicBrainz временно на паузе после сетевого сбоя; "
                f"повтор через ~{remaining} с"
            )

        last_error = None
        last_http_status = None
        dns_failures = 0

        for attempt in range(1, MAX_ATTEMPTS + 1):
            _wait_for_rate_limit()
            transport = 'requests'

            try:
                # If urllib3/OpenSSL starts returning EOF on a reused TLS socket,
                # switch the rest of the process to fresh connections. The final
                # attempts also use Python's stdlib HTTP stack as an independent
                # transport fallback. This changes transport only, not data or
                # recognition decisions.
                preferred = _preferred_fallback if attempt == 1 else None
                if preferred or attempt >= URLLIB_FALLBACK_FROM_ATTEMPT:
                    curl = _native_curl_path() if (preferred in {'curl', 'curl_compat'}
                                                   or not preferred and attempt % 2 == 0) else None
                    compatibility = preferred == 'curl_compat' or not preferred and attempt >= 6
                    transport = (('curl_compat' if compatibility else 'curl') if curl
                                 else ('urllib_tls12' if preferred == 'urllib_tls12' or compatibility else 'urllib'))
                    _record_transport(transport)
                    if transport == 'curl_compat':
                        status, headers, payload = _curl_request_json(curl, url, params, compatibility=True)
                    elif curl:
                        status, headers, payload = _curl_request_json(curl, url, params)
                    elif transport == 'urllib_tls12':
                        status, headers, payload = _urllib_request_json(url, params, tls12=True)
                    else:
                        status, headers, payload = _urllib_request_json(url, params)
                    _last_request_time = time.monotonic()
                    last_http_status = status

                    if status in {429, 500, 502, 503, 504}:
                        last_error = RuntimeError(f"MusicBrainz HTTP {status}")
                        _record_transport(transport, error=last_error)
                        if attempt < MAX_ATTEMPTS:
                            retry_after = headers.get("Retry-After")
                            if retry_after and str(retry_after).isdigit():
                                delay = float(retry_after)
                            else:
                                delay = min(2 ** attempt, 6)
                            time.sleep(delay)
                            continue
                        break
                    if status >= 400:
                        _record_transport(transport, error=RuntimeError(f'MusicBrainz HTTP {status}'))
                        raise RuntimeError(f"MusicBrainz HTTP {status}")
                    if not isinstance(payload, dict):
                        raise urllib.error.URLError('MusicBrainz вернул JSON без объекта ответа')
                    _provider_cooldown_until = 0.0
                    _cooldown_failures = 0
                    _record_transport(transport, success=True)
                    # Reuse a working fallback for subsequent requests in this
                    # process, instead of paying for three known broken calls
                    # before every Recording/Release lookup.
                    _preferred_fallback = transport
                    _dns_recovery.succeeded()
                    return payload

                headers = None
                if _force_fresh_connections or attempt > 1:
                    headers = {"Connection": "close"}

                _record_transport("requests")
                response = _session.get(
                    url,
                    params=params,
                    timeout=(10, 30),
                    headers=headers,
                )

                _last_request_time = time.monotonic()
                last_http_status = response.status_code

                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = RuntimeError(
                        "MusicBrainz HTTP "
                        f"{response.status_code}"
                    )
                    _record_transport(transport, error=last_error)

                    if attempt < MAX_ATTEMPTS:
                        retry_after = response.headers.get("Retry-After")
                        if retry_after and retry_after.isdigit():
                            delay = float(retry_after)
                        else:
                            delay = min(2 ** attempt, 6)

                        time.sleep(delay)
                        _reset_session()
                        _force_fresh_connections = True
                        continue
                    break

                response.raise_for_status()
                _provider_cooldown_until = 0.0
                _cooldown_failures = 0
                payload = response.json()
                if not isinstance(payload, dict):
                    raise urllib.error.URLError('MusicBrainz вернул JSON без объекта ответа')
                _record_transport("requests", success=True)
                _dns_recovery.succeeded()
                return payload

            except urllib.error.HTTPError as error:
                _last_request_time = time.monotonic()
                last_http_status = int(getattr(error, "code", 0) or 0)
                last_error = error
                _record_transport(transport, error=error)
                if last_http_status in {429, 500, 502, 503, 504} and attempt < MAX_ATTEMPTS:
                    retry_after = error.headers.get("Retry-After") if error.headers else None
                    delay = float(retry_after) if retry_after and str(retry_after).isdigit() else min(2 ** attempt, 6)
                    time.sleep(delay)
                    continue
                if last_http_status in {429, 500, 502, 503, 504}:
                    break
                raise

            except (
                requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                urllib.error.URLError,
                TimeoutError,
                ssl.SSLError,
            ) as error:
                _last_request_time = time.monotonic()
                last_error = error
                _record_transport(transport, error=error)
                if is_dns_error(error):
                    dns_failures += 1
                if attempt == 1:
                    _preferred_fallback = None
                _force_fresh_connections = True
                _reset_session()

                if attempt < MAX_ATTEMPTS:
                    time.sleep(min(2 ** attempt, 6))
                    continue

            except requests.exceptions.RequestException as error:
                _record_transport(transport, error=error)
                raise

        # Do not make every queued file spend another full six-attempt cycle
        # while MusicBrainz or the route to it is clearly unhealthy. Calls made
        # during this short cooldown fail fast, are NOT checkpointed as complete,
        # and become eligible for a clean retry later/next run.
        _cooldown_failures += 1
        # Try all existing transports first: a proxy or a different resolver
        # may work. Gate later cycles only after actual, repeated DNS failures.
        if dns_failures >= 2 and last_http_status is None:
            _dns_recovery.failed()
        cooldown = min(PROVIDER_COOLDOWN_SECONDS * (2 ** min(_cooldown_failures - 1, 4)), 300.0)
        _provider_cooldown_until = time.monotonic() + cooldown
        detail = _short_transport_error(last_error)
        if last_http_status:
            detail = f"HTTP {last_http_status}; {detail}"
        raise RuntimeError(
            "MusicBrainz временно недоступен после "
            f"{MAX_ATTEMPTS} попыток ({detail}). "
            f"Пауза провайдера {int(cooldown)} с; "
            "файл будет повторён, checkpoint не фиксируется"
        )


def get_recording(recording_id):
    if not recording_id:
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{recording_id}.json"

    # Single-flight per Recording. A second worker that asks for the same MBID
    # waits for the first and then reads its freshly written cache instead of
    # spending another MusicBrainz request.
    with _entity_lock(f"recording:{recording_id}"):
        if cache_file.exists():
            try:
                with cache_file.open("r", encoding="utf-8") as file:
                    return json.load(file)
            except (json.JSONDecodeError, OSError):
                pass

        data = _request_json(
            f"{BASE_URL}/recording/{recording_id}",
            params={
                "inc": "artist-credits+isrcs+releases+release-groups",
                "fmt": "json",
            },
        )

        from core.artist_aliases import register_credit
        register_credit(data.get("artist-credit"))
        atomic_write_json(cache_file, data)

        return data


def format_artist_credit(recording):
    if not recording:
        return ""

    return canonical_artist_credit_from_items(
        recording.get("artist-credit", [])
    )


def extract_release_groups(recording):
    if not recording:
        return []

    result = []
    seen = set()

    for release in recording.get(
        "releases",
        [],
    ):
        release_group = (
            release.get(
                "release-group"
            )
            or {}
        )

        group_id = (
            release_group.get("id")
        )

        title = (
            release_group.get("title")
            or release.get("title")
            or ""
        )

        primary_type = (
            release_group.get(
                "primary-type"
            )
            or release_group.get(
                "type"
            )
        )

        secondary_types = (
            release_group.get(
                "secondary-types",
                [],
            )
            or []
        )

        # Предпочтительно различаем
        # настоящие альбомы и сборники.
        if secondary_types:
            secondary_text = ", ".join(
                secondary_types
            )
        else:
            secondary_text = ""

        if group_id:
            key = group_id

        else:
            key = (
                title.casefold(),
                (
                    primary_type
                    or ""
                ).casefold(),
            )

        if key in seen:
            continue

        seen.add(key)

        result.append({
            "id": group_id,
            "title": title,
            "type": primary_type,
            "secondary_types": (
                secondary_types
            ),
            "secondary_text": (
                secondary_text
            ),
            "source": "musicbrainz",
        })

    # Более полезные типы показываем раньше.
    type_priority = {
        "Single": 0,
        "EP": 1,
        "Album": 2,
        "Broadcast": 3,
        "Other": 4,
    }

    result.sort(
        key=lambda item: (
            type_priority.get(
                item.get("type"),
                99,
            ),
            item.get(
                "title",
                "",
            ).casefold(),
        )
    )

    return result


def get_official_releases(
    recording_id,
    refresh=False,
):
    """Return concrete official releases containing this Recording."""
    if not recording_id:
        return []

    OFFICIAL_RELEASES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = OFFICIAL_RELEASES_CACHE_DIR / f"{recording_id}.json"

    with _entity_lock(f"official-releases:{recording_id}"):
        if cache_file.exists() and not refresh:
            try:
                with cache_file.open("r", encoding="utf-8") as file:
                    data = json.load(file)
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError):
                pass

        releases = []
        offset = 0
        limit = 100

        while True:
            data = _request_json(
                f"{BASE_URL}/release",
                params={
                    "recording": recording_id,
                    "status": "official",
                    "inc": "media+recordings+release-groups+artist-credits",
                    "limit": limit,
                    "offset": offset,
                    "fmt": "json",
                },
            )

            page_releases = data.get("releases") if isinstance(data, dict) else None
            total = data.get("release-count") if isinstance(data, dict) else None
            if (not isinstance(page_releases, list) or type(total) is not int
                    or total < 0 or any(not isinstance(r, dict) or not r.get('id') for r in page_releases)):
                raise ValueError('MusicBrainz: некорректная страница официальных релизов; проверка не завершена.')
            if not page_releases and offset < total:
                raise ValueError('MusicBrainz: список официальных релизов оборван; проверка не завершена.')
            known_ids = {r['id'] for r in releases}
            page_ids = [r['id'] for r in page_releases]
            if len(set(page_ids)) != len(page_ids) or known_ids.intersection(page_ids):
                raise ValueError('MusicBrainz: повтор страницы официальных релизов; проверка не завершена.')
            releases.extend(page_releases)
            # The API may return fewer than limit due to its track-count cap.
            offset += len(page_releases)
            if offset > total:
                raise ValueError('MusicBrainz: противоречивое число официальных релизов; проверка не завершена.')
            if offset >= total:
                break

        atomic_write_json(cache_file, releases)

        return releases


@cached_singleflight(lambda recording_id: recording_id)
def get_official_release_tracks(
    recording_id,
):
    """
    Возвращает все Track, которые используют
    данный Recording на официальных релизах
    MusicBrainz.

    Это позволяет различать:

    Recording.title
    и
    Track.title на конкретном релизе.
    """

    if not recording_id:
        return []

    TRACK_TITLES_CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    cache_file = (
        TRACK_TITLES_CACHE_DIR
        / f"{recording_id}.json"
    )

    # -----------------------------------------
    # КЭШ
    # -----------------------------------------

    if cache_file.exists():
        try:
            with cache_file.open(
                "r",
                encoding="utf-8",
            ) as file:
                return json.load(file)

        except (
            json.JSONDecodeError,
            OSError,
        ):
            pass

    # -----------------------------------------
    # КОНКРЕТНЫЕ OFFICIAL RELEASE
    # -----------------------------------------

    releases = (get_official_releases(recording_id))

    result = []

    seen = set()

    for release in releases:
        release_id = (
            release.get("id")
        )

        release_title = (
            release.get("title")
            or ""
        )

        release_status = (
            release.get("status")
            or ""
        )

        release_group = (
            release.get(
                "release-group"
            )
            or {}
        )

        release_group_id = (
            release_group.get("id")
        )

        release_group_title = (
            release_group.get("title")
            or release_title
        )

        primary_type = (
            release_group.get(
                "primary-type"
            )
        )

        secondary_types = (
            release_group.get(
                "secondary-types",
                [],
            )
            or []
        )

        for medium in release.get(
            "media",
            [],
        ):
            tracks = medium.get(
                "tracks"
            )

            # На всякий случай поддерживаем
            # оба варианта структуры.
            if tracks is None:
                tracks = medium.get(
                    "track",
                    [],
                )

            tracks = tracks or []

            for track in tracks:
                recording = (
                    track.get(
                        "recording"
                    )
                    or {}
                )

                if (
                    recording.get("id")
                    != recording_id
                ):
                    continue

                track_title = (
                    track.get("title")
                    or recording.get(
                        "title"
                    )
                    or ""
                )

                track_id = (
                    track.get("id")
                )

                key = (
                    release_id,
                    track_id,
                    track_title.casefold(),
                )

                if key in seen:
                    continue

                seen.add(key)

                result.append({
                    "track_id": (
                        track_id
                    ),

                    "track_title": (
                        track_title
                    ),

                    "track_number": (
                        track.get(
                            "number"
                        )
                    ),

                    "track_position": (
                        track.get(
                            "position"
                        )
                    ),

                    "release_id": (
                        release_id
                    ),

                    "release_title": (
                        release_title
                    ),

                    "release_status": (
                        release_status
                    ),

                    "release_date": (
                        release.get(
                            "date"
                        )
                    ),

                    "release_country": (
                        release.get(
                            "country"
                        )
                    ),

                    "release_group_id": (
                        release_group_id
                    ),

                    "release_group_title": (
                        release_group_title
                    ),

                    "release_group_type": (
                        primary_type
                    ),

                    "secondary_types": (
                        secondary_types
                    ),
                })

    # -----------------------------------------
    # СОХРАНЯЕМ КЭШ
    # -----------------------------------------

    atomic_write_json(cache_file, result)

    return result


@cached_singleflight(lambda recording_id,refresh=False: (recording_id,refresh))
def get_official_release_contexts(
    recording_id,
    refresh=False,
):
    """
    Строит ReleaseContext v2 для каждого
    конкретного официального релиза,
    содержащего данный Recording.

    Никакой релиз здесь НЕ выбирается.

    Эта функция только собирает факты,
    которые потом использует
    release_selector.py.
    """

    if not recording_id:
        return []

    RELEASE_CONTEXTS_CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    cache_file = (
        RELEASE_CONTEXTS_CACHE_DIR
        / f"{recording_id}.json"
    )

    # -----------------------------------------
    # КЭШ ГОТОВЫХ CONTEXT
    # -----------------------------------------

    if cache_file.exists() and not refresh:
        try:
            with cache_file.open(
                "r",
                encoding="utf-8",
            ) as file:
                contexts = json.load(
                    file
                )

            if isinstance(contexts, list) and all(
                "track_artist_credit_items" in item and "track_artist_identities" in item and "album_artist_credit_items" in item
                for item in contexts
            ):
                return contexts

        except (
            json.JSONDecodeError,
            OSError,
        ):
            pass

    # -----------------------------------------
    # БЕРЁМ КОНКРЕТНЫЕ RELEASE
    # -----------------------------------------

    releases = (
        get_official_releases(
            recording_id,refresh=True
        ) if refresh else get_official_releases(
            recording_id
        )
    )

    # -----------------------------------------
    # RELEASE -> RELEASE CONTEXT
    # -----------------------------------------

    contexts = (
        build_release_contexts(
            releases=releases,
            recording_id=recording_id,
        )
    )

    # -----------------------------------------
    # СОРТИРОВКА ТОЛЬКО ДЛЯ УДОБСТВА UI
    #
    # Это НЕ означает, что первый вариант
    # считается правильным.
    # -----------------------------------------

    type_priority = {
        "Single": 0,
        "EP": 1,
        "Album": 2,
        "Broadcast": 3,
        "Other": 4,
        "": 5,
    }

    def sort_key(
        context,
    ):
        secondary = {
            str(value).casefold()
            for value
            in (
                context.get(
                    "secondary_types"
                )
                or []
            )
        }

        # Сборники и DJ-mix показываем
        # после обычных релизов того же типа.
        risky = int(
            bool(
                secondary
                & {
                    "compilation",
                    "dj-mix",
                }
            )
        )

        return (
            type_priority.get(
                context.get(
                    "primary_type"
                )
                or "",
                99,
            ),

            risky,

            context.get(
                "date"
            )
            or "9999-99-99",

            (
                context.get(
                    "title"
                )
                or ""
            ).casefold(),

            context.get(
                "country"
            )
            or "",

            context.get(
                "release_id"
            )
            or "",
        )

    contexts.sort(
        key=sort_key
    )

    # ВАЖНО:
    #
    # Здесь специально НЕ вызываем
    # deduplicate_contexts().
    #
    # Два разных MusicBrainz Release могут
    # иметь одинаковые теги, но разные:
    #
    # release_id
    # country
    # barcode
    # artwork
    #
    # Эти конкретные издания пока сохраняем.
    # Решение об эквивалентности будет
    # принимать release_selector.

    atomic_write_json(cache_file, contexts)

    return contexts
