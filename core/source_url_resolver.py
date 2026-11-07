import hashlib
import html
import json
import re
import time
import threading
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

from core.cache_utils import atomic_write_json, read_json_if_fresh, cache_ttl_days
from core.singleflight import cached_singleflight
from core.audd_matcher import (
    artist_similarity,
    extract_version_markers,
    normalize_text,
    title_similarity,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / "cache" / "source_url"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_TIMEOUT = 12
MAX_SOURCE_URLS = 6

_session_local = threading.local()


def _get_session():
    session = getattr(_session_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "TagCleaner/0.7 (+parallel source resolver)",
        })
        _session_local.session = session
    return session

URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)

TRAILING_URL_PUNCTUATION = ".,;:!?)]}>"

PROVIDER_PRIORITY = {
    "spotify": 1.00,
    "soundcloud": 1.00,
    "bandcamp": 1.00,
    "youtube": 0.95,
    "generic": 0.70,
}

FIELD_PRIORITY_MARKERS = (
    "source_url",
    "source",
    "woas",
    "purl",
    "wors",
    "url",
)

WEAK_FIELD_MARKERS = (
    "comment",
    "description",
    "synopsis",
)

VIDEO_SUFFIX_RE = re.compile(
    r"\s*[\[(](?:official\s+)?(?:audio|video|music\s+video|visualizer|lyric\s+video|lyrics|hd|hq)[^\])]*[\])]\s*$",
    re.IGNORECASE,
)


class _MetaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self._inside_title = False
        self._title_parts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        tag = tag.lower()

        if tag == "meta":
            key = (
                attrs.get("property")
                or attrs.get("name")
                or ""
            ).strip().lower()
            value = (attrs.get("content") or "").strip()
            if key and value and key not in self.meta:
                self.meta[key] = value

        elif tag == "title":
            self._inside_title = True

    def handle_endtag(self, tag):
        if tag.lower() == "title":
            self._inside_title = False

    def handle_data(self, data):
        if self._inside_title:
            self._title_parts.append(data)

    @property
    def title(self):
        return html.unescape(" ".join(self._title_parts)).strip()


def _cache_path(url):
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _load_cache(url):
    cached = read_json_if_fresh(_cache_path(url), ttl_days=cache_ttl_days("TAGCLEANER_SOURCE_CACHE_DAYS", 30))
    if isinstance(cached, dict) and cached.get("status") != "ERROR":
        return cached
    return None


def _save_cache(url, payload):
    atomic_write_json(_cache_path(url), payload)


def _clean_url(value):
    value = str(value or "").strip()
    while value and value[-1] in TRAILING_URL_PUNCTUATION:
        value = value[:-1]
    return value


def classify_provider(url):
    host = (urlparse(url).hostname or "").casefold()

    if host == "spotify.com" or host.endswith(".spotify.com") or host == "spotify.link":
        return "spotify"
    if host == "soundcloud.com" or host.endswith(".soundcloud.com"):
        return "soundcloud"
    if host == "youtube.com" or host.endswith(".youtube.com") or host == "youtu.be":
        return "youtube"
    if host == "bandcamp.com" or host.endswith(".bandcamp.com"):
        return "bandcamp"
    return "generic"


def _field_priority(raw_key, logical):
    haystack = f"{raw_key or ''} {logical or ''}".casefold()

    if any(marker in haystack for marker in FIELD_PRIORITY_MARKERS):
        return 1.00
    if any(marker in haystack for marker in WEAK_FIELD_MARKERS):
        return 0.80
    return 0.70


