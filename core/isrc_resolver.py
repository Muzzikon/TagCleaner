import re
from pathlib import Path

from core.cache_utils import atomic_write_json, cache_ttl_days, read_json_if_fresh
from core.singleflight import cached_singleflight

from core.audd_matcher import (
    artist_similarity,
    normalize_text,
    title_similarity,
)

from core.musicbrainz_client import (
    BASE_URL,
    _request_json,
    format_artist_credit,
    get_recording,
)


ROOT_DIR = Path(__file__).resolve().parent.parent
ISRC_CACHE_DIR = (
    ROOT_DIR
    / "cache"
    / "musicbrainz_isrc"
)


def normalize_isrc(value):
    if not value:
        return ""

    return re.sub(
        r"[^A-Za-z0-9]",
        "",
        str(value),
    ).upper()


def _load_json(path):
    try:
        cached = read_json_if_fresh(path, ttl_days=cache_ttl_days("TAGCLEANER_ISRC_CACHE_DAYS", 30))
        if not isinstance(cached, dict) or not isinstance(cached.get("recordings"), list):
            return None
        if not cached["recordings"]:
            return read_json_if_fresh(path, ttl_days=cache_ttl_days("TAGCLEANER_NEGATIVE_CACHE_DAYS", 1))
        return cached
    except OSError:
        return None


def _save_json(path, data):
    try:
        atomic_write_json(path, data)
    except OSError:
        pass  # A cache write failure must not discard a valid provider reply.


@cached_singleflight(lambda isrc: normalize_isrc(isrc))
def lookup_recordings_by_isrc(isrc):
    """
    Ищет MusicBrainz Recording по ISRC.

    Запрос идёт через существующий musicbrainz_client,
    поэтому используется его общий rate limit/session.
    """

    normalized_isrc = normalize_isrc(
        isrc
    )

    if not normalized_isrc:
        return []

    ISRC_CACHE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    cache_file = (
        ISRC_CACHE_DIR
        / f"{normalized_isrc}.json"
    )

    cached = _load_json(
        cache_file
    )

    if cached is not None:
        return (
            cached.get(
                "recordings",
                [],
            )
            or []
        )

    data = _request_json(
        f"{BASE_URL}/recording",
        params={
            "query": (
                f"isrc:{normalized_isrc}"
            ),
            "limit": 100,
            "fmt": "json",
        },
    )

    recordings = (
        data.get(
            "recordings",
            [],
        )
        or []
    )

    _save_json(
        cache_file,
        {
            "isrc": normalized_isrc,
            "recordings": recordings,
        },
    )

    return recordings


def _unique_strings(values):
    result = []
    seen = set()

    for value in values:
        if not value:
            continue

        text = str(value).strip()

        if not text:
            continue

        key = normalize_text(
            text
        )

        if not key or key in seen:
            continue

        seen.add(key)
        result.append(text)

    return result


def _artist_values_from_audd(
    audd_result,
):
    audd_result = audd_result or {}

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

    return _unique_strings([
        audd_result.get("artist"),
        (
            " & ".join(
                spotify_artists
            )
            if spotify_artists
            else None
        ),
        apple.get("artist"),
    ])


def _title_values_from_audd(
    audd_result,
):
    audd_result = audd_result or {}

    apple = (
        audd_result.get("apple_music")
        or {}
    )

    return _unique_strings([
        audd_result.get("title"),
        apple.get("title"),
    ])


def _best_score(
    left_values,
    right_values,
    similarity_func,
):
    best_score = 0.0
    best_left = None
    best_right = None

    for left in left_values:
        for right in right_values:
            score = float(
                similarity_func(
                    left,
                    right,
                )
            )

            if score > best_score:
                best_score = score
                best_left = left
                best_right = right

    return {
        "score": best_score,
        "left": best_left,
        "right": best_right,
    }


def _recording_artist_values(
    recording,
):
    values = []

    credit = format_artist_credit(
        recording
    )

    if credit:
        values.append(
            credit
        )

    for item in recording.get(
        "artist-credit",
        [],
    ) or []:
        name = (
            item.get("name")
            or (
                item.get("artist")
                or {}
            ).get("name")
        )

        if name:
            values.append(
                name
            )

    return _unique_strings(
        values
    )


def score_recording_candidate(
    recording,
    audd_result,
):
    remote_artists = (
        _artist_values_from_audd(
            audd_result
        )
    )

    remote_titles = (
        _title_values_from_audd(
            audd_result
        )
    )

    mb_artists = (
        _recording_artist_values(
            recording
        )
    )

    mb_titles = _unique_strings([
        recording.get("title"),
    ])

    artist_match = _best_score(
        mb_artists,
        remote_artists,
        artist_similarity,
    )

    title_match = _best_score(
        mb_titles,
        remote_titles,
        title_similarity,
    )

    artist_score = (
        artist_match["score"]
    )

    title_score = (
        title_match["score"]
    )

    overall_score = round(
        0.45 * artist_score
        + 0.55 * title_score,
        2,
    )

    eligible = (
        (
            title_score >= 86.0
            and artist_score >= 70.0
        )
        or (
            title_score >= 80.0
            and artist_score >= 85.0
        )
    )

    from core.versioning import compare_versions
    version_conflict = bool(remote_titles) and all(compare_versions(recording.get("title"), value)[1]
                                                 for value in remote_titles)
    eligible = eligible and not version_conflict

    return {
        "recording_id": (
            recording.get("id")
        ),
        "title": (
            recording.get("title")
            or ""
        ),
        "artist_credit": (
            format_artist_credit(
                recording
            )
        ),
        "artist_score": round(
            artist_score,
            2,
        ),
        "title_score": round(
            title_score,
            2,
        ),
        "overall_score": (
            overall_score
        ),
        "artist_match": (
            artist_match
        ),
        "title_match": (
            title_match
        ),
        "eligible": eligible,
        "version_conflict": version_conflict,
        "search_score": (
            recording.get("score")
        ),
    }


