import re
from pathlib import Path
from core.text_cache import cached_text
from core.duration_policy import duration_difference, AUTOMATIC_DURATION_TOLERANCE

from rapidfuzz.fuzz import (
    ratio,
    token_sort_ratio,
)


@cached_text
def normalize(text):
    if not text:
        return ""

    text = text.lower()

    text = text.replace("’", "'")
    text = text.replace("`", "'")

    text = re.sub(
        r"[\[\]{}_:;\"!?]",
        " ",
        text,
    )

    text = re.sub(r"\s+", " ", text)

    return text.strip()


@cached_text
def compact(text):
    """
    Нужен для случаев вроде:

    CodePandorum
    Code: Pandorum
    """

    text = normalize(text)

    return re.sub(
        r"[\W_]+",
        "",
        text,
        flags=re.UNICODE,
    )


def similarity(a, b):
    if not a or not b:
        return 0.0

    normal_score = ratio(
        normalize(a),
        normalize(b),
    )

    compact_score = ratio(
        compact(a),
        compact(b),
    )

    # Reordering is allowed, but every word still matters. token_set_ratio
    # returns 100 for a short title contained in an unrelated long title.
    token_score = token_sort_ratio(
        normalize(a),
        normalize(b),
    )

    return max(
        normal_score,
        compact_score,
        token_score,
    ) / 100.0


def strict_similarity(a, b):
    if not a or not b:
        return 0.0

    normal_score = ratio(
        normalize(a),
        normalize(b),
    )

    compact_score = ratio(
        compact(a),
        compact(b),
    )

    return max(
        normal_score,
        compact_score,
    ) / 100.0


def split_artists(value, *, known_entities=()):
    if not value:
        return []

    # AcoustID supplies artist entities. Protect a known atomic project such
    # as "Dodge & Fuski" before splitting a flat filename collaboration.
    protected = {}
    names = sorted(set(known_entities), key=lambda name: (-len(name), name.casefold(), name))
    marker_prefix = 'TagCleanerAtomicArtist'
    while marker_prefix.casefold() in value.casefold() or any(marker_prefix.casefold() in name.casefold() for name in names):
        marker_prefix += '_'
    for index, name in enumerate(names):
        marker = f"{marker_prefix}{index}"
        pattern = re.escape(name).replace(r"\ \&\ ", r"\s*&\s*")
        value, count = re.subn(r"(?<!\w)" + pattern + r"(?!\w)", marker, value, flags=re.I)
        if count:
            protected[marker.casefold()] = name
    parts = re.split(
        r"\s*(?:&|,|;|\bx\b|×|\bvs\.?(?=\s|$)|\b(?:feat\.?|ft\.?|featuring)\s+)\s*",
        value,
        flags=re.IGNORECASE,
    )

    parts = [part.strip() for part in parts if part.strip()]
    if protected:
        markers = re.compile('|'.join(re.escape(key) for key in sorted(protected, key=len, reverse=True)), re.I)
        # A known name may be only the prefix of a longer local spelling,
        # e.g. Stylust inside Stylust Beats. Restore it inside the full part,
        # without dropping the suffix or leaking a synthetic artist entity.
        parts = [markers.sub(lambda match: protected[match.group(0).casefold()], part) for part in parts]
    return parts


def filename_parts(filename):
    stem = Path(filename).stem

    if " - " not in stem:
        return "", ""

    artist, title = stem.split(" - ", 1)

    return artist.strip(), title.strip()


def extract_featured_artists(title, *, known_entities=()):
    """
    Например:

    93 Style (feat. Virus Syndicate)

    вернёт:
    ["Virus Syndicate"]
    """

    if not title:
        return []

    patterns = [
        r"\(\s*(?:feat\.?|ft\.?|featuring)\s+([^)]+)\)",
        r"\[\s*(?:feat\.?|ft\.?|featuring)\s+([^\]]+)\]",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            title,
            re.IGNORECASE,
        )

        if match:
            return split_artists(
                match.group(1), known_entities=known_entities,
            )

    return []


@cached_text
def clean_title(title):
    """
    Убираем информацию, которая не является
    собственно названием записи.

    При этом Remix НЕ удаляем:
    конкретная версия трека для нас важна.
    """

    if not title:
        return ""

    result = title

    # feat.
    result = re.sub(
        r"\s*\(\s*(?:feat\.?|ft\.?|featuring)\s+[^)]+\)",
        "",
        result,
        flags=re.IGNORECASE,
    )

    result = re.sub(
        r"\s*\[\s*(?:feat\.?|ft\.?|featuring)\s+[^\]]+\]",
        "",
        result,
        flags=re.IGNORECASE,
    )

    # Служебные подписи источников
    garbage = [
        r"\[\s*free download\s*\]",
        r"\(\s*free download\s*\)",
        r"\[\s*official audio\s*\]",
        r"\[\s*official video\s*\]",
        r"\[\s*premiere\s*\]",
    ]

    for pattern in garbage:
        result = re.sub(
            pattern,
            "",
            result,
            flags=re.IGNORECASE,
        )

    # Жанровые префиксы
    result = re.sub(
        r"^\s*(?:dubstep|riddim|future bass|edm)"
        r"\s*(?:::|\|)\s*",
        "",
        result,
        flags=re.IGNORECASE,
    )

    result = re.sub(
        r"\s+",
        " ",
        result,
    )

    return result.strip()


