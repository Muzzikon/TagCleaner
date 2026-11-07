from __future__ import annotations

import re

from core.audd_matcher import (
    split_artist_names,
)


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _artists_text(values):
    if not values:
        return ""
    if isinstance(values, str):
        return values.strip()
    return " & ".join(str(value).strip() for value in values if str(value).strip())




def _artist_credit_parts(value, reference=None):
    """Возвращает набор артистов, не ломая обычные имена с ``and``.

    ACRCloud иногда оформляет join phrase словом ``and``:
        WoodenToaster and Mic the Microphone

    Базовый splitter намеренно не режет любое ``and`` (иначе можно сломать
    названия групп). Поэтому такой вариант разбираем только когда рядом есть
    другой credit, подтверждающий хотя бы одну из частей.

    Для slash требуется подтверждение всех частей: имя вроде AC/DC нельзя
    сокращать до AC по совпадению только одной части.
    """
    text = str(value or "").strip()
    parts = set(split_artist_names(text))

    if not text or not reference:
        return parts

    reference_parts = set(split_artist_names(reference or ""))
    if not reference_parts:
        return parts

    if re.search(r"\band\b", text, re.IGNORECASE):
        alt_text = re.sub(r"\band\b", " & ", text, flags=re.IGNORECASE)
        alt_parts = set(split_artist_names(alt_text))
        if len(alt_parts) > len(parts) and reference_parts & alt_parts:
            return alt_parts

    if '/' in text:
        alt_parts = set(split_artist_names(text.replace('/', ' & ')))
        # Slash may be part of a project name (AC/DC). Split only if the
        # other credit explicitly corroborates every resulting artist.
        from core.artist_aliases import same_artist
        if len(alt_parts) > len(parts) and all(
                any(a == b or same_artist(a, b) for b in reference_parts) for a in alt_parts):
            return alt_parts

    return parts


def _artist_credits_compatible(left_artist, right_artist):
    """
    Считает credits совместимыми, если различие объясняется feat/collab.

    Типичный случай из реального прогона:
        ACRCloud: AFK & Wooli
        MusicBrainz: AFK & Wooli feat. Jay Fresh

    Exact ISRC уже связывает аудио с Recording, поэтому отсутствие featured
    артиста в одном внешнем сервисе не должно само по себе блокировать SAFE.
    """
    left = _artist_credit_parts(left_artist, right_artist)
    right = _artist_credit_parts(right_artist, left_artist)

    if not left or not right:
        return False

    if left <= right or right <= left:
        return True

    from core.artist_aliases import same_artist
    small, large = (left, right) if len(left) <= len(right) else (right, left)
    return all(any(a == b or same_artist(a, b) for b in large) for a in small)


def _artist_credit_priority(item):
    kind = (item or {}).get("kind")
    return {
        "acrcloud_isrc_musicbrainz": 50,
        "acrcloud": 40,
        "catalog": 30,
        "source_url": 20,
        "acoustid": 10,
    }.get(kind, 0)


def _artist_credit_typo_match(left_artist, right_artist):
    """One small spelling difference must not be diluted by shared artists."""
    from core.artist_aliases import same_artist
    from core.matcher import strict_similarity
    left = _artist_credit_parts(left_artist, right_artist)
    right = _artist_credit_parts(right_artist, left_artist)
    if len(left) != len(right):
        return False
    missing_left = [a for a in left if not any(a == b or same_artist(a, b) for b in right)]
    missing_right = [b for b in right if not any(a == b or same_artist(a, b) for a in left)]
    return (len(missing_left) == len(missing_right) == 1
            and min(len(missing_left[0]), len(missing_right[0])) >= 5
            and strict_similarity(missing_left[0], missing_right[0]) >= 0.92)


def _with_richer_artist_credit(chosen, supporters):
    """
    Не даёт consensus терять соисполнителей.

    Примеры из массового прогона:
      Kayzo & Krimer -> Kayzo
      Too Vain & Zikiel -> Too Vain
      Urbanstep & Micah Martin -> Urbanstep

    Recording/title остаются от выбранного канонического источника,
    а Artist можно взять из другого сильного источника, если его credit
    совместим и содержит больше подтверждённых исполнителей.
    """
    chosen = dict(chosen or {})
    current_artist = chosen.get("artist") or ""
    current_parts = _artist_credit_parts(current_artist)

    candidates = []
    for item in [chosen, *(supporters or [])]:
        if not item or not item.get("artist"):
            continue
        if item.get("version_conflict"):
            continue

        artist_score = item.get("artist_score")
        if artist_score is not None and _float(artist_score) < 90.0:
            continue

        item_artist = item.get("artist") or ""
        item_parts = _artist_credit_parts(item_artist, current_artist)
        if not item_parts:
            continue

        if current_parts and not _artist_credits_compatible(current_artist, item_artist):
            continue

        candidates.append(item)

    if not candidates:
        return chosen

    best = max(
        candidates,
        key=lambda item: (
            len(_artist_credit_parts(item.get("artist") or "", current_artist)),
            _artist_credit_priority(item),
            _float(item.get("overall_score")),
        ),
    )

    best_parts = _artist_credit_parts(best.get("artist") or "", current_artist)
    if len(best_parts) > len(current_parts):
        chosen["artist"] = best.get("artist")

    return chosen


