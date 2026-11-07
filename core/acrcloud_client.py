from __future__ import annotations

import base64
import array
import hashlib
import hmac
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path
from urllib.parse import urlparse

import requests
from core.dns_recovery import DNSRecovery, is_dns_error
from core.network_settings import proxy_for_url
from dotenv import load_dotenv
from mutagen import File as MutagenFile

from core.cache_utils import (
    atomic_write_json,
    cache_ttl_days,
    file_signature,
    read_json_if_fresh,
    stable_digest,
)


ACR_ENDPOINT = "/v1/identify"
SAMPLE_DURATION = 11.0
REQUEST_TIMEOUT = 25
MAX_QPS_ATTEMPTS = 5
ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache" / "acrcloud_raw"
CACHE_TTL_DAYS = cache_ttl_days("TAGCLEANER_ACRCLOUD_CACHE_DAYS", 30.0)

_session_local = threading.local()
_dns_lock = threading.Lock()
_dns_guards = {}


def _dns_guard(host):
    with _dns_lock:
        return _dns_guards.setdefault(host, DNSRecovery(host))


def acrcloud_dns_stats():
    with _dns_lock:
        guards = list(_dns_guards.items())
    return {host: guard.stats() for host, guard in guards}


def _dns_deferred(host):
    # A proxy can resolve the provider remotely. Never make local provider DNS
    # a prerequisite for a route that already works through that proxy.
    if proxy_for_url(f'https://{host}{ACR_ENDPOINT}'):
        return None
    return _dns_guard(host).deferred()


class _DNSDeferred(RuntimeError):
    def __init__(self, result):
        super().__init__(result['error'])
        self.result = result


class _QuotaExhausted(RuntimeError):
    pass


class _RequestGate:
    """Shared start spacing and cooldown; cached fragments never enter here."""

    def __init__(self, interval, *, clock=None, sleep=None):
        self.interval = interval
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._next_start = 0.0
        self._quota_error = None

    def check_quota(self):
        with self._lock:
            if self._quota_error:
                raise _QuotaExhausted(self._quota_error)

    def quota_exhausted(self, message):
        with self._lock:
            self._quota_error = f'ACRCloud 3003: {message or "исчерпан лимит запросов"}; новые запросы отложены до следующего запуска'

    def wait(self):
        while True:
            with self._lock:
                if self._quota_error:
                    raise _QuotaExhausted(self._quota_error)
                now = self._clock()
                delay = self._next_start - now
                if delay <= 0:
                    self._next_start = now + self.interval
                    return
            # Recheck after waking: another worker or a 3015 response may
            # have advanced the shared deadline while we were asleep.
            self._sleep(min(delay, 1.0))

    def qps_exceeded(self, attempt):
        with self._lock:
            # Retain the slower rate for this run instead of alternating
            # successful requests with the same burst that caused 3015.
            self.interval = max(self.interval, min(self.interval * 2.0, 10.0))
            cooldown = max(self.interval, min(2.0 ** attempt, 30.0))
            self._next_start = max(self._next_start, self._clock() + cooldown)


_request_gates_lock = threading.Lock()
_request_gates = {}


def _request_interval():
    # A conservative application default, not an assumed account tariff.
    try:
        value = float(os.getenv("TAGCLEANER_ACRCLOUD_REQUEST_INTERVAL", "1.1"))
    except (TypeError, ValueError):
        return 1.1
    return max(0.05, min(60.0, value)) if math.isfinite(value) else 1.1


def _get_request_gate(host, access_key):
    # Different projects have separate limits; never retain the secret key.
    key = (host.casefold(), hashlib.sha256(access_key.encode("utf-8")).hexdigest())
    with _request_gates_lock:
        if key not in _request_gates:
            _request_gates[key] = _RequestGate(_request_interval())
        return _request_gates[key]


def _get_session():
    session = getattr(_session_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "TagCleaner/0.7 (+parallel local audio identity cache)",
            "Accept": "application/json",
        })
        _session_local.session = session
    return session