def choose_recording_candidate(
    recordings,
    audd_result,
):
    """
    Выбирает MusicBrainz Recording среди результатов
    точного ISRC-поиска и дополнительно сверяет его
    с AudD Artist/Title.
    """

    recordings = recordings or []

    if not recordings:
        return {
            "status": "NOT_FOUND",
            "selected": None,
            "candidates": [],
            "reason": (
                "MusicBrainz не нашёл Recording "
                "по этому ISRC."
            ),
        }

    scored = [
        score_recording_candidate(
            recording,
            audd_result,
        )
        for recording in recordings
    ]

    scored.sort(
        key=lambda item: (
            item.get(
                "overall_score",
                0.0,
            ),
            item.get(
                "title_score",
                0.0,
            ),
            item.get(
                "artist_score",
                0.0,
            ),
            item.get(
                "search_score",
                0,
            ) or 0,
        ),
        reverse=True,
    )

    eligible = [
        item
        for item in scored
        if item.get("eligible")
    ]

    if not eligible and sum(item.get("version_conflict") and item.get("overall_score", 0) >= 80 for item in scored) > 1:
        return {"status": "AMBIGUOUS", "selected": None, "candidates": scored,
                "reason": "Несколько похожих ISRC-кандидатов, но ни один не подтверждает полную версию трека."}

    if not eligible:
        return {
            "status": "CONFLICT",
            "selected": None,
            "candidates": scored,
            "reason": (
                "ISRC найден в MusicBrainz, но Artist/Title "
                "найденных Recording плохо согласуются с AudD."
            ),
        }

    best = eligible[0]

    if len(eligible) > 1:
        second = eligible[1]

        best_signature = (
            normalize_text(
                best.get("artist_credit")
            ),
            normalize_text(
                best.get("title")
            ),
        )

        second_signature = (
            normalize_text(
                second.get("artist_credit")
            ),
            normalize_text(
                second.get("title")
            ),
        )

        score_gap = (
            float(
                best.get(
                    "overall_score",
                    0.0,
                )
            )
            - float(
                second.get(
                    "overall_score",
                    0.0,
                )
            )
        )

        if (
            best_signature
            != second_signature
            and score_gap < 4.0
        ):
            return {
                "status": "AMBIGUOUS",
                "selected": None,
                "candidates": scored,
                "reason": (
                    "Один ISRC ведёт к нескольким разным "
                    "MusicBrainz Recording с близкой оценкой."
                ),
            }

    equivalent_ids = []
    best_signature = (
        normalize_text(
            best.get("artist_credit")
        ),
        normalize_text(
            best.get("title")
        ),
    )

    for item in eligible:
        signature = (
            normalize_text(
                item.get("artist_credit")
            ),
            normalize_text(
                item.get("title")
            ),
        )

        if signature == best_signature:
            recording_id = (
                item.get("recording_id")
            )

            if recording_id:
                equivalent_ids.append(
                    recording_id
                )

    return {
        "status": "RESOLVED",
        "selected": best,
        "candidates": scored,
        "equivalent_recording_ids": (
            equivalent_ids
        ),
        "reason": (
            "ISRC найден в MusicBrainz, а Recording "
            "согласуется с подтверждённым AudD Artist/Title."
        ),
    }


def resolve_isrc_recording(
    isrc,
    audd_result,
):
    normalized_isrc = normalize_isrc(
        isrc
    )

    if not normalized_isrc:
        return {
            "status": "NO_ISRC",
            "isrc": None,
            "recording_id": None,
            "recording": None,
            "selected": None,
            "candidates": [],
            "reason": (
                "AudD не вернул ISRC."
            ),
        }

    recordings = (
        lookup_recordings_by_isrc(
            normalized_isrc
        )
    )

    selection = (
        choose_recording_candidate(
            recordings,
            audd_result,
        )
    )

    result = {
        "status": selection.get(
            "status"
        ),
        "isrc": normalized_isrc,
        "recording_id": None,
        "recording": None,
        "selected": selection.get(
            "selected"
        ),
        "candidates": selection.get(
            "candidates",
            [],
        ),
        "equivalent_recording_ids": (
            selection.get(
                "equivalent_recording_ids",
                [],
            )
        ),
        "reason": selection.get(
            "reason"
        ),
    }

    if (
        result["status"]
        != "RESOLVED"
    ):
        return result

    selected = (
        result.get("selected")
        or {}
    )

    recording_id = (
        selected.get(
            "recording_id"
        )
    )

    if not recording_id:
        result["status"] = "ERROR"
        result["reason"] = (
            "MusicBrainz-кандидат не содержит recording_id."
        )
        return result

    recording = get_recording(
        recording_id
    )

    result[
        "recording_id"
    ] = recording_id

    result[
        "recording"
    ] = recording

    return result