def artist_group_similarity(
    expected_artists,
    candidate_artists,
):
    """
    Дополнительные артисты у кандидата сами по себе
    не считаются ошибкой.

    Например:

    filename:
    Excision

    MusicBrainz:
    Excision, Sam King

    Главный Excision совпал — уже хорошо.
    Sam King позже разберём как возможный feat.
    """

    if not expected_artists:
        return 0.0

    if not candidate_artists:
        return 0.0

    from core.artist_aliases import same_artist
    scores = []

    for expected in expected_artists:
        best_score = max(
            1.0 if same_artist(expected, candidate) else similarity(expected, candidate)
            for candidate in candidate_artists
        )

        scores.append(best_score)

    return sum(scores) / len(scores)


VERSION_KEYWORDS = (
    "remix",
    "vip",
    "edit",
    "flip",
    "bootleg",
    "tribute",
    "demo",
    "version",
    "remaster",
    "instrumental",
)


def _version_phrase_parts(value):
    """Возвращает (тип версии, отличительные слова) для Remix/VIP/Edit и т.п."""
    normalized = normalize(value)
    tokens = [token for token in re.findall(r"[\w']+", normalized, flags=re.UNICODE) if token]

    kinds = {
        keyword
        for keyword in VERSION_KEYWORDS
        if re.search(rf"\b{re.escape(keyword)}\b", normalized, re.IGNORECASE)
    }

    # У некоторых неофициальных версий используется собственная подпись
    # вместо слова Remix/Flip. Например "DUSTED by Dusty Bits" фактически
    # является flip-версией Borg. Для сравнения считаем DUSTED by разновидностью
    # flip, а имя после by — идентификатором автора версии.
    if re.search(r"\bdusted\s+by\b", normalized, re.IGNORECASE):
        kinds.add("flip")

    # bootleg remix и просто remix часто являются двумя названиями одной и
    # той же пользовательской версии. Для сравнения автор/ремиксер важнее.
    generic = {
        "remix", "vip", "edit", "flip", "bootleg", "tribute",
        "demo", "version", "remaster", "instrumental", "mix", "original",
        "dusted", "by",
    }
    qualifiers = {
        token for token in tokens
        if token not in generic and len(token) > 1
    }

    return kinds, qualifiers


def extract_version_markers(title):
    from core.versioning import version_descriptors
    return [" ".join(filter(None, (item["author"], item["kind"])))
            for item in version_descriptors(title)]


def version_similarity(file_title, candidate_title):
    from core.versioning import compare_versions
    return compare_versions(file_title, candidate_title)


def candidate_score(data, candidate):
    acoustid_score = candidate.get(
        "score",
        0.0,
    )

    candidate_artists = candidate.get(
        "artists",
        [],
    )

    candidate_title = (
        candidate.get("title")
        or ""
    )

    # -----------------------------------------
    # Данные из имени файла
    # -----------------------------------------

    file_artist, file_title = filename_parts(
        data.get("file", "")
    )

    main_artists = split_artists(
        file_artist, known_entities=candidate_artists,
    )

    featured_artists = extract_featured_artists(
        file_title, known_entities=candidate_artists,
    )

    expected_artists = (
        main_artists
        + featured_artists
    )

    # Если filename вообще невозможно разобрать,
    # используем существующий Artist
    if not expected_artists:
        expected_artists = split_artists(data.get("artist") or "", known_entities=candidate_artists)

    artist_similarity = (
        artist_group_similarity(
            expected_artists,
            candidate_artists,
        )
    )

    # -----------------------------------------
    # Title
    # -----------------------------------------

    reference_title = clean_title(
        file_title
    )

    if not reference_title:
        reference_title = clean_title(
            data.get("title") or ""
        )

    candidate_clean_title = clean_title(
        candidate_title
    )

    title_similarity = similarity(
        reference_title,
        candidate_clean_title,
    )

    version_score, version_mismatch = version_similarity(
        reference_title,
        candidate_title,
    )

    # -----------------------------------------
    # Итог
    #
    # Сам звук — главный источник.
    # Filename используется как подтверждение.
    # Существующие теги сюда пока не включаем:
    # именно они часто бывают мусором.
    # -----------------------------------------

    score = (
        acoustid_score * 0.60
        + artist_similarity * 0.15
        + title_similarity * 0.15
        + version_score * 0.10
    )

    # Если версия трека не совпадает,
    # автоматическое исправление запрещаем.
    if version_mismatch:
        score = min(
            score,
            0.74,
        )

    duration_diff = duration_difference(data.get('duration'), candidate.get('duration'))
    duration_mismatch = duration_diff is not None and duration_diff > AUTOMATIC_DURATION_TOLERANCE

    return {
        "score": score,
        "acoustid_score": acoustid_score,

        "artist_similarity": artist_similarity,
        "title_similarity": title_similarity,

        "version_score": version_score,
        "version_mismatch": version_mismatch,

        "candidate": candidate,
        "duration_diff": duration_diff,
        "duration_mismatch": duration_mismatch,
        "supplemental_audio_only": bool(candidate.get("supplemental_audio_only")),
        "audio_pass_conflict": bool(candidate.get("audio_pass_conflict")),
    }