def _cache_path(file_path: Path, sample_position: float) -> Path:
    key = {
        "file": file_signature(file_path),
        "host": (os.getenv("ACRCLOUD_HOST") or "").strip().casefold(),
        "sample_position": round(float(sample_position), 6),
        "sample_duration": SAMPLE_DURATION,
    }
    return CACHE_DIR / f"{stable_digest(key)}.json"


def _load_cached_result(file_path: Path, sample_position: float):
    try:
        payload = read_json_if_fresh(
            _cache_path(file_path, sample_position),
            ttl_days=CACHE_TTL_DAYS,
        )
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("status") not in {"FOUND", "NOT_FOUND"}:
        return None
    return payload


def _save_cached_result(file_path: Path, sample_position: float, payload: dict):
    try:
        if payload.get("status") in {"FOUND", "NOT_FOUND"}:
            atomic_write_json(_cache_path(file_path, sample_position), payload)
    except Exception:
        pass


def _clean_host(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return ""

    if "://" in value:
        parsed = urlparse(value)
        value = parsed.netloc or parsed.path

    return value.strip().strip("/")


def _load_credentials() -> tuple[str, str, str]:
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')

    host = _clean_host(os.getenv("ACRCLOUD_HOST"))
    access_key = (os.getenv("ACRCLOUD_ACCESS_KEY") or "").strip()
    access_secret = (os.getenv("ACRCLOUD_ACCESS_SECRET") or "").strip()

    missing = []
    if not host:
        missing.append("ACRCLOUD_HOST")
    if not access_key:
        missing.append("ACRCLOUD_ACCESS_KEY")
    if not access_secret:
        missing.append("ACRCLOUD_ACCESS_SECRET")

    if missing:
        raise RuntimeError(
            "Не настроены переменные ACRCloud в .env: " + ", ".join(missing)
        )

    return host, access_key, access_secret


def _audio_duration(file_path: Path) -> float:
    try:
        audio = MutagenFile(file_path)
        duration = float(getattr(getattr(audio, 'info', None), 'length', 0.0) or 0.0)
        if math.isfinite(duration) and duration > 0:
            return duration
        original_error = "Не удалось определить длительность аудио через Mutagen."
    except Exception as error:
        original_error = f"Mutagen не смог прочитать длительность аудио: {error}"
    # A decodable file can have MP3 headers which Mutagen cannot time. Use
    # the decoder's companion probe before abandoning all audio fragments.
    try:
        probe = shutil.which('ffprobe')
        if not probe:
            ffmpeg = shutil.which('ffmpeg')
            if ffmpeg:
                sibling = Path(ffmpeg).with_name('ffprobe.exe' if Path(ffmpeg).suffix.lower() == '.exe' else 'ffprobe')
                if sibling.is_file():
                    probe = str(sibling)
        if not probe:
            raise RuntimeError('ffprobe не найден')
        result = subprocess.run([probe, '-v', 'error', '-show_entries',
                                 'format=duration:stream=codec_type,duration', '-of', 'json', str(file_path)],
                                capture_output=True, text=True, check=False, timeout=30)
        if result.returncode:
            raise RuntimeError((result.stderr or 'ошибка чтения ffprobe').strip())
        payload = json.loads(result.stdout)
        streams = [item for item in payload.get('streams') or [] if item.get('codec_type') == 'audio']
        if not streams:
            raise RuntimeError('ffprobe не обнаружил аудиопоток')
        values = [streams[0].get('duration'), (payload.get('format') or {}).get('duration')]
        for value in values:
            try:
                duration = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(duration) and duration > 0:
                return duration
        raise RuntimeError('ffprobe не смог определить длительность аудио')
    except Exception as error:
        raise RuntimeError(f'{original_error} Резервное чтение: {error}') from error


def _choose_sample_start(duration: float, sample_position: float = 0.30) -> float:
    if duration <= SAMPLE_DURATION + 1.0:
        return 0.0

    try:
        position = float(sample_position)
    except (TypeError, ValueError):
        position = 0.30

    position = max(0.0, min(1.0, position))
    start = duration * position
    max_start = max(0.0, duration - SAMPLE_DURATION - 0.5)
    return round(min(start, max_start), 3)


def _find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg не найден в PATH.")
    return ffmpeg


def _create_sample(
    file_path: Path,
    output_path: Path,
    *,
    sample_position: float = 0.30,
) -> dict:
    duration = _audio_duration(file_path)
    start = _choose_sample_start(duration, sample_position)
    ffmpeg = _find_ffmpeg()

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start),
        "-i",
        str(file_path),
        "-t",
        str(SAMPLE_DURATION),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
    ]
    pcm = output_path.suffix.casefold() == ".wav"
    if pcm:
        # Avoid another lossy encoder and retain the original stereo signal.
        command[command.index("-ac") + 1] = "2"
        command[command.index("-ss"):command.index("-ss")] = ["-xerror"]
        command.extend(["-c:a", "pcm_s16le"])
    else:
        command.extend(["-b:a", "96k"])
    command.append(str(output_path))

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )

    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"ffmpeg не смог создать ACRCloud sample: {message}")

    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError("ffmpeg не создал sample для ACRCloud.")

    info = {
        "source_duration": round(duration, 3),
        "sample_start": start,
        "sample_duration": SAMPLE_DURATION,
        "sample_size": output_path.stat().st_size,
        "sample_position": round(float(sample_position), 3),
        "sample_format": "pcm_s16le" if pcm else "mp3",
    }
    if pcm:
        with wave.open(str(output_path), "rb") as sample:
            frames = sample.getnframes()
            if not frames or sample.getsampwidth() != 2:
                raise RuntimeError("PCM sample не содержит пригодного аудиопотока.")
            samples = array.array("h", sample.readframes(frames))
            if not any(samples):
                raise RuntimeError("PCM-фрагмент содержит только нулевые аудиоотсчёты; проверить файл и границы аудио.")
            info.update(sample_duration=round(frames / sample.getframerate(), 3),
                        channels=sample.getnchannels(), sample_rate=sample.getframerate())
    return info