def extract_source_references(data):
    """
    Берёт URL именно из tag_findings со статусом SOURCE_REFERENCE.
    Это важнее случайного URL из description: scanner/auditor уже решил,
    что поле похоже на provenance/source.
    """
    references = []
    seen = set()

    for finding in data.get("tag_findings") or []:
        if finding.get("status") != "SOURCE_REFERENCE":
            continue

        raw_key = finding.get("raw_key") or ""
        logical = finding.get("logical") or ""
        field_priority = _field_priority(raw_key, logical)

        for value in finding.get("values") or []:
            for match in URL_RE.findall(str(value)):
                url = _clean_url(match)
                if not url or url in seen:
                    continue

                seen.add(url)
                provider = classify_provider(url)
                references.append(
                    {
                        "url": url,
                        "provider": provider,
                        "raw_key": raw_key,
                        "logical": logical,
                        "field_priority": field_priority,
                    }
                )

    references.sort(
        key=lambda item: (
            item.get("field_priority", 0.0),
            PROVIDER_PRIORITY.get(item.get("provider"), 0.0),
        ),
        reverse=True,
    )

    return references[:MAX_SOURCE_URLS]


def _request_json(url, params=None):
    response = _get_session().get(
        url,
        params=params,
        headers={
            "User-Agent": "TagCleaner/0.4 (+local metadata resolver)",
            "Accept": "application/json,text/plain,*/*",
        },
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.json(), response.url


def _request_html(url):
    response = _get_session().get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 TagCleaner/0.4",
            "Accept": "text/html,application/xhtml+xml",
        },
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()
    return response.text, response.url


def _fetch_oembed(reference):
    url = reference["url"]
    provider = reference["provider"]

    if provider == "youtube":
        payload, final_url = _request_json(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
        )
    elif provider == "spotify":
        payload, final_url = _request_json(
            "https://open.spotify.com/oembed",
            params={"url": url},
        )
    elif provider == "soundcloud":
        payload, final_url = _request_json(
            "https://soundcloud.com/oembed",
            params={"url": url, "format": "json"},
        )
    else:
        raise ValueError("oEmbed не поддерживается для этого provider")

    return {
        "provider": provider,
        "url": url,
        "final_url": final_url,
        "raw_title": payload.get("title") or "",
        "author": payload.get("author_name") or "",
        "description": payload.get("description") or "",
        "thumbnail_url": payload.get("thumbnail_url"),
        "fetch_method": "oembed",
    }


def _fetch_html_metadata(reference):
    url = reference["url"]
    provider = reference["provider"]
    page, final_url = _request_html(url)

    parser = _MetaParser()
    parser.feed(page)
    meta = parser.meta

    raw_title = (
        meta.get("og:title")
        or meta.get("twitter:title")
        or parser.title
        or ""
    )

    author = (
        meta.get("music:musician")
        or meta.get("author")
        or ""
    )

    description = (
        meta.get("og:description")
        or meta.get("description")
        or meta.get("twitter:description")
        or ""
    )

    return {
        "provider": provider,
        "url": url,
        "final_url": final_url,
        "raw_title": html.unescape(raw_title).strip(),
        "author": html.unescape(author).strip(),
        "description": html.unescape(description).strip(),
        "thumbnail_url": meta.get("og:image") or meta.get("twitter:image"),
        "fetch_method": "html_meta",
    }


@cached_singleflight(lambda reference: reference["url"])
def fetch_source_metadata(reference):
    url = reference["url"]
    cached = _load_cache(url)
    if cached is not None:
        cached = dict(cached)
        cached["cached"] = True
        return cached

    last_error = None

    for attempt in range(2):
        try:
            provider = reference["provider"]
            if attempt == 0 and provider in {"youtube", "spotify", "soundcloud"}:
                result = _fetch_oembed(reference)
            else:
                result = _fetch_html_metadata(reference)

            result["cached"] = False
            _save_cache(url, result)
            return result

        except Exception as error:
            last_error = error
            if attempt == 0:
                time.sleep(0.8)

    return {
        "provider": reference.get("provider"),
        "url": url,
        "status": "ERROR",
        "error": str(last_error or "Неизвестная ошибка source URL"),
    }


def _strip_video_suffixes(title):
    value = str(title or "").strip()
    previous = None
    while value and value != previous:
        previous = value
        value = VIDEO_SUFFIX_RE.sub("", value).strip()
    return value


