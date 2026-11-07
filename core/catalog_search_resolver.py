import hashlib
import json
import re
import time
import threading
from pathlib import Path
from urllib.parse import quote_plus

import requests

from core.cache_utils import atomic_write_json, read_json_if_fresh, cache_ttl_days
from core.singleflight import cached_singleflight
from core.dns_recovery import DNSRecovery, is_dns_error
from core.network_settings import proxy_for_url
from core.naming import canonical_artist_credit_from_items
from core.audd_matcher import (
    artist_similarity,
    extract_version_markers,
    normalize_text,
    title_similarity,
)
from core.naming import canonical_album_name
from core.matcher import version_similarity
from core.duration_policy import AUTOMATIC_DURATION_TOLERANCE
from core.extra_catalogs import capability as extra_catalog_capability, search_discogs, search_lastfm


ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache" / "catalog_search"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_TIMEOUT = 20
MB_REQUEST_INTERVAL = 1.1
ITUNES_REQUEST_INTERVAL = 3.2
MAX_MB_RESULTS = 25
MAX_ITUNES_RESULTS = 25

_last_mb_request = 0.0
_last_itunes_request = 0.0
_itunes_request_lock = threading.RLock()
_itunes_dns = DNSRecovery('itunes.apple.com')


def itunes_dns_stats():
    return _itunes_dns.stats()

_session = requests.Session()
_session.headers.update({
    "User-Agent": "TagCleaner/0.6 (+catalog identity resolver)",
    "Accept": "application/json",
})

try:
    from core.musicbrainz_client import (
        BASE_URL as MB_BASE_URL,
        _request_json as _shared_mb_request_json,
    )
except Exception:
    MB_BASE_URL = "https://musicbrainz.org/ws/2"
    _shared_mb_request_json = None


NOISE_PREFIX_RE = re.compile(
    r"^(?:\[[^\]]+\]\s*|(?:dubstep|trap|edm|drum\s*&\s*bass|dnb)\s*[|:\-]\s*)+",
    re.IGNORECASE,
)