def _signature(access_key: str, access_secret: str, timestamp: str) -> str:
    string_to_sign = "\n".join(
        [
            "POST",
            ACR_ENDPOINT,
            access_key,
            "audio",
            "1",
            timestamp,
        ]
    )

    digest = hmac.new(
        access_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha1,
    ).digest()

    return base64.b64encode(digest).decode("ascii")


def _artist_names(item: dict) -> list[str]:
    result = []
    for artist in item.get("artists") or []:
        if isinstance(artist, dict):
            name = (artist.get("name") or "").strip()
            if name:
                result.append(name)
    return result


def _first_id(container, *path):
    current = container
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, str):
        return current or None
    return current


def _normalize_music_item(item: dict) -> dict:
    external_ids = item.get("external_ids") or {}
    external_metadata = item.get("external_metadata") or {}

    spotify = external_metadata.get("spotify") or {}
    deezer = external_metadata.get("deezer") or {}
    youtube = external_metadata.get("youtube") or {}
    musicbrainz = external_metadata.get("musicbrainz") or []

    mb_track_ids = []
    if isinstance(musicbrainz, list):
        for entry in musicbrainz:
            track_id = _first_id(entry, "track", "id")
            if track_id:
                mb_track_ids.append(track_id)

    artists = _artist_names(item)

    return {
        "artist": " & ".join(artists) if artists else None,
        "artists": artists,
        "title": item.get("title") or None,
        "album": _first_id(item, "album", "name"),
        "release_date": item.get("release_date") or None,
        "label": item.get("label") or None,
        "duration_ms": item.get("duration_ms"),
        "score": item.get("score"),
        "acrid": item.get("acrid") or None,
        "isrc": external_ids.get("isrc") or None,
        "upc": external_ids.get("upc") or None,
        "spotify_track_id": _first_id(spotify, "track", "id"),
        "deezer_track_id": _first_id(deezer, "track", "id"),
        "youtube_id": youtube.get("vid") if isinstance(youtube, dict) else None,
        "musicbrainz_track_ids": mb_track_ids,
        "raw": item,
    }