def _title_only_support_is_safe(full_identity):
    """Title-only source может подтверждать identity, но не неполный Artist."""
    if not full_identity or not full_identity.get("artist"):
        return False

    artist_score = full_identity.get("artist_score")
    if artist_score is None:
        return False

    return (
        _float(artist_score) >= 98.0
        and _float(full_identity.get("title_score")) >= 97.0
        and _float(full_identity.get("overall_score")) >= 97.0
        and not full_identity.get("version_conflict")
    )


def _acr_conflict_evidence(acr_result):
    """Высокоуверенный ACRCloud-кандидат, отвергнутый только локальными тегами."""
    acr_result = acr_result or {}
    if acr_result.get("status") not in {"CONFLICT", "PARTIAL"}:
        return None

    best = acr_result.get("best") or {}
    match = acr_result.get("match") or {}
    artist = best.get("artist") or ""
    title = best.get("title") or ""
    acr_score = None if match.get("acr_score") is None else _float(match.get("acr_score"))

    if not artist or not title:
        return None
    if (acr_score is None or acr_score < 85.0) and not acr_result.get("identity_ambiguous"):
        return None

    return {
        "kind": "acrcloud_local_conflict",
        "artist": artist,
        "title": title,
        "recording_id": None,
        "provider": "acrcloud",
        "isrc": best.get("isrc"),
        "overall_score": _float(match.get("overall_score")),
        "artist_score": _float(match.get("artist_score")),
        "title_score": _float(match.get("title_score")),
        "acr_score": acr_score,
        "version_conflict": bool(match.get("version_conflict")),
        "strong": False,
        "identity_ambiguous": bool(acr_result.get("identity_ambiguous")),
        "identity_alternatives": acr_result.get("identity_alternatives") or [],
        "verification_incomplete": bool(acr_result.get("verification_incomplete")),
    }


def _catalog_audio_confirmation_ok(evidence):
    if not evidence or evidence.get("kind") != "catalog":
        return False
    duration_diff = evidence.get("duration_diff")
    return (
        evidence.get("provider") == "musicbrainz"
        and bool(evidence.get("recording_id"))
        and _float(evidence.get("artist_score")) >= 95.0
        and _float(evidence.get("title_score")) >= 94.0
        and _float(evidence.get("overall_score")) >= 94.0
        and (duration_diff is None or _float(duration_diff, 999.0) <= 6.0)
    )


def _version_conflict(left_title, right_title):
    from core.versioning import compare_versions
    return compare_versions(left_title, right_title)[1]


def _identity_title_score(left_title, right_title):
    """Whole-title agreement after explicit credit/version formatting.

    WRatio boosts a contained short title to 90–95 even for different songs.
    Discovery may be fuzzy; automatic consensus requires full title agreement.
    Version authors/kinds are checked by identities_match before this helper.
    """
    from core.matcher import clean_title, strict_similarity
    from core.audd_matcher import normalize_text
    from core.naming import canonical_track_title
    from core.versioning import version_descriptors

    def prepare(value):
        value = clean_title(canonical_track_title(value))
        # A trailing explicit featured credit is metadata, not a title word.
        return re.sub(r"\s+(?:feat\.?|ft\.?|featuring)\s+[^()\[\]]+(?=$|\s*[\(\[])",
                      " ", value, flags=re.I).strip()

    left, right = prepare(left_title), prepare(right_title)
    left_numbers, right_numbers = re.findall(r'\d+', left), re.findall(r'\d+', right)
    if left_numbers and right_numbers and ([n.lstrip('0') or '0' for n in left_numbers]
                                          != [n.lstrip('0') or '0' for n in right_numbers]):
        return 0.0
    parts = r'\b(?:part|pt\.?|volume|vol\.?|chapter)\s+([ivxlcdm]+|\d+)\b'
    left_parts, right_parts = re.findall(parts, left, re.I), re.findall(parts, right, re.I)
    if [p.casefold() for p in left_parts] != [p.casefold() for p in right_parts]:
        return 0.0
    score = strict_similarity(normalize_text(left), normalize_text(right))
    left_versions, right_versions = version_descriptors(left), version_descriptors(right)
    if (left_versions and right_versions
            and not any(item['bare'] for item in [*left_versions, *right_versions])):
        # Already verified matching version signatures. Strip those blocks
        # only, preserving ordinary parentheses such as Part III or subtitles.
        def base(value):
            return re.sub(r"[\(\[]([^()\[\]]+)[\)\]]",
                          lambda m: ' ' if version_descriptors('(' + m.group(1) + ')') else m.group(0), value)
        score = max(score, strict_similarity(normalize_text(base(left)), normalize_text(base(right))))
    return 100.0 * score