def _parse_artist_title(metadata):
    provider = metadata.get("provider") or "generic"
    raw_title = _strip_video_suffixes(metadata.get("raw_title") or "")
    author = str(metadata.get("author") or "").strip()

    artist = ""
    title = raw_title

    # SoundCloud oEmbed обычно возвращает "Track by Artist".
    if provider == "soundcloud" and re.search(r"\sby\s[^()\[\]]+$", raw_title):
        left, right = raw_title.rsplit(" by ", 1)
        if left.strip() and right.strip():
            title = left.strip()
            artist = right.strip()

    # Bandcamp/страницы часто имеют "Track | Artist".
    elif provider == "bandcamp" and " | " in raw_title:
        left, right = raw_title.split(" | ", 1)
        if left.strip() and right.strip():
            title = left.strip()
            artist = right.strip()

    # YouTube: Artist - Title — самый полезный распространённый формат.
    elif provider == "youtube":
        for separator in (" - ", " – ", " — "):
            if separator in raw_title:
                left, right = raw_title.split(separator, 1)
                if left.strip() and right.strip():
                    artist = left.strip()
                    title = right.strip()
                    break

    # Generic page: пробуем Artist - Title, но осторожно.
    elif provider == "generic":
        for separator in (" - ", " – ", " — "):
            if separator in raw_title:
                left, right = raw_title.split(separator, 1)
                if left.strip() and right.strip():
                    artist = left.strip()
                    title = right.strip()
                    break

    # author_name YouTube — это канал, не всегда артист. Не подменяем им
    # artist автоматически, но сохраняем как отдельное доказательство.
    return {
        "artist": artist,
        "title": title.strip(),
        "author": author,
        "raw_title": raw_title,
    }


def _filename_artist_title(file_path):
    stem = Path(file_path).stem
    if " - " not in stem:
        return "", stem
    artist, title = stem.split(" - ", 1)
    return artist.strip(), title.strip()


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value if item is not None)
    return str(value)


def evaluate_source_candidate(file_path, data, reference, metadata):
    parsed = _parse_artist_title(metadata)

    source_artist = parsed["artist"]
    source_title = parsed["title"]
    filename_artist, filename_title = _filename_artist_title(file_path)

    local_artist = _as_text(data.get("artist"))
    local_title = _as_text(data.get("title"))

    title_scores = [
        title_similarity(source_title, value)
        for value in (local_title, filename_title)
        if source_title and value
    ]
    title_score = max(title_scores) if title_scores else 0.0

    artist_scores = [
        artist_similarity(source_artist, value)
        for value in (local_artist, filename_artist)
        if source_artist and value
    ]
    artist_score = max(artist_scores) if artist_scores else None

    local_markers = set()
    local_markers |= extract_version_markers(local_title)
    local_markers |= extract_version_markers(filename_title)
    source_markers = extract_version_markers(source_title)

    from core.versioning import local_version_conflict
    version_conflict = local_version_conflict([local_title, filename_title], source_title)

    provider = reference.get("provider") or "generic"
    field_priority = float(reference.get("field_priority") or 0.0)
    provider_priority = PROVIDER_PRIORITY.get(provider, 0.70)
    provenance_score = 100.0 * min(field_priority, provider_priority)

    if artist_score is None:
        overall = 0.78 * title_score + 0.22 * provenance_score
    else:
        overall = 0.50 * title_score + 0.35 * artist_score + 0.15 * provenance_score

    if version_conflict:
        status = "CONFLICT"
        reason = "Source URL указывает на другую версию трека (Remix/VIP/Edit/Bootleg)."

    elif artist_score is not None and title_score >= 90.0 and artist_score >= 72.0:
        status = "STRONG"
        reason = "Метаданные source URL хорошо совпадают с Artist/Title файла."

    elif (
        artist_score is None
        and provider in {"spotify", "soundcloud", "bandcamp"}
        and field_priority >= 0.95
        and title_score >= 92.0
    ):
        status = "STRONG"
        reason = "Прямой source URL подтверждает название трека; Artist в oEmbed не раскрыт."

    elif title_score >= 78.0 and (artist_score is None or artist_score >= 50.0):
        status = "PARTIAL"
        reason = "Source URL частично согласуется с filename/тегами, но подтверждения недостаточно."

    else:
        status = "CONFLICT"
        reason = "Метаданные source URL плохо согласуются с filename/текущими тегами."

    return {
        "status": status,
        "provider": provider,
        "url": reference.get("url"),
        "raw_key": reference.get("raw_key"),
        "logical": reference.get("logical"),
        "source_artist": source_artist or None,
        "source_title": source_title or None,
        "source_author": parsed.get("author") or None,
        "raw_title": parsed.get("raw_title") or None,
        "artist_score": round(artist_score, 2) if artist_score is not None else None,
        "title_score": round(title_score, 2),
        "provenance_score": round(provenance_score, 2),
        "overall_score": round(overall, 2),
        "version_conflict": version_conflict,
        "local_version_markers": sorted(local_markers),
        "source_version_markers": sorted(source_markers),
        "reason": reason,
        "fetch_method": metadata.get("fetch_method"),
        "cached": bool(metadata.get("cached")),
    }