def _request_identification(sample_path, host, access_key, access_secret, history):
    gate = _get_request_gate(host, access_key)
    sample_bytes = sample_path.stat().st_size
    for attempt in range(1, MAX_QPS_ATTEMPTS + 1):
        gate.wait()
        dns = _dns_deferred(host)
        if dns:
            raise _DNSDeferred(dns)
        # A cooldown can outlive the original timestamp. Sign each actual
        # request after waiting and reopen the same sample from byte zero.
        timestamp = str(int(time.time()))
        data = {
            "access_key": access_key,
            "sample_bytes": str(sample_bytes),
            "timestamp": timestamp,
            "signature": _signature(access_key, access_secret, timestamp),
            "data_type": "audio",
            "signature_version": "1",
        }
        entry = {"attempt": attempt, "sample_format": sample_path.suffix.lstrip(".")}
        history.append(entry)
        with sample_path.open("rb") as sample_file:
            response = _get_session().post(
                f"https://{host}{ACR_ENDPOINT}",
                files={"sample": (sample_path.name, sample_file,
                                  "audio/wav" if sample_path.suffix == ".wav" else "audio/mpeg")},
                data=data,
                timeout=REQUEST_TIMEOUT,
            )
        response.raise_for_status()
        payload = response.json()
        _dns_guard(host).succeeded()
        status = payload.get("status") or {}
        entry.update(status_code=status.get("code"), status_message=status.get("msg") or "")
        if status.get('code') == 3003:
            gate.quota_exhausted(status.get('msg'))
        if status.get("code") != 3015:
            return payload
        # 3015 is a temporary rate limit; 3003 is a request quota and must
        # not enter this loop. Even on exhaustion, protect other workers.
        gate.qps_exceeded(attempt)
        if attempt == MAX_QPS_ATTEMPTS:
            return payload


def _request_diagnostics(history):
    return {
        "request_attempts": len(history),
        "rate_limit_retries": sum(entry.get("attempt", 1) > 1 for entry in history),
        "request_history": history,
    }