def identities_match(left, right):
    """Консервативно проверяет, говорят ли два источника об одной identity."""
    left = left or {}
    right = right or {}

    left_title = left.get("title") or ""
    right_title = right.get("title") or ""

    if not left_title or not right_title:
        return False

    if _version_conflict(left_title, right_title):
        return False

    title_score = _identity_title_score(left_title, right_title)

    left_artist = left.get("artist") or ""
    right_artist = right.get("artist") or ""

    if left_artist and right_artist:
        return title_score >= 92.0 and (
            _artist_credits_compatible(left_artist, right_artist)
            or _artist_credit_typo_match(left_artist, right_artist))

    # Источник без Artist (чаще всего Spotify oEmbed) может быть только
    # дополнительным подтверждением title, а не самостоятельной identity.
    return title_score >= 97.0


def _source_evidence(source_result):
    source_result = source_result or {}
    if source_result.get("status") != "STRONG":
        return None

    best = source_result.get("best") or {}
    title = best.get("source_title") or best.get("raw_title") or ""
    artist = best.get("source_artist") or ""

    if not title:
        return None

    return {
        "kind": "source_url",
        "artist": artist,
        "title": title,
        "recording_id": None,
        "provider": best.get("provider"),
        "overall_score": _float(best.get("overall_score")),
        "artist_score": (
            None if best.get("artist_score") is None else _float(best.get("artist_score"))
        ),
        "title_score": _float(best.get("title_score")),
        "version_conflict": bool(best.get("version_conflict")),
        "strong": True,
    }


def _catalog_evidence(catalog_result):
    catalog_result = catalog_result or {}
    if catalog_result.get("status") != "STRONG":
        return None

    best = catalog_result.get("best") or {}
    artist = best.get("artist") or ""
    title = best.get("title") or ""

    if not title:
        return None

    return {
        "kind": "catalog",
        "artist": artist,
        "title": title,
        "recording_id": best.get("recording_id"),
        "provider": best.get("provider"),
        "overall_score": _float(best.get("overall_score")),
        "artist_score": _float(best.get("artist_score")),
        "title_score": _float(best.get("title_score")),
        "duration_diff": (
            None if best.get("duration_diff") is None else _float(best.get("duration_diff"))
        ),
        "version_conflict": bool(best.get("version_conflict")),
        "strong": True,
    }


def _acr_evidence(acr_result):
    acr_result = acr_result or {}
    if acr_result.get("status") != "STRONG":
        return None

    best = acr_result.get("best") or {}
    match = acr_result.get("match") or {}
    artist = best.get("artist") or ""
    title = best.get("title") or ""

    if not artist or not title:
        return None

    return {
        "kind": "acrcloud",
        "artist": artist,
        "title": title,
        "recording_id": None,
        "provider": "acrcloud",
        "isrc": best.get("isrc"),
        "overall_score": _float(match.get("overall_score")),
        "artist_score": _float(match.get("artist_score")),
        "title_score": _float(match.get("title_score")),
        "acr_score": (
            None if match.get("acr_score") is None else _float(match.get("acr_score"))
        ),
        "version_conflict": bool(match.get("version_conflict")),
        "strong": True,
        "identity_ambiguous": bool(acr_result.get("identity_ambiguous")),
        "identity_alternatives": acr_result.get("identity_alternatives") or [],
        "verification_incomplete": bool(acr_result.get("verification_incomplete")),
    }


def _acoustid_evidence(acoustid_match, acoustid_safety):
    acoustid_match = acoustid_match or {}
    candidate = acoustid_match.get("candidate") or {}

    title = candidate.get("title") or ""
    artist = _artists_text(candidate.get("artists"))

    if not title:
        return None

    return {
        "kind": "acoustid",
        "artist": artist,
        "title": title,
        "recording_id": candidate.get("recording_id"),
        "provider": "acoustid",
        "overall_score": 100.0 * _float(acoustid_match.get("score")),
        "artist_score": 100.0 * _float(acoustid_match.get("artist_similarity")),
        "title_score": 100.0 * _float(acoustid_match.get("title_similarity")),
        "acoustid_score": _float(acoustid_match.get("acoustid_score")),
        "version_conflict": bool(acoustid_match.get("version_mismatch")),
        "safety": acoustid_safety or "REJECT",
        "identity_ambiguous": bool(acoustid_match.get("identity_ambiguous")),
        "identity_alternatives": acoustid_match.get("identity_alternatives") or [],
        "duration_mismatch": bool(acoustid_match.get("duration_mismatch")),
        "supplemental_audio_only": bool(acoustid_match.get("supplemental_audio_only")),
        "audio_pass_conflict": bool(acoustid_match.get("audio_pass_conflict")),
        "strong": acoustid_safety in {"SAFE", "REVIEW"},
    }