def rank_candidates(data, candidates):
    ranked = []

    for candidate in candidates:
        ranked.append(
            candidate_score(
                data,
                candidate,
            )
        )

    ranked.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    if ranked:
        best = ranked[0]
        alternatives = []
        for item in ranked[1:]:
            if (item["acoustid_score"] < 0.85
                    or best["acoustid_score"] - item["acoustid_score"] > 0.02):
                continue
            # Almost identical audio hits cannot be disambiguated merely by
            # a filename that may itself be wrong (including Original/VIP).
            audio_tied = (item["acoustid_score"] >= 0.90
                          and abs(best["acoustid_score"] - item["acoustid_score"]) <= 0.01)
            if not audio_tied and best["score"] - item["score"] > 0.02:
                continue
            if not item["candidate"].get("title") or not item["candidate"].get("artists"):
                continue
            if not _same_candidate_identity(best["candidate"], item["candidate"]):
                alternatives.append({**item["candidate"], "match_score": item["score"]})
        for other in best['candidate'].get('audio_pass_alternatives') or []:
            if not _same_candidate_identity(best['candidate'], other) and other not in alternatives:
                alternatives.append(other)
        best["identity_ambiguous"] = bool(alternatives) or best.get('audio_pass_conflict', False)
        best["identity_alternatives"] = alternatives

    return ranked


def _same_candidate_identity(left, right):
    """Different releases/MBIDs of one identity are not competing songs."""
    left_title, right_title = left.get("title") or "", right.get("title") or ""
    if version_similarity(left_title, right_title)[1]:
        return False
    if (left.get("recording_id")
            and left.get("recording_id") == right.get("recording_id")):
        return True
    from core.identity_fusion import _identity_title_score
    if _identity_title_score(left_title, right_title) < 92:
        return False
    from core.artist_aliases import same_artist
    from core.naming import extract_feature_entities_from_title
    a = list(left.get('artists') or []) + extract_feature_entities_from_title(left_title)['artists']
    b = list(right.get('artists') or []) + extract_feature_entities_from_title(right_title)['artists']
    # One shared artist does not make A & B and A & C the same recording.
    # Spelling/case/punctuation and verified aliases may agree; fuzzy overlap
    # is not evidence that an entirely different credited person is absent.
    def equivalent(x, y):
        return compact(x) == compact(y) or same_artist(x, y)
    return bool(a and b and all(any(equivalent(x, y) for y in b) for x in a)
                and all(any(equivalent(x, y) for x in a) for y in b))


def confidence_level(score):
    if score >= 0.90:
        return "HIGH"

    if score >= 0.75:
        return "MEDIUM"

    return "LOW"


def metadata_agreement(match):
    artist_score = match.get(
        "artist_similarity",
        0.0,
    )

    title_score = match.get(
        "title_similarity",
        0.0,
    )

    version_mismatch = match.get(
        "version_mismatch",
        False,
    )

    if version_mismatch:
        return "CONFLICT"

    if (
        artist_score >= 0.90
        and title_score >= 0.90
    ):
        return "STRONG"

    if (
        artist_score >= 0.75
        and title_score >= 0.75
    ):
        return "PARTIAL"

    return "CONFLICT"


def action_safety(match):
    acoustid_score = match.get(
        "acoustid_score",
        0.0,
    )

    artist_score = match.get(
        "artist_similarity",
        0.0,
    )

    title_score = match.get(
        "title_similarity",
        0.0,
    )

    version_mismatch = match.get(
        "version_mismatch",
        False,
    )

    if (match.get("duration_mismatch") or match.get("identity_ambiguous")
            or match.get("supplemental_audio_only") or match.get("audio_pass_conflict")):
        return "REVIEW"

    if version_mismatch:
        return "REVIEW" if acoustid_score >= 0.90 else "REJECT"

    if (
        acoustid_score >= 0.90
        and artist_score >= 0.90
        and title_score >= 0.90
    ):
        return "SAFE"

    if (
        acoustid_score >= 0.80
        and artist_score >= 0.70
        and title_score >= 0.70
    ):
        return "REVIEW"

    candidate = match.get("candidate") or {}
    if acoustid_score >= 0.90 and candidate.get("title") and candidate.get("artists"):
        # A grossly wrong filename does not erase a strong audio lead. It
        # remains a manual proposal and can trigger independent recognition.
        return "REVIEW"

    return "REJECT"