def _recognize_with_acrcloud_uncached(
    file_path,
    *,
    sample_position: float = 0.30,
) -> dict:
    """Recognize one audio file with ACRCloud using an ~11s temporary sample."""

    file_path = Path(file_path)
    if not file_path.exists():
        return {
            "status": "ERROR",
            "error": f"Файл не найден: {file_path}",
            "candidates": [],
        }

    try:
        host, access_key, access_secret = _load_credentials()
        _get_request_gate(host, access_key).check_quota()
    except _QuotaExhausted as error:
        return {'status': 'ERROR', 'error': str(error), 'status_code': 3003,
                'quota_skipped': True, 'candidates': [], **_request_diagnostics([])}
    except Exception as error:
        return {"status": "ERROR", "error": str(error), "candidates": []}

    request_history = []
    sample_info = None
    preparation = {}
    try:
        dns = _dns_deferred(host)
        if dns:
            return {**dns, 'candidates': [], **_request_diagnostics([])}
        with tempfile.TemporaryDirectory(prefix="tagcleaner_acr_") as temp_dir:
            sample_path = Path(temp_dir) / "sample.mp3"
            sample_info = _create_sample(
                file_path,
                sample_path,
                sample_position=sample_position,
            )
            payload = _request_identification(
                sample_path, host, access_key, access_secret, request_history,
            )
            if (payload.get("status") or {}).get("code") == 2004:
                # Re-decode this very same position from the source, rather
                # than replacing an unverified fragment with a different one.
                preparation = {"pcm_fallback": True, "initial_status_code": 2004}
                sample_path = Path(temp_dir) / "sample.wav"
                sample_info = _create_sample(file_path, sample_path, sample_position=sample_position)
                payload = _request_identification(
                    sample_path, host, access_key, access_secret, request_history,
                )

    except _QuotaExhausted as error:
        return {'status': 'ERROR', 'error': str(error), 'status_code': 3003,
                'quota_skipped': True, 'candidates': [], 'sample': sample_info,
                **_request_diagnostics(request_history), **preparation}
    except _DNSDeferred as error:
        return {**error.result, 'candidates': [], 'sample': sample_info,
                **_request_diagnostics(request_history), **preparation}
    except requests.RequestException as error:
        dns_error = is_dns_error(error) and not proxy_for_url(f'https://{host}{ACR_ENDPOINT}')
        if dns_error:
            _dns_guard(host).failed()
        return {
            "status": "ERROR",
            "error": (f'DNS: адрес {host} недоступен; проверка ACRCloud не завершена.'
                      if dns_error else f"ACRCloud network/API error: {error}"),
            'error_kind': 'dns' if dns_error else 'network_or_api',
            'verification_incomplete': True,
            "candidates": [],
            "sample": sample_info,
            **_request_diagnostics(request_history),
            **preparation,
        }
    except ValueError as error:
        return {
            "status": "ERROR",
            "error": f"ACRCloud вернул не-JSON ответ: {error}",
            "candidates": [],
            "sample": sample_info,
            **_request_diagnostics(request_history),
            **preparation,
        }
    except Exception as error:
        return {
            "status": "ERROR",
            "error": str(error),
            "candidates": [],
            "sample": sample_info,
            **_request_diagnostics(request_history),
            **preparation,
        }

    status = payload.get("status") or {}
    status_code = status.get("code")
    status_message = status.get("msg") or ""
    diagnostics = {**_request_diagnostics(request_history), **preparation}

    if status_code == 1001:
        return {
            "status": "NOT_FOUND",
            "error": None,
            "status_code": status_code,
            "status_message": status_message,
            "candidates": [],
            "sample": sample_info,
            "raw": payload,
            **diagnostics,
        }

    if status_code != 0:
        return {
            "status": "ERROR",
            "error": f"ACRCloud {status_code}: {status_message}",
            "status_code": status_code,
            "status_message": status_message,
            "candidates": [],
            "sample": sample_info,
            "raw": payload,
            **diagnostics,
        }

    music = ((payload.get("metadata") or {}).get("music") or [])
    candidates = [_normalize_music_item(item) for item in music if isinstance(item, dict)]

    if not candidates:
        return {
            "status": "NOT_FOUND",
            "error": None,
            "status_code": status_code,
            "status_message": status_message,
            "candidates": [],
            "sample": sample_info,
            "raw": payload,
            **diagnostics,
        }

    best = candidates[0]

    return {
        "status": "FOUND",
        "error": None,
        "status_code": status_code,
        "status_message": status_message,
        "artist": best.get("artist"),
        "title": best.get("title"),
        "album": best.get("album"),
        "release_date": best.get("release_date"),
        "label": best.get("label"),
        "duration_ms": best.get("duration_ms"),
        "score": best.get("score"),
        "acrid": best.get("acrid"),
        "isrc": best.get("isrc"),
        "upc": best.get("upc"),
        "spotify_track_id": best.get("spotify_track_id"),
        "deezer_track_id": best.get("deezer_track_id"),
        "youtube_id": best.get("youtube_id"),
        "musicbrainz_track_ids": best.get("musicbrainz_track_ids") or [],
        "candidates": candidates,
        "sample": sample_info,
        "raw": payload,
        **diagnostics,
    }


def recognize_with_acrcloud(
    file_path,
    *,
    sample_position: float = 0.30,
    use_cache: bool = True,
) -> dict:
    """
    Cached ACRCloud recognition. The cache stores only raw/normalized provider
    output, never TagCleaner SAFE/REVIEW decisions.
    """
    file_path = Path(file_path)

    if use_cache and file_path.exists():
        cached = _load_cached_result(file_path, sample_position)
        if cached is not None:
            return {**cached, "cache_hit": True}

    result = _recognize_with_acrcloud_uncached(
        file_path,
        sample_position=sample_position,
    )
    result["cache_hit"] = False

    if use_cache and file_path.exists():
        _save_cached_result(file_path, sample_position, result)

    return result
