import re
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz


VERSION_MARKERS = {
    "remix",
    "vip",
    "edit",
    "bootleg",
    "flip",
    "rework",
    "remaster",
    "remastered",
    "instrumental",
}

ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|&|\bx\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b)\s*",
    re.IGNORECASE,
)


def normalize_text(value):
    if value is None:
        return ""

    value = unicodedata.normalize(
        "NFKC",
        str(value),
    )

    value = (
        value.replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
        .replace("–", "-")
        .replace("—", "-")
    )

    value = value.casefold()

    # Скобки оставляем только как разделители: содержимое важно,
    # особенно для Remix/VIP/Edit.
    value = re.sub(
        r"[\[\]{}()]",
        " ",
        value,
    )

    value = re.sub(
        r"[^\w'!+]+",
        " ",
        value,
        flags=re.UNICODE,
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def normalize_artist_name(value):
    return normalize_text(value)


def split_artist_names(value):
    if not value:
        return []

    parts = ARTIST_SPLIT_RE.split(
        str(value)
    )

    result = []

    for part in parts:
        normalized = normalize_artist_name(
            part
        )

        if normalized:
            result.append(normalized)

    return result


def artist_similarity(a, b):
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)

    if not a_norm or not b_norm:
        return 0.0

    from core.artist_aliases import same_artist
    if same_artist(a, b):
        return 100.0

    direct = float(
        fuzz.WRatio(
            a_norm,
            b_norm,
        )
    )

    a_parts = set(
        split_artist_names(a)
    )
    b_parts = set(
        split_artist_names(b)
    )

    if not a_parts or not b_parts:
        return direct

    intersection = len(
        a_parts & b_parts
    )

    union = len(
        a_parts | b_parts
    )

    jaccard = (
        100.0 * intersection / union
        if union
        else 0.0
    )

    # Если один credit является подмножеством другого,
    # это часто просто различие в feat/collab оформлении.
    subset_score = 0.0

    if (
        a_parts <= b_parts
        or b_parts <= a_parts
    ):
        subset_score = (
            100.0
            * min(
                len(a_parts),
                len(b_parts),
            )
            / max(
                len(a_parts),
                len(b_parts),
            )
        )

    return max(
        direct,
        jaccard,
        subset_score,
    )


def title_similarity(a, b):
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)

    if not a_norm or not b_norm:
        return 0.0

    return float(
        fuzz.WRatio(
            a_norm,
            b_norm,
        )
    )


def extract_version_markers(value):
    from core.versioning import version_kinds
    return version_kinds(value)


def _filename_artist_title(file_path):
    stem = Path(
        file_path
    ).stem

    if " - " not in stem:
        return "", stem

    artist, title = stem.split(
        " - ",
        1,
    )

    return (
        artist.strip(),
        title.strip(),
    )


def _unique_strings(values):
    result = []
    seen = set()

    for value in values:
        if not value:
            continue

        text = str(value).strip()

        if not text:
            continue

        key = normalize_text(text)

        if not key or key in seen:
            continue

        seen.add(key)
        result.append(text)

    return result


def _best_pair(
    local_values,
    remote_values,
    similarity_func,
):
    best = {
        "score": 0.0,
        "local": None,
        "remote": None,
    }

    for local_value in local_values:
        for remote_value in remote_values:
            score = float(
                similarity_func(
                    local_value,
                    remote_value,
                )
            )

            if score > best["score"]:
                best = {
                    "score": score,
                    "local": local_value,
                    "remote": remote_value,
                }

    return best


def _get_local_isrc(data):
    for key in (
        "isrc",
        "ISRC",
    ):
        value = data.get(key)

        if value:
            return str(value).strip()

    return ""


