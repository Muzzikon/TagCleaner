import re
from pathlib import Path

from core.audd_matcher import (
    normalize_text,
    split_artist_names,
)
from core.musicbrainz_client import (
    format_artist_credit,
)


FEAT_RE = re.compile(
    r"(?:\bfeat\.?\b|\bft\.?\b|\bfeaturing\b)\s+([^\]\[()]+)",
    re.IGNORECASE,
)


def _filename_artist_title(file_path):
    stem = Path(file_path).stem

    if " - " not in stem:
        return "", stem

    artist, title = stem.split(" - ", 1)
    return artist.strip(), title.strip()


def _clean_credit_text(value):
    if not value:
        return ""

    return (
        str(value)
        .replace("/", " & ")
        .replace(";", " & ")
        .strip()
    )


def _artist_set(value):
    value = _clean_credit_text(value)

    if not value:
        return set()

    return {
        normalize_text(part)
        for part in split_artist_names(value)
        if normalize_text(part)
    }


def _featured_artist_set(title):
    if not title:
        return set()

    result = set()

    for match in FEAT_RE.finditer(str(title)):
        result |= _artist_set(match.group(1))

    return result


def _spotify_artist_set(audd_result):
    spotify = (audd_result or {}).get("spotify") or {}
    artists = spotify.get("artists") or []

    result = set()

    for artist in artists:
        result |= _artist_set(artist)

    return result


def _apple_artist_set(audd_result):
    apple = (audd_result or {}).get("apple_music") or {}

    result = _artist_set(apple.get("artist"))
    result |= _featured_artist_set(apple.get("title"))

    return result


def _musicbrainz_artist_set(recording):
    return _artist_set(
        format_artist_credit(recording or {})
    )


def _display_name_map(source_values):
    result = {}

    for source in source_values.values():
        for normalized, display in source.items():
            result.setdefault(normalized, display)

    return result


def _source_map_from_values(values):
    result = {}

    for display in values:
        if not display:
            continue

        cleaned = _clean_credit_text(display)

        for part in split_artist_names(cleaned):
            normalized = normalize_text(part)

            if normalized:
                result.setdefault(
                    normalized,
                    str(part).strip(),
                )

    return result


def evaluate_credit_consensus(
    file_path,
    data,
    audd_result,
    musicbrainz_recording,
):
    """
    Сравнивает user-facing Artist credit между источниками.

    Identity трека и artist credit — разные задачи. Даже если ISRC
    однозначно вывел на MusicBrainz Recording, эта функция проверяет,
    не теряет ли MusicBrainz заметного участника, которого подтверждают
    несколько других источников.

    Статусы:
        CONSENSUS — существенных противоречий нет.
        PARTIAL   — данных недостаточно или MB содержит неподтверждённый
                    дополнительный credit.
        CONFLICT  — минимум один исполнитель подтверждается >=2
                    не-MusicBrainz источниками, но отсутствует в MB credit.
    """

    data = data or {}
    audd_result = audd_result or {}
    musicbrainz_recording = musicbrainz_recording or {}

    filename_artist, filename_title = _filename_artist_title(
        file_path
    )

    spotify = audd_result.get("spotify") or {}
    apple = audd_result.get("apple_music") or {}

    source_values = {
        "filename": _source_map_from_values([
            filename_artist,
        ]),
        "tags": _source_map_from_values([
            data.get("artist"),
        ]),
        "audd": _source_map_from_values([
            audd_result.get("artist"),
        ]),
        "spotify": _source_map_from_values(
            spotify.get("artists") or []
        ),
        "apple": _source_map_from_values([
            apple.get("artist"),
        ]),
        "musicbrainz": _source_map_from_values([
            format_artist_credit(
                musicbrainz_recording
            ),
        ]),
    }

    # Featured artists часто живут не в Artist, а в Title.
    # Учитываем их как часть credit, но намеренно НЕ вытаскиваем
    # remix-автора из "(... Remix)": ремиксер не всегда является
    # recording artist в MusicBrainz.
    title_sources = {
        "filename": filename_title,
        "tags": data.get("title"),
        "audd": audd_result.get("title"),
        "apple": apple.get("title"),
    }

    for source_name, title in title_sources.items():
        for artist in _featured_artist_set(title):
            source_values[source_name].setdefault(
                artist,
                artist,
            )

    display_names = _display_name_map(
        source_values
    )

    mb_artists = set(
        source_values["musicbrainz"].keys()
    )

    non_mb_sources = (
        "filename",
        "tags",
        "audd",
        "spotify",
        "apple",
    )

    support_counts = {}
    supported_by = {}

    for source_name in non_mb_sources:
        for artist in source_values[source_name]:
            support_counts[artist] = (
                support_counts.get(artist, 0)
                + 1
            )
            supported_by.setdefault(
                artist,
                [],
            ).append(source_name)

    expected_artists = {
        artist
        for artist, count in support_counts.items()
        if count >= 2
    }

    missing_from_mb = (
        expected_artists
        - mb_artists
    )

    non_mb_union = set()
    for source_name in non_mb_sources:
        non_mb_union |= set(
            source_values[source_name].keys()
        )

    mb_only = (
        mb_artists
        - non_mb_union
    )

    mb_supported = {
        artist
        for artist in mb_artists
        if support_counts.get(artist, 0) >= 1
    }

    if not mb_artists:
        status = "PARTIAL"
        reason = (
            "MusicBrainz Recording не содержит пригодного Artist credit."
        )

    elif missing_from_mb:
        status = "CONFLICT"
        names = [
            display_names.get(artist, artist)
            for artist in sorted(missing_from_mb)
        ]
        reason = (
            "MusicBrainz credit не содержит исполнителя(ей), "
            "которых подтверждают минимум два других источника: "
            + ", ".join(names)
            + "."
        )

    elif mb_only:
        status = "PARTIAL"
        names = [
            display_names.get(artist, artist)
            for artist in sorted(mb_only)
        ]
        reason = (
            "MusicBrainz содержит дополнительный credit, который "
            "не подтверждён остальными источниками: "
            + ", ".join(names)
            + "."
        )

    elif mb_supported:
        status = "CONSENSUS"
        reason = (
            "MusicBrainz Artist credit не теряет исполнителей, "
            "которых независимо подтверждают другие источники."
        )

    else:
        status = "PARTIAL"
        reason = (
            "Identity подтверждён, но источников недостаточно для "
            "уверенной проверки Artist credit."
        )

    def display_list(values):
        return [
            display_names.get(value, value)
            for value in sorted(values)
        ]

    return {
        "status": status,
        "reason": reason,
        "musicbrainz_credit": (
            format_artist_credit(
                musicbrainz_recording
            )
            or None
        ),
        "expected_artists": display_list(
            expected_artists
        ),
        "missing_from_musicbrainz": display_list(
            missing_from_mb
        ),
        "musicbrainz_only": display_list(
            mb_only
        ),
        "support_counts": {
            display_names.get(artist, artist): count
            for artist, count in sorted(
                support_counts.items()
            )
        },
        "supported_by": {
            display_names.get(artist, artist): sources
            for artist, sources in sorted(
                supported_by.items()
            )
        },
        "sources": {
            source_name: [
                source_values[source_name][artist]
                for artist in sorted(
                    source_values[source_name]
                )
            ]
            for source_name in source_values
        },
    }