def _shazam_evidence(result):
    result = result or {}
    best, match = result.get('best') or {}, result.get('match') or {}
    if not best.get('artist') or not best.get('title'):
        return None
    return {'kind': 'shazam', 'provider': 'shazam', 'artist': best['artist'],
            'title': best['title'], 'recording_id': None, 'isrc': best.get('isrc'),
            'overall_score': _float(match.get('overall_score')),
            'artist_score': _float(match.get('artist_score')), 'title_score': _float(match.get('title_score')),
            'strong': result.get('status') == 'STRONG',
            'audio_confirmed': result.get('agreement_count', 0) >= 2,
            'agreement_count': result.get('agreement_count', 0),
            'version_conflict': bool(match.get('version_conflict')),
            'identity_ambiguous': bool(result.get('identity_ambiguous')),
            'identity_alternatives': result.get('identity_alternatives') or [],
            'verification_incomplete': bool(result.get('verification_incomplete'))}


def _independent_audio_confirmation(chosen, evidence):
    return any(identities_match(chosen, item) and not item.get('version_conflict')
               and not item.get('identity_ambiguous') and not item.get('verification_incomplete')
               and ((item.get('kind') == 'acrcloud' and _float(item.get('acr_score')) >= 90)
                    or (item.get('kind') == 'shazam' and item.get('strong') and item.get('audio_confirmed')))
               for item in evidence)


def _acr_mb_evidence(acr_mb_result, acr_result):
    acr_mb_result = acr_mb_result or {}
    acr_result = acr_result or {}

    if acr_mb_result.get("status") != "RESOLVED":
        return None

    selected = acr_mb_result.get("selected") or {}
    recording = acr_mb_result.get("recording") or {}
    acr_match = acr_result.get("match") or {}

    artist = selected.get("artist_credit") or ""
    title = selected.get("title") or recording.get("title") or ""

    if not title:
        return None

    return {
        "kind": "acrcloud_isrc_musicbrainz",
        "artist": artist,
        "title": title,
        "recording_id": acr_mb_result.get("recording_id"),
        "provider": "musicbrainz",
        "isrc": acr_mb_result.get("isrc"),
        "overall_score": _float(selected.get("overall_score")),
        "artist_score": _float(selected.get("artist_score")),
        "title_score": _float(selected.get("title_score")),
        "acr_score": (
            None if acr_match.get("acr_score") is None else _float(acr_match.get("acr_score"))
        ),
        "version_conflict": bool(acr_match.get("version_conflict")),
        "strong": True,
    }


def _catalog_is_exceptionally_strong(evidence):
    if not evidence or evidence.get("kind") != "catalog":
        return False

    duration_diff = evidence.get("duration_diff")

    return (
        evidence.get("provider") == "musicbrainz"
        and bool(evidence.get("recording_id"))
        and _float(evidence.get("artist_score")) >= 98.0
        and _float(evidence.get("title_score")) >= 98.0
        and _float(evidence.get("overall_score")) >= 96.0
        and (duration_diff is None or _float(duration_diff, 999.0) <= 2.0)
        and not evidence.get("version_conflict")
    )