def _identity_key(result):
    return (
        normalize_text(result.get("source_artist") or ""),
        normalize_text(result.get("source_title") or ""),
    )


def choose_source_result(results):
    if not results:
        return {
            "status": "NO_SOURCE",
            "best": None,
            "results": [],
            "reason": "В тегах нет пригодных source URL.",
        }

    successful = [
        result for result in results
        if result.get("status") in {"STRONG", "PARTIAL", "CONFLICT"}
    ]

    if not successful:
        return {
            "status": "ERROR",
            "best": None,
            "results": results,
            "reason": "Source URL найдены, но получить метаданные не удалось.",
        }

    strong = [r for r in successful if r.get("status") == "STRONG"]

    # Несколько сильных URL, которые ведут к разным title, нельзя склеивать.
    strong_title_keys = {
        normalize_text(r.get("source_title") or "")
        for r in strong
        if r.get("source_title")
    }

    artist_conflict = any(
        left.get("source_artist") and right.get("source_artist")
        and artist_similarity(left["source_artist"], right["source_artist"]) < 90
        for index, left in enumerate(strong) for right in strong[index + 1:]
    )
    if len(strong_title_keys) > 1 or artist_conflict:
        strong_sorted = sorted(
            strong,
            key=lambda r: r.get("overall_score", 0.0),
            reverse=True,
        )
        return {
            "status": "AMBIGUOUS",
            "best": strong_sorted[0] if strong_sorted else None,
            "results": results,
            "reason": "Несколько source URL дают разные сильные identity-кандидаты.",
        }

    rank = {"STRONG": 3, "PARTIAL": 2, "CONFLICT": 1}
    best = max(
        successful,
        key=lambda r: (
            rank.get(r.get("status"), 0),
            r.get("overall_score", 0.0),
        ),
    )

    return {
        "status": best.get("status") or "PARTIAL",
        "best": best,
        "results": results,
        "reason": best.get("reason"),
    }


def resolve_source_identity(file_path, data):
    references = extract_source_references(data)

    if not references:
        return {
            "status": "NO_SOURCE",
            "best": None,
            "results": [],
            "references": [],
            "reason": "В тегах нет пригодных source URL.",
        }

    results = []

    for reference in references:
        metadata = fetch_source_metadata(reference)

        if metadata.get("status") == "ERROR":
            results.append(
                {
                    "status": "ERROR",
                    "provider": reference.get("provider"),
                    "url": reference.get("url"),
                    "raw_key": reference.get("raw_key"),
                    "logical": reference.get("logical"),
                    "error": metadata.get("error"),
                }
            )
            continue

        results.append(
            evaluate_source_candidate(
                file_path=file_path,
                data=data,
                reference=reference,
                metadata=metadata,
            )
        )

    selected = choose_source_result(results)
    selected["references"] = references
    return selected