VIDEO_SUFFIX_RE = re.compile(
    r"\s*[\[(](?:official\s+)?(?:audio|video|music\s+video|visualizer|lyrics?|hd|hq)[^\])]*[\])]\s*$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Cache / HTTP
# ---------------------------------------------------------------------------

def _cache_path(provider, key):
    digest = hashlib.sha256(f"{provider}\n{key}".encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{provider}_{digest}.json"


def _load_cache(provider, key):
    path = _cache_path(provider, key)
    cached = read_json_if_fresh(path, ttl_days=cache_ttl_days("TAGCLEANER_CATALOG_CACHE_DAYS", 30))
    if not isinstance(cached, dict) or not isinstance(cached.get('results'),list) or cached.get("status") == "ERROR":
        return None
    if not cached.get("results"):
        if not cached.get('response_validated'):return None
        return read_json_if_fresh(path, ttl_days=cache_ttl_days("TAGCLEANER_NEGATIVE_CACHE_DAYS", 1))
    return cached


def _save_cache(provider, key, payload):
    atomic_write_json(_cache_path(provider, key), payload)


def _wait_interval(kind):
    global _last_mb_request, _last_itunes_request

    if kind == "musicbrainz":
        last = _last_mb_request
        interval = MB_REQUEST_INTERVAL
    else:
        last = _last_itunes_request
        interval = ITUNES_REQUEST_INTERVAL

    elapsed = time.monotonic() - last
    if elapsed < interval:
        time.sleep(interval - elapsed)


def _mark_request(kind):
    global _last_mb_request, _last_itunes_request
    if kind == "musicbrainz":
        _last_mb_request = time.monotonic()
    else:
        _last_itunes_request = time.monotonic()


def _request_json(url, params, kind):
    if kind == "musicbrainz" and _shared_mb_request_json is not None:
        return _shared_mb_request_json(url, params=params)

    # iTunes has its own conservative interval. Serialize only this provider;
    # MusicBrainz/source/AcoustID/ACRCloud continue concurrently.
    with _itunes_request_lock:
        direct = not proxy_for_url(url)
        dns = _itunes_dns.deferred() if direct else None
        if dns:
            raise RuntimeError(dns['error'])
        _wait_interval(kind)
        try:
            response = _session.get(
                url,
                params=params,
                headers={
                    "User-Agent": "TagCleaner/0.5 (+catalog identity resolver)",
                    "Accept": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            if direct and is_dns_error(error):
                _itunes_dns.failed()
                raise RuntimeError('DNS: адрес itunes.apple.com недоступен; проверка каталога не завершена.') from error
            raise
        finally:
            _mark_request(kind)
        response.raise_for_status()
        payload = response.json()
        _itunes_dns.succeeded()
        return payload



# ---------------------------------------------------------------------------
# Local identity
# ---------------------------------------------------------------------------

def _clean_title(value):
    value = str(value or "").strip()
    value = NOISE_PREFIX_RE.sub("", value).strip()

    previous = None
    while value and value != previous:
        previous = value
        value = VIDEO_SUFFIX_RE.sub("", value).strip()

    return value


FEATURE_CLAUSE_RE = re.compile(
    r"\s*[\(\[]\s*(?:feat(?:uring)?|ft)[\.,]?\s+[^\)\]]+[\)\]]",
    re.IGNORECASE,
)

FEATURE_NAME_RE = re.compile(
    r"[\(\[]\s*(?:feat(?:uring)?|ft)[\.,]?\s+([^\)\]]+)[\)\]]",
    re.IGNORECASE,
)


def _title_query_variants(value):
    """
    Варианты только для ПОИСКА. Финальная проверка всё равно сравнивает
    кандидата с исходным полным filename/title и duration.

    MusicBrainz часто хранит featured artist в artist-credit, а не в title:
      Get Up! (feat. Skrillex) [Tut Tut Child Remix]
    может называться:
      Get Up! (Tut Tut Child Remix)

    Поэтому убираем только bracketed feat/ft clause, не Remix/VIP/Edit.
    """
    original = _clean_title(value)
    if not original:
        return []

    values = [original]
    without_feature = FEATURE_CLAUSE_RE.sub(" ", original)
    without_feature = re.sub(r"\s+", " ", without_feature).strip()

    if without_feature and normalize_text(without_feature) != normalize_text(original):
        values.append(without_feature)

    result = []
    seen = set()
    for item in values:
        key = normalize_text(item)
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _feature_names(value):
    result = []
    for match in FEATURE_NAME_RE.finditer(str(value or "")):
        credit = match.group(1) or ""
        parts = re.split(
            r"\s*(?:,|&|\bx\b|\band\b|\bfeat\.?\b|\bft\.?\b)\s*",
            credit,
            flags=re.IGNORECASE,
        )
        result.extend(part.strip() for part in parts if part.strip())
    return result


def _feature_variant_supported(local_title, remote_title, remote_artist):
    feature_names = _feature_names(local_title)
    if not feature_names:
        return True

    # Если remote title сам сохраняет feat credit, обычный similarity решит.
    if _feature_names(remote_title):
        return True

    if not remote_artist:
        return False

    return all(
        artist_similarity(name, remote_artist) >= 70.0
        for name in feature_names
    )


def _filename_artist_title(file_path):
    stem = Path(file_path).stem.strip()
    if " - " not in stem:
        return "", _clean_title(stem)

    artist, title = stem.split(" - ", 1)
    return artist.strip(), _clean_title(title)


def _local_identity(file_path, data):
    filename_artist, filename_title = _filename_artist_title(file_path)

    tag_artist = str(data.get("artist") or "").strip()
    tag_title = _clean_title(data.get("title") or "")

    # Filename обычно переживает downloader/uploader metadata лучше тегов.
    # Теги используются как дополнительное доказательство, но query строим
    # прежде всего по имени файла, если оно имеет Artist - Title.
    artist = filename_artist or tag_artist
    title = filename_title or tag_title

    return {
        "artist": artist,
        "title": title,
        "filename_artist": filename_artist,
        "filename_title": filename_title,
        "tag_artist": tag_artist,
        "tag_title": tag_title,
        "tag_album": str(data.get("album") or "").strip(),
        "duration": _safe_float(data.get("duration")),
    }


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Search providers
# ---------------------------------------------------------------------------

def _lucene_quote(value):
    value = str(value or "")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def _format_mb_artist(recording):
    return canonical_artist_credit_from_items(
        recording.get("artist-credit") or []
    )


@cached_singleflight(lambda artist, title, refresh=False: (artist or "", title or "", refresh))
def search_musicbrainz(artist, title, refresh=False):
    if not title:
        return []

    query_parts = [f"recording:{_lucene_quote(title)}"]
    if artist:
        query_parts.append(f"artist:{_lucene_quote(artist)}")

    query = " AND ".join(query_parts)
    cache_key = query

    cached = None if refresh else _load_cache("musicbrainz", cache_key)
    if cached is not None:
        from core.artist_aliases import register_credit
        for candidate in cached.get("results") or []:
            register_credit((candidate.get("raw") or {}).get("artist-credit"))
        return cached.get("results") or []

    data = _request_json(
        f"{MB_BASE_URL}/recording",
        params={
            "query": query,
            "limit": MAX_MB_RESULTS,
            "fmt": "json",
        },
        kind="musicbrainz",
    )

    if not isinstance(data,dict) or not isinstance(data.get('recordings'),list) or data.get('error'):
        raise ValueError('MusicBrainz: неполный ответ поиска; отсутствие записи не подтверждено.')

    results = []
    from core.artist_aliases import register_credit
    for item in data.get("recordings") or []:
        register_credit(item.get("artist-credit"))
        results.append(
            {
                "provider": "musicbrainz",
                "recording_id": item.get("id"),
                "artist": _format_mb_artist(item),
                "title": item.get("title") or "",
                "duration": (
                    _safe_float(item.get("length")) / 1000.0
                    if _safe_float(item.get("length")) is not None
                    else None
                ),
                "provider_score": _safe_float(item.get("score")),
                "raw": item,
            }
        )

    _save_cache("musicbrainz", cache_key, {"query": query, "results": results,'response_validated':True})
    return results


@cached_singleflight(lambda artist, title,refresh=False: (artist or "", title or "",refresh))
def search_itunes(artist, title,refresh=False):
    if not title:
        return []

    term = " ".join(part for part in (artist, title) if part).strip()
    cache_key = term.casefold()

    cached = None if refresh else _load_cache("itunes", cache_key)
    if cached is not None:
        return cached.get("results") or []

    data = _request_json(
        "https://itunes.apple.com/search",
        params={
            "term": term,
            "media": "music",
            "entity": "song",
            "country": "US",
            "limit": 100 if refresh else MAX_ITUNES_RESULTS,
            "explicit": "Yes",
        },
        kind="itunes",
    )

    if not isinstance(data,dict) or not isinstance(data.get('results'),list) or data.get('errorMessage'):
        raise ValueError('iTunes: неполный ответ поиска; отсутствие записи не подтверждено.')

    results = []
    for item in data.get("results") or []:
        if item.get("kind") != "song":
            continue

        results.append(
            {
                "provider": "itunes",
                "track_id": item.get("trackId"),
                "collection_id": item.get("collectionId"),
                "artist": item.get("artistName") or "",
                "title": item.get("trackName") or "",
                "album": item.get("collectionName") or "",
                "duration": (
                    _safe_float(item.get("trackTimeMillis")) / 1000.0
                    if _safe_float(item.get("trackTimeMillis")) is not None
                    else None
                ),
                "release_date": item.get("releaseDate"),
                "track_number": item.get("trackNumber"),
                "track_count": item.get("trackCount"),
                "url": item.get("trackViewUrl"),
                "provider_score": None,
                "raw": item,
            }
        )

    _save_cache("itunes", cache_key, {"term": term, "results": results,'response_validated':True})
    return results


# ---------------------------------------------------------------------------
# Album evidence (только tiebreak между уже сильными identity)
# ---------------------------------------------------------------------------

def _album_key(value):
    return normalize_text(canonical_album_name(value or ""))


def _candidate_album_values(candidate):
    values = []

    album = candidate.get("album")
    if album:
        values.append(str(album))

    raw = candidate.get("raw") or {}
    for release in raw.get("releases") or []:
        title = release.get("title")
        if title:
            values.append(str(title))
        release_group = release.get("release-group") or {}
        group_title = release_group.get("title")
        if group_title:
            values.append(str(group_title))

    # порядок важен только для диагностики; дубликаты убираем
    result = []
    seen = set()
    for value in values:
        key = _album_key(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _album_match(local_album, candidate):
    local_key = _album_key(local_album)
    if not local_key:
        return False
    return any(_album_key(value) == local_key for value in _candidate_album_values(candidate))

# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------

def _duration_similarity(local_duration, remote_duration):
    if local_duration is None or remote_duration is None:
        return None, None

    diff = abs(float(local_duration) - float(remote_duration))

    if diff <= 2.0:
        score = 100.0
    elif diff <= 5.0:
        score = 95.0
    elif diff <= 10.0:
        score = 85.0
    elif diff <= 20.0:
        score = 65.0
    elif diff <= 30.0:
        score = 35.0
    else:
        score = 0.0

    return score, diff


def evaluate_catalog_candidate(local, candidate):
    artist = candidate.get("artist") or ""
    title = _clean_title(candidate.get("title") or "")

    artist_scores = []
    for value in (
        local.get("filename_artist"),
        local.get("tag_artist"),
        local.get("artist"),
    ):
        if value and artist:
            artist_scores.append(float(artist_similarity(value, artist)))

    title_scores = []
    for value in (
        local.get("filename_title"),
        local.get("tag_title"),
        local.get("title"),
    ):
        if not value or not title:
            continue

        title_scores.append(float(title_similarity(value, title)))

        # MusicBrainz часто переносит feat из title в artist-credit.
        # Сравниваем feature-stripped вариант только если remote Artist
        # действительно содержит этого featured исполнителя.
        if _feature_variant_supported(value, title, artist):
            for variant in _title_query_variants(value)[1:]:
                title_scores.append(float(title_similarity(variant, title)))

    artist_score = max(artist_scores) if artist_scores else 0.0
    title_score = max(title_scores) if title_scores else 0.0

    duration_score, duration_diff = _duration_similarity(
        local.get("duration"),
        candidate.get("duration"),
    )

    album_match = _album_match(
        local.get("tag_album"),
        candidate,
    )

    local_version_titles = [
        value
        for value in (
            local.get("filename_title"),
            local.get("tag_title"),
            local.get("title"),
        )
        if value and extract_version_markers(value)
    ]
    remote_markers = extract_version_markers(title)

    # Проверка версии должна быть симметричной. Раньше мы защищались только
    # от случая "локально Remix/VIP, а каталог вернул original". Из-за
    # этого обычный трек мог получить STRONG от случайного Remix-кандидата.
    # Кроме того, одного общего слова "remix" недостаточно: Ray Volpe
    # Remix и DEVAULT Remix — разные версии.
    version_conflict = False
    if local_version_titles:
        if not remote_markers:
            version_conflict = True
        else:
            version_checks = [
                version_similarity(local_title, title)
                for local_title in local_version_titles
            ]
            version_conflict = all(
                mismatch
                for _, mismatch in version_checks
            )
    elif remote_markers:
        version_conflict = True

    provider_score = candidate.get("provider_score")
    provider_score = float(provider_score) if provider_score is not None else 90.0

    if duration_score is None:
        overall = (
            0.50 * title_score
            + 0.40 * artist_score
            + 0.10 * provider_score
        )
    else:
        overall = (
            0.43 * title_score
            + 0.34 * artist_score
            + 0.18 * duration_score
            + 0.05 * provider_score
        )

    if version_conflict:
        status = "CONFLICT"
        reason = "Каталог нашёл другую версию трека: Remix/VIP/Edit/Bootleg не совпадает с локальной версией."
    elif (
        title_score >= 92.0
        and artist_score >= 80.0
        and (duration_diff is None or duration_diff <= AUTOMATIC_DURATION_TOLERANCE)
        and (not local_version_titles or title_score >= 94.0)
        and overall >= 88.0
    ):
        status = "STRONG"
        reason = "Каталожный Artist/Title хорошо совпадает; версия и длительность не конфликтуют."
    elif (
        title_score >= 82.0
        and artist_score >= 60.0
        and (duration_diff is None or duration_diff <= 30.0)
        and overall >= 72.0
    ):
        status = "PARTIAL"
        reason = "Каталог даёт похожий трек, но доказательств недостаточно для trusted identity."
    else:
        status = "CONFLICT"
        reason = "Каталожный результат недостаточно согласуется с filename/тегами/длительностью."

    if candidate.get('discovery_only') and status == 'STRONG':
        status = 'PARTIAL'
        reason = 'Дополнительный каталог: кандидат для проверки, без независимого подтверждения по аудио.'

    result = dict(candidate)
    result.update(
        {
            "status": status,
            "artist": artist or None,
            "title": title or None,
            "artist_score": round(artist_score, 2),
            "title_score": round(title_score, 2),
            "duration_score": (
                round(duration_score, 2)
                if duration_score is not None
                else None
            ),
            "duration_diff": (
                round(duration_diff, 2)
                if duration_diff is not None
                else None
            ),
            "album_match": bool(album_match),
            "candidate_albums": _candidate_album_values(candidate),
            "overall_score": round(overall, 2),
            "version_conflict": version_conflict,
            "reason": reason,
        }
    )
    result.pop("raw", None)
    return result


def _same_identity(a, b):
    if not a or not b:
        return False

    from core.identity_fusion import identities_match
    # Candidate agreement is stricter than discovery: a shared substring
    # cannot collapse two different recordings into one catalog identity.
    return identities_match(a, b)


def choose_catalog_result(results):
    usable = [
        item for item in results
        if item.get("status") in {"STRONG", "PARTIAL", "CONFLICT"}
    ]

    if not usable:
        return {
            "status": "NOT_FOUND",
            "best": None,
            "results": results,
            "reason": "Каталоги не вернули пригодных кандидатов.",
        }

    strong = [item for item in usable if item.get("status") == "STRONG"]
    strong.sort(
        key=lambda item: (
            bool(item.get("album_match")),
            item.get("overall_score", 0.0),
            -(item.get("duration_diff") if item.get("duration_diff") is not None else 9999.0),
        ),
        reverse=True,
    )

    if len(strong) >= 2:
        best = strong[0]
        disagreeing = [item for item in strong[1:] if not _same_identity(best, item)]
        if disagreeing:
            return {
                "status": "AMBIGUOUS",
                "best": best,
                "results": results,
                "reason": "Несколько сильных каталожных кандидатов указывают на разные записи.",
            }

    if strong:
        return {
            "status": "STRONG",
            "best": strong[0],
            "results": results,
            "reason": strong[0].get("reason"),
        }

    partial = [item for item in usable if item.get("status") == "PARTIAL"]
    partial.sort(key=lambda item: item.get("overall_score", 0.0), reverse=True)

    # Два независимых каталога с одной и той же PARTIAL identity могут вместе
    # повысить её до STRONG.
    for index, first in enumerate(partial):
        for second in partial[index + 1:]:
            if first.get('discovery_only') or second.get('discovery_only'):
                continue
            # Repeated text agreement cannot explain an audio length mismatch.
            if any(x.get('duration_diff') is not None and
                   x['duration_diff'] > AUTOMATIC_DURATION_TOLERANCE for x in (first, second)):
                continue
            if first.get("provider") == second.get("provider"):
                continue
            if _same_identity(first, second):
                best = first if first.get("overall_score", 0.0) >= second.get("overall_score", 0.0) else second
                upgraded = dict(best)
                upgraded["status"] = "STRONG"
                upgraded["reason"] = "Два независимых каталога подтверждают одну и ту же identity."
                return {
                    "status": "STRONG",
                    "best": upgraded,
                    "results": results,
                    "reason": upgraded["reason"],
                }

    if partial:
        return {
            "status": "PARTIAL",
            "best": partial[0],
            "results": results,
            "reason": partial[0].get("reason"),
        }

    return {
        "status": "CONFLICT",
        "best": max(usable, key=lambda item: item.get("overall_score", 0.0)),
        "results": results,
        "reason": "Каталоги вернули кандидатов, но они конфликтуют с локальной identity.",
    }


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------

def resolve_catalog_identity(file_path, data):
    local = _local_identity(file_path, data)

    if not local.get("title"):
        return {
            "status": "NOT_FOUND",
            "best": None,
            "results": [],
            "reason": "Недостаточно Artist/Title для каталожного поиска.",
            "local": local,
        }

    results = []
    errors = []
    seen_catalog_keys = set()

    def add_candidates(candidates):
        for candidate in candidates or []:
            key = (
                candidate.get("provider"),
                candidate.get("recording_id") or candidate.get("track_id"),
                normalize_text(candidate.get("artist") or ""),
                normalize_text(candidate.get("title") or ""),
            )
            if key in seen_catalog_keys:
                continue
            seen_catalog_keys.add(key)
            results.append(evaluate_catalog_candidate(local, candidate))

    # 1) MusicBrainz — сначала точный Artist + Title.
    try:
        add_candidates(
            search_musicbrainz(local.get("artist"), local.get("title"))
        )
    except Exception as error:
        errors.append({"provider": "musicbrainz", "error": str(error)})

    selected = choose_catalog_result(results)

    # 2) Если exact query не дал STRONG, пробуем MusicBrainz только по Title.
    # Это важно для старых псевдонимов/credit-вариантов и коллабораций, где
    # локальный Artist записан иначе. Кандидат всё равно обязан пройти нашу
    # локальную artist/title/duration проверку, так что поиск становится шире,
    # а порог доверия — нет.
    if selected.get("status") != "STRONG" and local.get("artist"):
        try:
            add_candidates(
                search_musicbrainz("", local.get("title"))
            )
        except Exception as error:
            errors.append({"provider": "musicbrainz-title-only", "error": str(error)})

        selected = choose_catalog_result(results)

    # 3) Если мешает только оформление featured artist, пробуем более
    # канонический query-вариант. Порог принятия кандидата НЕ снижается:
    # evaluate_catalog_candidate всё ещё проверяет исходный полный title,
    # version marker, Artist и duration.
    if selected.get("status") != "STRONG":
        variants = _title_query_variants(local.get("title"))
        for variant in variants[1:]:
            try:
                add_candidates(
                    search_musicbrainz(local.get("artist"), variant)
                )
            except Exception as error:
                errors.append({
                    "provider": "musicbrainz-variant",
                    "error": str(error),
                })

            selected = choose_catalog_result(results)
            if selected.get("status") == "STRONG":
                break

            try:
                add_candidates(
                    search_musicbrainz("", variant)
                )
            except Exception as error:
                errors.append({
                    "provider": "musicbrainz-variant-title-only",
                    "error": str(error),
                })

            selected = choose_catalog_result(results)
            if selected.get("status") == "STRONG":
                break

    if selected.get("status") != "STRONG":
        from core.artist_aliases import search_aliases
        for alias in search_aliases(local.get("artist")):
            if normalize_text(alias) == normalize_text(local.get("artist")):
                continue
            try:
                add_candidates(search_musicbrainz(alias, local.get("title")))
            except Exception as error:
                errors.append({"provider": "musicbrainz-verified-alias", "error": str(error)})
            selected = choose_catalog_result(results)
            if selected.get("status") == "STRONG":
                break

    # 4) Если MusicBrainz всё ещё не дал сильное точное совпадение, Apple.
    if selected.get("status") != "STRONG":
        try:
            add_candidates(
                search_itunes(local.get("artist"), local.get("title"))
            )
        except Exception as error:
            errors.append({"provider": "itunes", "error": str(error)})

        selected = choose_catalog_result(results)

    # Apple full-credit terms often miss collaborations, aliases and featured
    # names. Expand discovery too, while validating against the unchanged
    # complete local identity in add_candidates/evaluate_catalog_candidate.
    if selected.get("status") != "STRONG":
        for variant in _title_query_variants(local.get("title")):
            if not local.get("artist") and normalize_text(variant) == normalize_text(local.get("title")):
                continue  # The initial Apple query was already title-only.
            try:
                add_candidates(search_itunes("", variant))
            except Exception as error:
                errors.append({"provider": "itunes-title-only", "error": str(error)})
            selected = choose_catalog_result(results)
            if selected.get("status") == "STRONG":
                break

    # Labs indexes MusicBrainz data too: optional discovery, never a second
    # independent vote and never automatic evidence of an audio match.
    if selected.get('status') != 'STRONG':
        from core.listenbrainz_catalog import enabled as labs_enabled,search as labs_search
        if labs_enabled():
            try:add_candidates(labs_search(local.get('artist'),local.get('title')))
            except Exception:errors.append({'provider':'listenbrainz','error':'optional_catalog_unavailable'})
            selected=choose_catalog_result(results)

    # New sources expand discovery without acquiring an independent vote in
    # the trusted identity consensus. Always score against ORIGINAL local data.
    extra_searches = {}
    if selected.get('status') != 'STRONG':
        for provider, search in (('discogs', search_discogs), ('lastfm', search_lastfm)):
            if not extra_catalog_capability()[provider]['enabled']:
                continue
            try:
                batch = search(local.get('artist'), local.get('title'))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                # A broken optional cache/response must not erase candidates
                # already obtained from the other providers. No raw exception
                # text here: request exceptions can contain credentials.
                errors.append({'provider': provider, 'error': 'optional_catalog_read_error'})
                continue
            add_candidates(batch.get('results'))
            extra_searches[provider] = {
                'candidates': len(batch.get('results') or []),
                'search_limited': bool(batch.get('search_limited')),
            }
            errors.extend({'provider': provider, 'error': error} for error in batch.get('errors') or [])

        # Query corrections are hints, never replacements of filename/tags.
        # Keep all candidates in the result/checkpoint, but bound follow-up
        # searches to four distinct plausible identities per file.
        hints = sorted((x for x in results if x.get('discovery_only') and x.get('status') == 'PARTIAL'),
                       key=lambda x: x.get('overall_score', 0), reverse=True)
        seen_queries = {(normalize_text(local.get('artist') or ''), normalize_text(local.get('title') or ''))}
        queries = 0
        for hint in hints:
            key = (normalize_text(hint.get('artist') or ''), normalize_text(hint.get('title') or ''))
            if key in seen_queries:
                continue
            seen_queries.add(key)
            queries += 1
            if queries > 4:
                extra_searches['followup_limited'] = True
                break
            for provider, search in (('musicbrainz', search_musicbrainz), ('itunes', search_itunes)):
                try:
                    add_candidates(search(hint.get('artist'), hint.get('title')))
                except Exception as error:
                    errors.append({'provider': provider + '-catalog-hint', 'error': str(error)})
            # Evaluate all selected hints to preserve competing identities.
        selected = choose_catalog_result(results)

    selected['extra_catalog_searches'] = extra_searches
    selected["local"] = local
    selected["errors"] = errors

    if selected.get("status") == "NOT_FOUND" and errors and not results:
        selected["status"] = "ERROR"
        selected["reason"] = "Каталожный поиск не выполнен из-за сетевых/API ошибок."

    return selected
