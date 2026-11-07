import hashlib
import json
import math
import os
from pathlib import Path
from core.cache_utils import atomic_write_json, cache_ttl_days, read_json_if_fresh


ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache" / "audd_recognition"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_VERSION = 1

_AUDD_DISABLED_REASON = None
_AUDD_DISABLED_CATEGORY = None


def audd_network_enabled():
    """AudD network use is opt-in; positive cached evidence stays usable."""
    return (os.getenv('TAGCLEANER_AUDD') or '0').strip().lower() in {'1', 'true', 'yes', 'on'}


def _normalize_fingerprint_data(fingerprint_data):
    fingerprint_data = fingerprint_data or {}

    duration = fingerprint_data.get("duration")
    fingerprint = fingerprint_data.get("fingerprint")

    try:
        duration = round(float(duration), 3)
    except Exception:
        duration = duration

    return {
        "duration": duration,
        "fingerprint": fingerprint,
    }


def make_audd_cache_key(fingerprint_data):
    payload = _normalize_fingerprint_data(fingerprint_data)

    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def _cache_path(fingerprint_data):
    key = make_audd_cache_key(
        fingerprint_data
    )
    return CACHE_DIR / f"{key}.json"


def load_cached_audd_result(fingerprint_data):
    if not _has_cache_identity(fingerprint_data):
        return None
    path = _cache_path(
        fingerprint_data
    )

    if not path.exists():
        return None

    try:
        payload = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
    except Exception:
        return None

    if payload.get("version") != CACHE_VERSION:
        return None

    result = payload.get("result")
    if not isinstance(result, dict):
        return None

    status = result.get("status")
    if status not in {
        "FOUND",
        "NOT_FOUND",
    }:
        return None

    # A past miss is not permanent proof of absence from a growing catalog.
    # Keep positive audio evidence available; only expire negative responses.
    if status == 'NOT_FOUND':
        try:
            if read_json_if_fresh(path, ttl_days=cache_ttl_days('TAGCLEANER_NEGATIVE_CACHE_DAYS', 1)) is None:
                return None
        except OSError:
            return None

    return result


def save_audd_result(
    fingerprint_data,
    result,
):
    if not _has_cache_identity(fingerprint_data):
        return False
    if not isinstance(result, dict):
        return False

    status = result.get("status")

    # Сохраняем только устойчивые ответы.
    # Ошибки API, сеть и лимиты нельзя делать
    # постоянным кэшем.
    if status not in {
        "FOUND",
        "NOT_FOUND",
    }:
        return False

    path = _cache_path(
        fingerprint_data
    )

    payload = {
        "version": CACHE_VERSION,
        "result": result,
    }

    try:
        atomic_write_json(path, payload)
    except Exception:
        return False

    return True


def _has_cache_identity(fingerprint_data):
    """A failed fingerprint must not give unrelated files one shared key."""
    payload = _normalize_fingerprint_data(fingerprint_data)
    try:
        duration = float(payload.get("duration"))
        return math.isfinite(duration) and duration > 0 and bool(payload.get("fingerprint"))
    except (TypeError, ValueError):
        return False


def extract_audd_error_info(result):
    result = result or {}
    error = result.get("error")

    if isinstance(error, dict):
        code = error.get("error_code")
        message = (
            error.get("error_message")
            or str(error)
        )
    else:
        code = result.get("error_code")
        message = (
            result.get("error_message")
            or error
            or ""
        )

    try:
        code = int(code)
    except Exception:
        pass

    return {
        "code": code,
        "message": str(message or ""),
    }


def is_audd_quota_error(result):
    info = extract_audd_error_info(
        result
    )

    code = info.get("code")
    message = (
        info.get("message")
        or ""
    ).lower()

    if code in {
        900,
        901,
        902,
        903,
    }:
        return True

    markers = (
        "limit was reached",
        "limit reached",
        "authorization failed",
        "quota",
        "rate limit",
    )

    return any(
        marker in message
        for marker in markers
    )


def audd_failure_category(result):
    """Keep service quota separate from transport and invalid credentials."""
    info = extract_audd_error_info(result)
    code = info['code']
    if code == 902:
        return 'quota_exhausted'
    if code == 901:
        return 'anonymous_limit'
    if code in {900, 903}:
        return 'authentication'
    if 'limit was reached' in info['message'].lower() or 'quota' in info['message'].lower():
        return 'quota_exhausted'
    return 'unknown'


def disable_audd_for_run(reason):
    global _AUDD_DISABLED_REASON
    global _AUDD_DISABLED_CATEGORY

    _AUDD_DISABLED_CATEGORY = audd_failure_category({'error': reason})

    _AUDD_DISABLED_REASON = (
        str(reason)
        if reason
        else "AudD временно отключён."
    )


def audd_disabled_reason():
    return _AUDD_DISABLED_REASON


def audd_service_state():
    return {'disabled': bool(_AUDD_DISABLED_REASON), 'category': _AUDD_DISABLED_CATEGORY}


def audd_is_disabled():
    return bool(
        _AUDD_DISABLED_REASON
    )


def reset_audd_circuit_breaker():
    global _AUDD_DISABLED_REASON
    global _AUDD_DISABLED_CATEGORY
    _AUDD_DISABLED_REASON = None
    _AUDD_DISABLED_CATEGORY = None