def evaluate_audd_match(
    file_path,
    data,
    audd_result,
):
    """
    Проверяет, согласуется ли найденный AudD трек
    с filename и текущими тегами.

    Возвращает:
        STRONG   — AudD хорошо подтверждается локальными данными.
        PARTIAL  — есть заметное согласие, но недостаточно для доверия.
        CONFLICT — AudD, скорее всего, распознал не тот трек/фрагмент.

    Даже STRONG пока НЕ означает SAFE-write.
    """

    data = data or {}
    audd_result = audd_result or {}

    filename_artist, filename_title = (
        _filename_artist_title(
            file_path
        )
    )

    spotify = (
        audd_result.get("spotify")
        or {}
    )

    apple = (
        audd_result.get("apple_music")
        or {}
    )

    spotify_artists = (
        spotify.get("artists")
        or []
    )

    local_artists = _unique_strings(
        [
            data.get("artist"),
            filename_artist,
        ]
    )

    local_titles = _unique_strings(
        [
            data.get("title"),
            filename_title,
        ]
    )

    remote_artists = _unique_strings(
        [
            audd_result.get("artist"),
            (
                " & ".join(
                    spotify_artists
                )
                if spotify_artists
                else None
            ),
            apple.get("artist"),
        ]
    )

    remote_titles = _unique_strings(
        [
            audd_result.get("title"),
            apple.get("title"),
        ]
    )

    artist_match = _best_pair(
        local_artists,
        remote_artists,
        artist_similarity,
    )

    title_match = _best_pair(
        local_titles,
        remote_titles,
        title_similarity,
    )

    artist_score = (
        artist_match["score"]
    )

    title_score = (
        title_match["score"]
    )

    local_version_markers = set()

    for value in local_titles:
        local_version_markers |= (
            extract_version_markers(
                value
            )
        )

    remote_version_markers = set()

    for value in remote_titles:
        remote_version_markers |= (
            extract_version_markers(
                value
            )
        )

    from core.versioning import local_version_conflict
    version_conflict = local_version_conflict(local_titles, title_match.get("remote") or audd_result.get("title"))

    local_isrc = _get_local_isrc(
        data
    )

    remote_isrc = (
        audd_result.get("isrc")
        or ""
    )

    isrc_exact = bool(
        local_isrc
        and remote_isrc
        and normalize_text(local_isrc)
        == normalize_text(remote_isrc)
    )

    overall_score = round(
        (
            0.45 * artist_score
            + 0.55 * title_score
        ),
        2,
    )

    if isrc_exact and not version_conflict:
        status = "STRONG"
        reason = (
            "ISRC AudD совпадает с локальным ISRC."
        )

    elif version_conflict:
        status = "CONFLICT"
        reason = (
            "Версия трека конфликтует: filename/теги явно "
            "указывают Remix/VIP/Edit, а AudD подтверждает "
            "другой тип версии."
        )

    elif (
        title_score >= 88.0
        and artist_score >= 72.0
    ) or (
        title_score >= 82.0
        and artist_score >= 85.0
    ):
        status = "STRONG"
        reason = (
            "Artist и Title AudD хорошо согласуются "
            "с filename/текущими тегами."
        )

    elif (
        title_score >= 72.0
        and artist_score >= 55.0
    ):
        status = "PARTIAL"
        reason = (
            "Есть заметное совпадение Artist/Title, "
            "но оно недостаточно сильное для доверия."
        )

    else:
        status = "CONFLICT"
        reason = (
            "Artist/Title AudD плохо согласуются "
            "с filename и текущими тегами."
        )

    return {
        "status": status,
        "overall_score": overall_score,
        "artist_score": round(
            artist_score,
            2,
        ),
        "title_score": round(
            title_score,
            2,
        ),
        "artist_match": artist_match,
        "title_match": title_match,
        "version_conflict": (
            version_conflict
        ),
        "local_version_markers": sorted(
            local_version_markers
        ),
        "remote_version_markers": sorted(
            remote_version_markers
        ),
        "local_isrc": local_isrc or None,
        "remote_isrc": remote_isrc or None,
        "isrc_exact": isrc_exact,
        "reason": reason,
    }