def _audio_ambiguity_resolution(chosen, evidence):
    """Record how a tied audio identity was (or was not) disambiguated."""
    audio = next((item for item in evidence if item.get("kind") == "acoustid"
                  and item.get("identity_ambiguous")), None)
    if not audio:
        return None
    alternatives = audio.get("identity_alternatives") or []
    resolution = {"resolved": False, "source": None, "alternatives": alternatives}
    acr = next((item for item in evidence if item.get("kind") in {"acrcloud", "shazam"}
                and not item.get("identity_ambiguous") and not item.get("verification_incomplete")
                and not item.get('version_conflict')
                and ((item.get('kind') == 'acrcloud' and _float(item.get('acr_score')) >= 90)
                     or (item.get('kind') == 'shazam' and item.get('strong') and item.get('audio_confirmed')))
                and identities_match(chosen, item)), None)
    if not acr or not alternatives:
        return resolution
    from core.matcher import clean_title, strict_similarity, normalize
    from core.versioning import version_descriptors
    bridge = next((item for item in evidence if item.get("kind") == "acrcloud_isrc_musicbrainz"
                   and item.get("recording_id") == chosen.get("recording_id")
                   and item.get("recording_id") and identities_match(chosen, item)), None)
    for alternative in alternatives:
        other = {"artist": _artists_text(alternative.get("artists")), "title": alternative.get("title")}
        if identities_match(acr, other):
            return resolution
        # A service returning a bare title does not prove that a VIP/extended
        # version was absent. Require an explicit version or an exact ISRC
        # bridge to this recording when only the version distinguishes hits.
        stripped_other = re.sub(r"[\(\[][^()\[\]]*[\)\]]", "", clean_title(other["title"] or ""))
        stripped_chosen = re.sub(r"[\(\[][^()\[\]]*[\)\]]", "", clean_title(chosen.get("title") or ""))
        suffix = r"\s+(?:vip|instrumental|demo|remaster(?:ed)?|version|(?:extended|radio)\s+(?:mix|edit|version))\s*$"
        stripped_other = re.sub(suffix, "", stripped_other, flags=re.I).strip()
        stripped_chosen = re.sub(suffix, "", stripped_chosen, flags=re.I).strip()
        left_base, right_base = normalize(stripped_chosen), normalize(stripped_other)
        # For a bare "Song Mat Zo Remix", the author cannot be stripped
        # without guessing. A whole-word base prefix still requires review.
        base_prefix = bool(left_base and right_base and (
            left_base.startswith(right_base + ' ') or right_base.startswith(left_base + ' ')))
        # Different credit spellings/aliases cannot prove absence of a VIP or
        # remaster. A bare provider title must not disambiguate same-base
        # versions just because one candidate uses another artist spelling.
        same_base = strict_similarity(stripped_chosen, stripped_other) >= 0.92 or base_prefix
        if same_base and _version_conflict(chosen.get("title"), other["title"]):
            if not version_descriptors(acr.get("title")) and not bridge:
                return resolution
    resolution.update(resolved=True, source="acrcloud_isrc_musicbrainz" if bridge else acr['kind'])
    return resolution


def _automatic_write_objections(chosen, evidence):
    """Consensus must not hide an already available contrary audio result.

    Do not spend extra requests here. Retain the proposed identity and all
    evidence; only its permission for automatic writing changes.
    """
    reasons = []
    for item in evidence:
        kind = item.get("kind")
        if kind in {"acrcloud", "acrcloud_local_conflict", "acrcloud_verification"}:
            if item.get("version_conflict"):
                reasons.append("ACRCloud конфликтует с явно указанной локальной версией; распознанный фрагмент может относиться к оригиналу или общей части ремикса.")
            if item.get("identity_ambiguous"):
                reasons.append("ACRCloud распознал разные треки/версии в ответах по фрагментам; требуется проверка всех вариантов.")
            if item.get("verification_incomplete"):
                reasons.append("Проверка остальных фрагментов ACRCloud не завершена из-за ошибки; автоматическая запись требует повторной проверки.")
        if kind in {'shazam', 'shazam_verification'}:
            if item.get('version_conflict'):
                reasons.append('Shazam конфликтует с явно указанной локальной версией; общая часть оригинала и ремикса не доказывает нужную версию.')
            if item.get('identity_ambiguous'):
                reasons.append('Shazam распознал разные треки/версии в отдельных фрагментах; требуется проверка всех вариантов.')
            if item.get('verification_incomplete'):
                reasons.append('Проверка фрагментов Shazam не завершена из-за ошибки; автоматическая запись требует повторной проверки.')
        audio_strong = (
            (kind == "acoustid" and _float(item.get("acoustid_score")) >= 0.90)
            or (kind in {"acrcloud", "acrcloud_local_conflict"}
                and item.get("acr_score") is not None and _float(item.get("acr_score")) >= 90.0)
            or (kind == 'shazam' and item.get('audio_confirmed'))
        )
        if audio_strong and not identities_match(chosen, item):
            reasons.append(f"{kind}: сильное аудио-распознавание указывает на другой трек или версию; требуется проверка.")
        elif kind == 'shazam' and not identities_match(chosen, item):
            reasons.append('Один фрагмент Shazam указывает на другой трек/версию; ответ сохранён, но конфликт ещё не разрешён.')
        elif item.get("strong") and not item.get("version_conflict") and not identities_match(chosen, item):
            reasons.append(f"{kind}: сильные источники противоречат друг другу; требуется проверка.")
        if kind == "acoustid" and item.get("duration_mismatch") and identities_match(chosen, item):
            reasons.append("Длительность аудио-кандидата выходит за допустимое расхождение; текстовое совпадение не объясняет различие границ или версии.")
        if kind == "acoustid" and item.get("version_conflict"):
            reasons.append("Аудио-кандидат конфликтует с явно указанной локальной версией; текстовый каталог не разрешает конфликт автоматически.")
        if kind == "acoustid" and item.get("identity_ambiguous"):
            # Text catalog/source queries derive from local names. Only an
            # independent audio result can select between tied audio matches.
            resolution = _audio_ambiguity_resolution(chosen, evidence)
            if not resolution or not resolution["resolved"]:
                reasons.append("AcoustID дал несколько близких кандидатов разных треков/версий; независимого аудио-подтверждения выбора нет.")
        if kind == 'acoustid' and item.get('audio_pass_conflict'):
            reasons.append('Дополнительные отпечатки AcoustID указали на разные треки/версии; все ответы одной базы сохранены для проверки.')
        if (kind == 'acoustid' and item.get('supplemental_audio_only')
                and not _independent_audio_confirmation(chosen, evidence)):
            reasons.append('Кандидат получен или усилен только дополнительным отпечатком AcoustID; независимого подтверждения по аудио нет.')
    return list(dict.fromkeys(reasons))


