from __future__ import annotations

from pathlib import Path

from core.audd_matcher import (
    artist_similarity,
    title_similarity,
    extract_version_markers,
    normalize_text,
)


def _filename_artist_title(file_path):
    stem = Path(file_path).stem
    if " - " not in stem:
        return "", stem
    artist, title = stem.split(" - ", 1)
    return artist.strip(), title.strip()


def _unique_strings(values):
    result = []
    seen = set()
    for value in values:
        if not value:
            continue
        text = str(value).strip()
        key = normalize_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _best_score(local_values, remote_value, similarity_func):
    if not remote_value:
        return 0.0, None

    best_score = 0.0
    best_local = None

    for local in local_values:
        score = float(similarity_func(local, remote_value))
        if score > best_score:
            best_score = score
            best_local = local

    return best_score, best_local


def evaluate_acrcloud_match(file_path, data, acr_result):
    """Conservatively validate an ACRCloud candidate against local identity.

    STRONG means the candidate is good enough to count as trusted identity,
    not that it is allowed to write tags automatically.
    """

    data = data or {}
    acr_result = acr_result or {}

    filename_artist, filename_title = _filename_artist_title(file_path)

    local_artists = _unique_strings([
        data.get("artist"),
        filename_artist,
    ])
    local_titles = _unique_strings([
        data.get("title"),
        filename_title,
    ])

    remote_artist = acr_result.get("artist") or ""
    remote_title = acr_result.get("title") or ""

    artist_score, artist_local = _best_score(
        local_artists,
        remote_artist,
        artist_similarity,
    )
    title_score, title_local = _best_score(
        local_titles,
        remote_title,
        title_similarity,
    )

    local_version_markers = set()
    for value in local_titles:
        local_version_markers |= extract_version_markers(value)

    remote_version_markers = extract_version_markers(remote_title)

    from core.versioning import local_version_conflict
    version_conflict = local_version_conflict(local_titles, remote_title)

    local_isrc = str(data.get("isrc") or data.get("ISRC") or "").strip()
    remote_isrc = str(acr_result.get("isrc") or "").strip()
    isrc_exact = bool(
        local_isrc
        and remote_isrc
        and normalize_text(local_isrc) == normalize_text(remote_isrc)
    )

    acr_score_raw = acr_result.get("score")
    try:
        acr_score = float(acr_score_raw) if acr_score_raw is not None else None
    except (TypeError, ValueError):
        acr_score = None

    overall_score = round(0.45 * artist_score + 0.55 * title_score, 2)

    if isrc_exact and not version_conflict:
        status = "STRONG"
        reason = "ISRC ACRCloud совпадает с локальным ISRC."
    elif version_conflict:
        status = "CONFLICT"
        reason = (
            "Версия трека конфликтует: локальные данные явно указывают "
            "Remix/VIP/Edit/Bootleg/Flip, а ACRCloud подтверждает другую версию."
        )
    elif (
        (title_score >= 88.0 and artist_score >= 72.0)
        or (title_score >= 82.0 and artist_score >= 85.0)
    ):
        # Очень низкий ACR score не считаем полностью trusted, даже если
        # текст похож. Это дополнительный защитный барьер от слабого fingerprint.
        if acr_score is not None and acr_score < 45.0:
            status = "PARTIAL"
            reason = (
                "Artist/Title хорошо совпадают, но ACRCloud score слишком низкий "
                "для trusted identity."
            )
        else:
            status = "STRONG"
            reason = (
                "Artist/Title ACRCloud хорошо согласуются с filename/текущими тегами, "
                "версия не конфликтует."
            )
    elif title_score >= 72.0 and artist_score >= 55.0:
        status = "PARTIAL"
        reason = (
            "Есть заметное совпадение Artist/Title, но его недостаточно для trusted identity."
        )
    else:
        status = "CONFLICT"
        reason = "Artist/Title ACRCloud плохо согласуются с filename/текущими тегами."

    return {
        "status": status,
        "overall_score": overall_score,
        "artist_score": round(artist_score, 2),
        "title_score": round(title_score, 2),
        "acr_score": acr_score,
        "artist_match": {
            "local": artist_local,
            "remote": remote_artist or None,
        },
        "title_match": {
            "local": title_local,
            "remote": remote_title or None,
        },
        "version_conflict": version_conflict,
        "local_version_markers": sorted(local_version_markers),
        "remote_version_markers": sorted(remote_version_markers),
        "local_isrc": local_isrc or None,
        "remote_isrc": remote_isrc or None,
        "isrc_exact": isrc_exact,
        "reason": reason,
    }