def _result(status, chosen=None, *, evidence=None, reason=""):
    chosen = chosen or {}
    evidence = [item for item in (evidence or []) if item]

    review_reasons = _automatic_write_objections(chosen, evidence) if status in {"SAFE", "REVIEW"} else []
    if review_reasons and status == "SAFE":
        status = "REVIEW"
        reason = " ".join(review_reasons)

    return {
        "status": status,
        "artist": chosen.get("artist") or None,
        "title": chosen.get("title") or None,
        "recording_id": chosen.get("recording_id"),
        "provider": chosen.get("provider") or chosen.get("kind"),
        "identity_source": chosen.get("kind"),
        "confidence": round(_float(chosen.get("overall_score")) / 100.0, 4),
        "evidence": evidence,
        "alternatives": [item for item in evidence if not identities_match(chosen, item)],
        "review_reasons": review_reasons,
        "ambiguity_resolution": (None if any(item.get('kind') == 'acoustid' and item.get('audio_pass_conflict')
                                             for item in evidence) else _audio_ambiguity_resolution(chosen, evidence)),
        "reason": reason,
    }


def fuse_identity(
    *,
    acoustid_match=None,
    acoustid_safety=None,
    source_result=None,
    catalog_result=None,
    acr_result=None,
    acr_mb_result=None,
    shazam_result=None,
):
    """Собирает несколько recognition-layer в одно решение.

    SAFE здесь означает именно право построить автоматический Artist/Title plan.
    Release-dependent поля всё равно проходят ReleaseContext selector отдельно.
    """

    acoustid = _acoustid_evidence(acoustid_match, acoustid_safety)
    source = _source_evidence(source_result)
    catalog = _catalog_evidence(catalog_result)
    acr = _acr_evidence(acr_result)
    acr_conflict = _acr_conflict_evidence(acr_result)
    acr_mb = _acr_mb_evidence(acr_mb_result, acr_result)
    shazam = _shazam_evidence(shazam_result)

    evidence = [item for item in (acoustid, source, catalog, acr, acr_conflict, acr_mb, shazam) if item]
    if (shazam_result or {}).get('verification_incomplete') and not shazam:
        evidence.append({'kind': 'shazam_verification', 'strong': False, 'verification_incomplete': True})
    shazam_strong = shazam if shazam and shazam.get('strong') else None
    if (acr_result or {}).get("verification_incomplete") and not (acr or acr_conflict):
        # A failed position is still an unfinished check when the other
        # positions returned NOT_FOUND. Absence of a best candidate must
        # not make that failure disappear from consensus/write decisions.
        evidence.append({"kind": "acrcloud_verification", "strong": False,
                         "verification_incomplete": True})

    # 1) Самый сильный новый путь: реальное audio recognition ACRCloud,
    # exact ISRC и затем конкретный MusicBrainz Recording.
    if acr_mb:
        acr_ok = (
            acr is not None
            and not acr.get("version_conflict")
            and _float(acr.get("artist_score")) >= 90.0
            and _float(acr.get("title_score")) >= 92.0
            and (acr.get("acr_score") is None or _float(acr.get("acr_score")) >= 70.0)
        )
        # Exact ISRC уже является сильной связью аудио -> Recording.
        # Artist credit разных сервисов часто отличается только переносом
        # featured artist (AFK & Wooli vs AFK & Wooli feat. Jay Fresh),
        # поэтому не требуем искусственно высокий fuzzy overall.
        mb_ok = (
            _float(acr_mb.get("title_score")) >= 88.0
            and _artist_credits_compatible(
                acr.get("artist") if acr else "",
                acr_mb.get("artist") or "",
            )
            and not acr_mb.get("version_conflict")
        )

        if acr_ok and mb_ok:
            chosen = _with_richer_artist_credit(
                acr_mb,
                [acr, catalog, source],
            )
            return _result(
                "SAFE",
                chosen,
                evidence=evidence,
                reason=(
                    "ACRCloud STRONG дал ISRC, а MusicBrainz однозначно разрешил его "
                    "в Recording; Artist/Title согласованы с аудио и локальной identity."
                ),
            )

        return _result(
            "REVIEW",
            acr_mb,
            evidence=evidence,
            reason=(
                "ISRC разрешён в MusicBrainz, но межисточниковые оценки недостаточно "
                "высоки для автоматической записи Artist/Title."
            ),
        )

    # AcoustID мог быть REJECT только потому, что текущие локальные теги
    # потеряли VIP/Remix или часть credit. Если fingerprint и независимый
    # MusicBrainz catalog пришли к ТОЧНО тому же recording_id, это уже
    # достаточное подтверждение для исправления локальной metadata.
    if (
        catalog
        and acoustid
        and acoustid.get("safety") == "REJECT"
        and catalog.get("recording_id")
        and catalog.get("recording_id") == acoustid.get("recording_id")
        and _float(acoustid.get("acoustid_score")) >= 0.93
        and identities_match(catalog, acoustid)
        and _catalog_audio_confirmation_ok(catalog)
    ):
        return _result(
            "SAFE",
            catalog,
            evidence=evidence,
            reason=(
                "AcoustID и MusicBrainz catalog независимо указали на один и тот же "
                "Recording; конфликт был только с текущими локальными тегами."
            ),
        )

    # Два независимых аудио-распознавателя могут согласиться между собой и
    # одновременно конфликтовать с filename/тегами. Это признак вероятно
    # неверно подписанного файла. Никогда не пишем такое автоматически, но
    # вместо NOT_FOUND показываем пользователю вероятную реальную identity.
    if (
        acoustid
        and acoustid.get("safety") == "REJECT"
        and acr_conflict
        and _float(acoustid.get("acoustid_score")) >= 0.90
        and (acr_conflict.get("acr_score") is None or _float(acr_conflict.get("acr_score")) >= 90.0)
        and identities_match(acoustid, acr_conflict)
    ):
        return _result(
            "REVIEW",
            acoustid,
            evidence=evidence,
            reason=(
                "AcoustID и ACRCloud независимо распознали одну и ту же аудио-identity, "
                "но она конфликтует с filename/текущими тегами. Вероятно, файл подписан "
                "неверно; требуется ручное подтверждение."
            ),
        )

    # 2) MusicBrainz catalog даёт канонический Recording. Ищем независимое
    # подтверждение source/ACR/неотклонённого AcoustID.
    if catalog and catalog.get("recording_id"):
        positive = []
        weak_title_only = []

        for other in (source, acr, acoustid, shazam_strong):
            if not other:
                continue
            if other.get("kind") == "acoustid" and other.get("safety") == "REJECT":
                continue
            if other.get("version_conflict"):
                continue
            if identities_match(catalog, other):
                if other.get("artist"):
                    positive.append(other)
                else:
                    weak_title_only.append(other)

        if positive:
            chosen = _with_richer_artist_credit(
                catalog,
                positive,
            )
            return _result(
                "SAFE",
                chosen,
                evidence=evidence,
                reason=(
                    "MusicBrainz catalog дал конкретный Recording, и ту же Artist/Title "
                    "независимо подтвердил ещё один recognition-layer."
                ),
            )

        if weak_title_only and _catalog_is_exceptionally_strong(catalog):
            return _result(
                "SAFE",
                catalog,
                evidence=evidence,
                reason=(
                    "MusicBrainz дал почти точное Artist/Title/Duration совпадение, а "
                    "прямой source URL независимо подтвердил Title."
                ),
            )

        # REVIEW-AcoustID + почти точный MB catalog — сильная комбинация даже
        # без третьего источника: audio fingerprint уже присутствует.
        if (
            acoustid
            and acoustid.get("safety") == "REVIEW"
            and identities_match(catalog, acoustid)
            and _catalog_is_exceptionally_strong(catalog)
        ):
            return _result(
                "SAFE",
                catalog,
                evidence=evidence,
                reason=(
                    "AcoustID дал тот же Recording/identity с REVIEW-уровнем, а "
                    "MusicBrainz catalog подтвердил Artist/Title/Duration почти точно."
                ),
            )

        return _result(
            "REVIEW",
            catalog,
            evidence=evidence,
            reason=(
                "MusicBrainz catalog уверенно нашёл Recording, но независимого "
                "подтверждения пока недостаточно для SAFE Artist/Title."
            ),
        )

    # 3) AcoustID REVIEW с Recording можно повысить только независимым
    # сильным совпадением. REJECT никогда не повышаем сам по себе.
    if acoustid and acoustid.get("safety") == "REVIEW" and acoustid.get("recording_id"):
        supporters = [
            item for item in (source, catalog, acr, shazam_strong)
            if item and not item.get("version_conflict") and identities_match(acoustid, item)
        ]
        if supporters:
            return _result(
                "SAFE",
                acoustid,
                evidence=evidence,
                reason=(
                    "AcoustID-кандидат уровня REVIEW независимо подтверждён другим "
                    "сильным источником."
                ),
            )
        return _result(
            "REVIEW",
            acoustid,
            evidence=evidence,
            reason="AcoustID-кандидат правдоподобен, но нуждается в независимом подтверждении.",
        )

    # 4) Identity-only consensus. Это уже может безопасно править Artist/Title,
    # но без recording_id не пытаемся автоматически назначать ReleaseContext.
    strong_identity = [item for item in (acr, catalog, source, shazam_strong) if item]
    for index, first in enumerate(strong_identity):
        for second in strong_identity[index + 1:]:
            if first.get('kind') == 'shazam' or second.get('kind') == 'shazam':
                other = second if first.get('kind') == 'shazam' else first
                # Shazam and iTunes can share metadata; a text search built
                # from the local name is insufficient new audio confirmation.
                if (other.get('kind') != 'acrcloud' or _float(other.get('acr_score')) < 90
                        or other.get('verification_incomplete') or other.get('identity_ambiguous')):
                    continue
            if identities_match(first, second):
                with_artist = [item for item in (first, second) if item.get("artist")]

                # Title-only provenance не имеет права подтверждать усечённый Artist.
                # Для такого SAFE полный источник обязан практически идеально
                # совпадать с локальным Artist. FaceSplit & Code Pandorum -> Facesplit
                # теперь останется REVIEW, а Qoiet -> Qoiet сохранит SAFE.
                if len(with_artist) == 1 and not _title_only_support_is_safe(with_artist[0]):
                    continue

                # Базовый источник выбираем по качеству title/аудио, затем отдельно
                # восстанавливаем более полный совместимый artist credit.
                choices = sorted(
                    (first, second),
                    key=lambda item: (
                        bool(item.get("artist")),
                        item.get("kind") == "acrcloud",
                        item.get("kind") == "catalog",
                        _float(item.get("overall_score")),
                    ),
                    reverse=True,
                )
                chosen = _with_richer_artist_credit(choices[0], [first, second])
                if chosen.get("artist"):
                    return _result(
                        "SAFE",
                        chosen,
                        evidence=evidence,
                        reason=(
                            "Два независимых сильных источника подтверждают одну и ту же "
                            "Artist/Title identity. ReleaseContext при этом не назначается "
                            "без MusicBrainz recording_id."
                        ),
                    )

    # 5) Один сильный fallback всё равно полезен: покажем REVIEW вместо
    # полного BLOCKED/NOT_FOUND, но не будем автоматически писать.
    candidates = [item for item in (acr, catalog, source) if item and item.get("artist")]
    if candidates:
        chosen = max(candidates, key=lambda item: _float(item.get("overall_score")))
        return _result(
            "REVIEW",
            chosen,
            evidence=evidence,
            reason=(
                "Есть один сильный fallback-кандидат, но для автоматической записи "
                "нужно ещё одно независимое подтверждение."
            ),
        )

    if source:
        return _result(
            "REVIEW",
            source,
            evidence=evidence,
            reason=(
                "Source URL уверенно подтверждает Title, но Artist отсутствует; "
                "автоматически переписывать identity нельзя."
            ),
        )

    if shazam:
        return _result('REVIEW', shazam, evidence=evidence,
                       reason='Shazam нашёл аудио-кандидата; Artist/Title и все ответы по фрагментам сохранены для проверки.')

    # Incorrect local names and missing remix suffixes do not erase a strong
    # audio hit. Keep it for review, including every contradictory fragment.
    # It never gains automatic write permission through this path.
    if acr_conflict and (_float(acr_conflict.get("acr_score")) >= 90.0
                         or acr_conflict.get("identity_ambiguous")):
        return _result(
            "REVIEW", acr_conflict, evidence=evidence,
            reason=("ACRCloud уверенно распознал аудио, но оно конфликтует с локальными "
                    "данными или другими фрагментами. Кандидат сохранён для ручной "
                    "проверки; правильность трека и версии ещё не подтверждена."),
        )

    if acoustid and acoustid.get("safety") == "REJECT":
        return _result(
            "CONFLICT",
            acoustid,
            evidence=evidence,
            reason=(
                "AcoustID дал кандидата, но локальная Artist/Title или версия ему "
                "противоречат, а сильного fallback-подтверждения нет."
            ),
        )

    return _result(
        "NOT_FOUND",
        None,
        evidence=evidence,
        reason="Ни один слой не дал достаточно надёжной identity.",
    )
