import re
from pathlib import Path

from core.matcher import (
    strict_similarity,
    extract_version_markers,
    version_similarity,
)
from core.local_cleanup import (
    analyze_local_cleanup,
)
from core.naming import (
    canonical_album_name,
    canonical_artist_credit,
    canonical_artist_credit_from_items,
    canonical_track_title,
    canonical_entity_list,
    artist_roles_from_items,
    split_credit_entities,
    extract_feature_entities_from_title,
    extract_remixer_entities,
    split_feature_credit,
    ensure_feature_credit_in_title,
    artist_identity_records_from_items,
    clean_spaces,
)


def normalize_value(value):
    if not value:
        return ""

    value = str(value)

    value = value.replace("’", "'")
    value = value.replace("`", "'")

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip().casefold()


def clean_duplicate_brackets(value):
    if not value:
        return value

    value = str(value)

    previous = None

    while previous != value:
        previous = value

        value = re.sub(
            r"\(\(([^()]*)\)\)",
            r"(\1)",
            value,
        )

        value = re.sub(
            r"\[\[([^\[\]]*)\]\]",
            r"[\1]",
            value,
        )

    return value


def values_equal(a, b):
    return (
        normalize_value(a)
        == normalize_value(b)
    )


def normalize_display_value(
    value,
):
    """
    Нормализация для сравнения
    конечного написания тега.

    Убираем технические различия:
    - лишние пробелы;
    - разные варианты апострофа.

    Но РЕГИСТР сохраняем.
    """

    if value is None:
        return ""

    value = str(value)

    value = value.replace(
        "’",
        "'",
    )

    value = value.replace(
        "`",
        "'",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def display_values_equal(
    a,
    b,
):
    return (
        normalize_display_value(a)
        == normalize_display_value(b)
    )


def split_artist_names(value):
    """
    Приводит строку кредита к набору артистов.

    Например:
    ShockOne feat. Phetsta
    ->
    {"shockone", "phetsta"}
    """

    if not value:
        return set()

    value = str(value)

    value = re.sub(
        r"\b(?:feat\.?|ft\.?|featuring|with|vs\.?)\b",
        ",",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s+[x×]\s+",
        ",",
        value,
        flags=re.IGNORECASE,
    )

    value = value.replace(
        "&",
        ",",
    )

    value = value.replace(
        ";",
        ",",
    )

    parts = value.split(",")

    return {
        normalize_value(part)
        for part in parts
        if normalize_value(part)
    }


def extract_credit_artist_names(
    artist_credit,
):
    """
    Разбирает отформатированный
    MusicBrainz Artist credit
    на отдельных исполнителей.
    """

    if not artist_credit:
        return set()

    value = str(
        artist_credit
    ).strip()

    # feat. / ft. / featuring
    value = re.sub(
        r"\s+"
        r"(?:feat\.?|ft\.?|featuring)"
        r"\s+",
        ",",
        value,
        flags=re.IGNORECASE,
    )

    # Остальные join phrases.
    value = re.sub(
        r"\s+"
        r"(?:&|×|x|and)"
        r"\s+",
        ",",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"\s*;\s*",
        ",",
        value,
    )

    result = set()

    for part in value.split(","):
        name = part.strip()

        if name:
            result.add(
                name
            )

    return result


def _normalize_credit_text(
    value,
):
    """
    Нормализация только для поиска
    имени исполнителя внутри строки.

    Ничего не записывает в теги.
    """

    if not value:
        return ""

    value = str(
        value
    ).casefold()

    value = re.sub(
        r"[^\w]+",
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


def _text_contains_artist(
    text,
    artist_name,
):
    """
    Проверяет, встречается ли имя
    исполнителя внутри Title.

    Например:

    Title:
    Outbreak (аeat Armanni Reign)

    Artist:
    Armanni Reign

    -> True

    Слово feat./ft. при этом вообще
    распознавать не требуется.
    """

    text_norm = (
        _normalize_credit_text(
            text
        )
    )

    artist_norm = (
        _normalize_credit_text(
            artist_name
        )
    )

    if not (
        text_norm
        and artist_norm
    ):
        return False

    return (
        f" {artist_norm} "
        in
        f" {text_norm} "
    )


def _artist_plan_final_value(
    artist_plan,
):
    """
    Возвращает Artist, который реально
    останется после применения плана.

    REVIEW/BLOCKED/ASK считаются
    неподтверждёнными.
    """

    status = (
        artist_plan.get(
            "status"
        )
        or ""
    )

    if status == "SAFE":
        return (
            artist_plan.get(
                "proposed"
            )
            or ""
        )

    if status == "KEEP":
        return (
            artist_plan.get(
                "current"
            )
            or ""
        )

    return ""


def protect_title_artist_credits(
    current_title,
    title_plan,
    artist_plan,
    artist_credit,
):
    """
    Последняя страховка перед записью.

    Если SAFE-изменение Title удаляет имя
    официального исполнителя, то разрешаем
    его только если Artist уже гарантированно
    содержит этого исполнителя.

    Это работает даже при сломанном:

    feat.
    ft.
    аeat
    fеat
    других мусорных разделителях.
    """

    if not title_plan:
        return title_plan

    if (
        title_plan.get("status")
        != "SAFE"
    ):
        return title_plan

    proposed_title = (
        title_plan.get(
            "proposed"
        )
    )

    if not (
        current_title
        and proposed_title
        and artist_credit
    ):
        return title_plan

    official_names = (
        extract_credit_artist_names(
            artist_credit
        )
    )

    if not official_names:
        return title_plan

    removed_names = []

    for artist_name in (
        official_names
    ):
        was_in_title = (
            _text_contains_artist(
                current_title,
                artist_name,
            )
        )

        remains_in_title = (
            _text_contains_artist(
                proposed_title,
                artist_name,
            )
        )

        if (
            was_in_title
            and not remains_in_title
        ):
            removed_names.append(
                artist_name
            )

    if not removed_names:
        return title_plan

    final_artist = (
        _artist_plan_final_value(
            artist_plan
        )
    )

    # Если все исчезнувшие из Title
    # исполнители гарантированно находятся
    # в итоговом Artist — всё нормально.
    missing_from_artist = []

    for artist_name in removed_names:
        if not _text_contains_artist(
            final_artist,
            artist_name,
        ):
            missing_from_artist.append(
                artist_name
            )

    if not missing_from_artist:
        return title_plan

    names = ", ".join(
        sorted(
            missing_from_artist
        )
    )

    return {
        **title_plan,

        "status": "REVIEW",

        "reason": (
            "Новое название убирает "
            "из Title исполнителя: "
            f"{names}. "
            "Поле Artist ещё не гарантирует "
            "сохранение этого credit, поэтому "
            "Title нельзя изменять отдельно."
        ),

        "dependency_group": (
            "artist_title_credit"
        ),
    }


FEATURE_CREDIT_RE = re.compile(
    r"\b(?:feat\.?|ft\.?|featuring)\b",
    flags=re.IGNORECASE,
)


def has_feature_credit(value):
    if not value:
        return False

    return bool(
        FEATURE_CREDIT_RE.search(
            str(value)
        )
    )


def make_field(
    current=None,
    proposed=None,
    status="KEEP",
    reason="",
    options=None,
):
    return {
        "current": current or "",
        "proposed": proposed,
        "status": status,
        "reason": reason,
        "options": options or [],
    }


def _append_reason(reason, extra):
    reason = (reason or "").strip()
    extra = (extra or "").strip()

    if not reason:
        return extra
    if not extra:
        return reason
    return reason + " " + extra


def _filename_artist_text(file_path):
    if not file_path:
        return ""

    stem = Path(str(file_path)).stem
    if " - " not in stem:
        return ""

    return stem.split(" - ", 1)[0].strip()


def _apply_style_to_field_plan(
    field_plan,
    *,
    canonicalizer,
    identity_safety,
    references=None,
    field_label,
):
    """Apply final presentation style without changing recognition semantics.

    SAFE stays SAFE, REVIEW stays REVIEW. KEEP may become SAFE only when the
    recording itself is already SAFE and the canonicalizer changes formatting
    rather than identity. ASK/BLOCKED are never upgraded.
    """
    field_plan = dict(field_plan or {})
    status = field_plan.get("status") or "KEEP"

    if status in {"ASK", "BLOCKED"}:
        return field_plan

    current = field_plan.get("current") or ""
    proposed = field_plan.get("proposed")

    if status in {"SAFE", "REVIEW"} and proposed is not None:
        base_value = proposed
    else:
        base_value = current

    try:
        styled = canonicalizer(
            base_value,
            references=references or [],
        )
    except TypeError:
        styled = canonicalizer(base_value)

    if not styled or display_values_equal(base_value, styled):
        return field_plan

    style_reason = (
        f"{field_label} приведён к единому стилю TagCleaner: "
        "единые разделители и оформление без изменения identity."
    )

    if status == "SAFE":
        field_plan["proposed"] = styled
        field_plan["reason"] = _append_reason(
            field_plan.get("reason"),
            style_reason,
        )
        return field_plan

    if status == "REVIEW":
        # Пользователь всё равно должен подтвердить смысловое изменение, но
        # вариант в интерфейсе уже показываем в едином формате.
        field_plan["proposed"] = styled
        field_plan["reason"] = _append_reason(
            field_plan.get("reason"),
            style_reason,
        )
        return field_plan

    if status == "KEEP" and identity_safety == "SAFE":
        return make_field(
            current=current,
            proposed=styled,
            status="SAFE",
            reason=style_reason,
        )

    return field_plan


def _filename_title_text(file_path):
    if not file_path:
        return ""

    stem = Path(str(file_path)).stem
    if " - " not in stem:
        return stem.strip()

    return stem.split(" - ", 1)[1].strip()


def _artist_entity_key(value):
    value = normalize_value(value)
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE)


def _likely_same_credit_spelling(left, right):
    """Conservative typo/variant match inside one artist role.

    Recognition sources often disagree by one or two letters in a person's
    surname (for example Mikey Ceasar/Caeser/Ceaser).  Those variants must not
    become three featured artists.  This helper is intentionally narrower than
    general artist matching: both values must contain the same number of words,
    all but at most one word must match exactly, and the differing word must
    start with the same letter and remain reasonably similar.
    """
    left_tokens = [
        token for token in re.findall(r"[\w']+", clean_spaces(left).casefold(), flags=re.UNICODE)
        if token
    ]
    right_tokens = [
        token for token in re.findall(r"[\w']+", clean_spaces(right).casefold(), flags=re.UNICODE)
        if token
    ]

    if len(left_tokens) < 2 or len(left_tokens) != len(right_tokens):
        return False

    differing = []
    for a, b in zip(left_tokens, right_tokens):
        if a == b:
            continue
        differing.append((a, b))

    if len(differing) != 1:
        return False

    a, b = differing[0]
    if not a or not b or a[0] != b[0]:
        return False
    if abs(len(a) - len(b)) > 1:
        return False

    return strict_similarity(a, b) >= 0.64


def _entity_matches_any(name, values):
    key = _artist_entity_key(name)
    if not key:
        return False

    for value in values or []:
        other = _artist_entity_key(value)
        if not other:
            continue
        if key == other:
            return True
        # Alias/punctuation tolerance without making short names too fuzzy.
        if len(key) >= 5 and len(other) >= 5:
            if strict_similarity(key, other) >= 0.92:
                return True
        # Role-local spelling tolerance.  Keep this deliberately narrow so
        # unrelated artists are never merged merely because their names look
        # somewhat alike.
        if _likely_same_credit_spelling(name, value):
            return True
    return False


def _append_unique_entity(values, name):
    name = str(name or "").strip()
    if not name:
        return
    if _entity_matches_any(name, values):
        return
    values.append(name)


def _credit_part_keys(value):
    parts = split_credit_entities(value)
    if not parts and value:
        parts = [value]
    return [_artist_entity_key(part) for part in parts if _artist_entity_key(part)]


def _extra_credit_entities(flat_credit, known_entities, references):
    """Return names present in a flat consensus credit but absent from MB.

    Known structured entities are consumed by their component tokens.  This
    keeps an atomic entity such as ``Dodge & Fuski`` intact in the structured
    model while still allowing us to discover a richer extra collaborator
    such as ``PhaseOne`` from a flat fallback credit.
    """
    parts = split_credit_entities(flat_credit, references=references)
    if not parts:
        return []

    known_counts = {}
    for entity in known_entities or []:
        for key in _credit_part_keys(entity):
            known_counts[key] = known_counts.get(key, 0) + 1

    extras = []
    for part in parts:
        key = _artist_entity_key(part)
        if not key:
            continue
        if known_counts.get(key, 0) > 0:
            known_counts[key] -= 1
            continue
        _append_unique_entity(extras, part)

    return extras


def _source_parenthetical_project_members(data, structured_primary):
    """Return names shown only as a parenthetical expansion of a project.

    Some providers expose a project together with its members, for example
    ``1000volts (Redman & Jayceeoh)``.  ACRCloud may then flatten that into
    ``1000volts & Redman & Jayceeoh & ...``.  When MusicBrainz already gives
    us a structured primary entity ``1000volts``, the names inside those
    parentheses are explanatory aliases/members, not extra track artists.

    This evidence is deliberately narrow: it is used only to *suppress* an
    extra primary promotion, never to invent an artist identity.
    """
    source_identity = data.get("source_url_identity") or {}
    best = source_identity.get("best") or {}
    source_artist = clean_spaces(best.get("source_artist") or "")
    if not source_artist:
        return []

    match = re.fullmatch(r"(.+?)\s*\(([^()]*)\)\s*", source_artist)
    if not match:
        return []

    outer = clean_spaces(match.group(1))
    inner = clean_spaces(match.group(2))
    if not outer or not inner:
        return []

    if not _entity_matches_any(outer, structured_primary or []):
        return []

    members = []
    for name in split_credit_entities(inner, references=[inner]):
        _append_unique_entity(members, name)
    return members


def _title_role_hints(data, title_value, artist_value):
    feature_artists = []
    feature_role = ""
    remixer_artists = []

    # Local tags and the filename can describe a different song/version.
    # They remain review evidence, but must not manufacture a hybrid candidate
    # (e.g. Ebrahim's title + Bronyfied from an unrelated Aviators remix), or
    # remove a candidate's primary artist because an old title calls it a remixer.
    from core.identity_fusion import identities_match
    chosen = {"artist": artist_value, "title": title_value}
    title_values = [title_value]
    for local_artist, local_title in (
        (data.get("artist"), data.get("title")),
        (_filename_artist_text(data.get("path")), _filename_title_text(data.get("path"))),
    ):
        if (local_artist and local_title and artist_value
                and identities_match({"artist": local_artist, "title": local_title}, chosen)):
            title_values.append(local_title)

    # A flat fallback artist credit may carry the featuring role even when
    # the chosen Recording title does not.
    _, flat_role, flat_feature = split_feature_credit(artist_value)
    if flat_feature:
        feature_role = flat_role or feature_role or "feat."
        for name in split_credit_entities(flat_feature):
            _append_unique_entity(feature_artists, name)

    for value in title_values:
        hint = extract_feature_entities_from_title(value)
        if hint.get("role") == "with":
            confirmed_names = split_credit_entities(artist_value)
            for item in (_musicbrainz_for_final_identity(data).get("artist-credit") or []):
                if isinstance(item, dict):
                    confirmed_names.append(item.get("name") or (item.get("artist") or {}).get("name") or "")
            if not all(_entity_matches_any(name, confirmed_names) for name in hint.get("artists") or []):
                hint = {"role": "", "artists": []}
        if hint.get("role") and not feature_role:
            feature_role = hint.get("role")
        for name in hint.get("artists") or []:
            _append_unique_entity(feature_artists, name)

        for name in extract_remixer_entities(value):
            _append_unique_entity(remixer_artists, name)

    # A cached/flat Recording credit can lose a role that the concrete track
    # still states explicitly. Reuse only same-song, same-version evidence.
    from core.editor_identity import role_evidence
    for evidence in role_evidence(data,artist_value,title_value):
        for name in split_credit_entities(evidence['featured']):
            if not _entity_matches_any(name,remixer_artists):
                _append_unique_entity(feature_artists,name)

    return {
        "featured": feature_artists,
        "feature_role": feature_role or "feat.",
        "remixers": remixer_artists,
    }


def _musicbrainz_for_final_identity(data):
    """Return MusicBrainz data only when it belongs to the final identity.

    The first AcoustID pass may fetch a MusicBrainz Recording even when that
    candidate is later REJECT/CONFLICT. If a fallback consensus then identifies
    a different track (especially without a MusicBrainz recording_id), the old
    ``data["musicbrainz"]`` must not influence write-side artist roles.
    """
    musicbrainz = data.get("musicbrainz") or {}
    fusion = data.get("fallback_fusion") or {}

    if fusion.get("status") in {"SAFE", "REVIEW"}:
        fusion_recording_id = str(fusion.get("recording_id") or "").strip()
        mb_recording_id = str(musicbrainz.get("id") or "").strip()

        # No MB anchor for the final fallback identity -> any previously
        # fetched Recording may be stale and is therefore ignored.
        if not fusion_recording_id:
            return {}

        # When a fallback is anchored to MB, only the exact same Recording may
        # provide structured role information.
        if not mb_recording_id or mb_recording_id != fusion_recording_id:
            return {}

    return musicbrainz


def _resolve_write_artist_roles(
    data,
    artist_value,
    title_value,
    references,
    release_context_selection=None,
):
    """Resolve write-side primary/featured/remixer roles.

    Recognition is intentionally permissive.  This function runs only after
    identity has been chosen (SAFE or provisional REVIEW) and uses structured MusicBrainz artist-credit
    whenever available *for the final identity*. Richer consensus names are
    preserved, but an extra name that is explicitly the remixer is never
    promoted to primary Artist.
    """
    musicbrainz = _musicbrainz_for_final_identity(data)
    from core.release_credits import authority
    declared=authority(data,artist_value,title_value,release_context_selection)
    if declared and declared.get('conflict'):
        # Preserve the entered pair and force review rather than fall back to
        # an older recording credit after a release/version conflict.
        return {'primary':split_credit_entities(artist_value),'featured':[],
                'remixers':[],'conflict':declared['reason'],'preserve_identity':declared.get('preserve_identity')}
    if declared and not declared.get('conflict') and declared['exact']:
        from core.release_credits import apply as apply_declared
        styled=apply_declared(artist_value,title_value,declared)
        from core.release_credits import preserve_spelling
        primary=preserve_spelling(declared['primary'],artist_value,title_value)
        # Keep unclassified extra collaborators rather than silently losing them.
        extra=_extra_credit_entities(styled['artist'],primary,references)
        return {'primary':[*primary,*extra],'featured':preserve_spelling(declared['featured'],artist_value,title_value),
                'feature_role':declared.get('feature_role') or 'feat.','remixers':extract_remixer_entities(title_value),
                'structured':True,'release_specific_credit':declared['source']!='MusicBrainz Recording',
                'primary_aliases':declared.get('primary_aliases') or primary,
                'authoritative_title':styled['title'],
                'authoritative_primary':True,'explicit_feature_evidence':bool(declared['featured']),
                'explicit_remixer_evidence':bool(extract_remixer_entities(title_value))}
    mb_items = musicbrainz.get("artist-credit") or []

    # Prefer the artist credit from the concrete selected release/track.
    # MusicBrainz can keep the stable artist entity under a new current name
    # while ``artist-credit[].name`` preserves how the artist was actually
    # credited on an older release.  Using the release-specific credit here
    # prevents retroactive rewrites such as Mastadon -> MARAUDA or
    # Spag Heddy -> SPAG on historical files, and also preserves intentional
    # casing such as SKisM / oddprophet / heRobust.
    selection = release_context_selection or {}
    selected_context = (
        selection.get("selected")
        if selection.get("status") == "SELECTED"
        else None
    ) or {}
    selected_credit_items = selected_context.get("track_artist_credit_items") or []
    role_items = selected_credit_items or mb_items
    original_role_items = role_items
    name_preference = data.get("_artist_name_preference", "historical")
    if name_preference == "current":
        role_items = [({**item, "name": (item.get("artist") or {}).get("name") or item.get("name")}
                       if isinstance(item, dict) else item) for item in role_items]
    mb_roles = artist_roles_from_items(role_items)
    hints = _title_role_hints(data, title_value, artist_value)

    # Release-level role evidence is useful when a Recording artist-credit
    # flattens a featured guest into the primary list.  We only trust this
    # signal for a concrete SELECTED release whose Album Artist actually
    # matches at least one of the Recording primary artists.  This avoids
    # treating ``Various Artists`` compilations as role authority.
    selected_albumartist = clean_spaces(
        selected_context.get("album_artist_credit") or ""
    )
    selected_albumartist_is_role_authority = bool(
        selected_albumartist
        and normalize_value(selected_albumartist) != "various artists"
        and any(
            _text_contains_artist(selected_albumartist, name)
            for name in (mb_roles.get("primary") or [])
        )
    )

    filename_primary = split_credit_entities(_filename_artist_text(data.get("path")))
    primary = []
    featured = []
    feature_role = mb_roles.get("feature_role") or hints.get("feature_role") or "feat."

    if role_items:
        for name in mb_roles.get("primary") or []:
            # Explicit X Remix is a stronger write-side role signal than a
            # flat primary listing from a noisy third-party credit.
            if (_entity_matches_any(name, hints.get("remixers"))
                    and not _entity_matches_any(name, filename_primary)):
                continue

            # A selected release can disambiguate a guest role that MB's
            # Recording credit flattened into the primary list. Example:
            #   Recording: Sullivan King, Matt McGuire & Sam King
            #   filename/ACR title: Lockdown (feat. Sam King)
            #   selected Album Artist: Sullivan King & Matt McGuire
            # Apple-style output should keep Sam King as featured.
            if (
                selected_albumartist_is_role_authority
                and _entity_matches_any(name, hints.get("featured"))
                and not _text_contains_artist(selected_albumartist, name)
            ):
                _append_unique_entity(featured, name)
                continue

            _append_unique_entity(primary, name)

        for name in mb_roles.get("featured") or []:
            if _entity_matches_any(name, hints.get("remixers")):
                continue
            _append_unique_entity(featured, name)

        known = list(mb_roles.get("primary") or []) + list(mb_roles.get("featured") or [])

        # Treat the current/global entity name as an alias of the same artist
        # when MusicBrainz gives us a release-specific ``credited-as`` name.
        # Otherwise a historical credit such as Mastadon could be followed by
        # a bogus extra collaborator MARAUDA even though both names share one
        # stable MBID.
        for identity in artist_identity_records_from_items(original_role_items):
            for alias in identity.get("aliases") or []:
                _append_unique_entity(known, alias)

        extras = _extra_credit_entities(artist_value, known, references)
        if not data.get("fallback_fusion"):
            # Do not promote uploader/tag noise, but preserve collaborators
            # confirmed by the filename and the already selected audio result.
            corroborated = split_credit_entities(_filename_artist_text(data.get("path")))
            corroborated += ((data.get("best_match") or {}).get("candidate") or {}).get("artists") or []
            extras = [name for name in extras if _entity_matches_any(name, known + corroborated)]
        project_members = _source_parenthetical_project_members(
            data,
            mb_roles.get("primary") or [],
        )

        for name in extras:
            if _entity_matches_any(name, hints.get("remixers")):
                continue
            if _entity_matches_any(name, hints.get("featured")):
                _append_unique_entity(featured, name)
            elif _entity_matches_any(name, project_members):
                # Provider metadata such as ``1000volts (Redman & Jayceeoh)``
                # describes the project, it does not add Redman/Jayceeoh as
                # separate primary artists on this recording.  Structured MB
                # credit remains authoritative for the track credit.
                continue
            else:
                # A richer independent consensus can legitimately restore a
                # collaborator missing from MusicBrainz (Kayzo & Krimer,
                # WoodenToaster & Mic the Microphone, etc.).
                _append_unique_entity(primary, name)
    else:
        # No structured authority: use explicit feat/remix syntax as role
        # evidence and keep everything else as primary collaborators.
        flat_primary, flat_role, flat_feature = split_feature_credit(
            canonical_artist_credit(artist_value, references=references)
        )
        audio_entities = data.get("_provisional_audio_artist_names") or []

        if flat_feature:
            feature_role = flat_role or feature_role
            for name in split_credit_entities(flat_primary, references=references):
                if (not _entity_matches_any(name, hints.get("remixers"))
                        or _entity_matches_any(name, filename_primary)):
                    _append_unique_entity(primary, name)
            for name in split_credit_entities(flat_feature):
                if not _entity_matches_any(name, hints.get("remixers")):
                    _append_unique_entity(featured, name)
        else:
            for name in (audio_entities or split_credit_entities(artist_value, references=references)):
                if (_entity_matches_any(name, hints.get("remixers"))
                        and not _entity_matches_any(name, filename_primary)):
                    continue
                if _entity_matches_any(name, hints.get("featured")):
                    _append_unique_entity(featured, name)
                else:
                    _append_unique_entity(primary, name)

        for name in hints.get("featured") or []:
            if not _entity_matches_any(name, primary):
                _append_unique_entity(featured, name)

    # Structured primary role wins over a misleading local "feat." spelling.
    featured = [
        name for name in featured
        if not _entity_matches_any(name, primary)
    ]

    # The collection's explicit feat. convention applies even when a flat
    # provider credit labels every participant as primary. Keep collaborators
    # unless a literal featured credit identifies the guest.
    from core.credit_style import credit_values
    role_title=title_value
    if hints.get('featured'):
        role_title=ensure_feature_credit_in_title(role_title,'feat.',canonical_entity_list(hints['featured']))
    styled=credit_values(canonical_entity_list(primary),role_title)
    if styled['artist']!=canonical_entity_list(primary) and not role_items:
        removed=[name for name in primary if not _text_contains_artist(styled['artist'],name)]
        if removed and styled['artist']:
            primary=[name for name in primary if name not in removed]
            for name in removed:_append_unique_entity(featured,name)

    return {
        "primary": primary,
        "featured": featured,
        "feature_role": feature_role,
        "remixers": hints.get("remixers") or [],
        "structured": bool(role_items),
        "release_specific_credit": bool(selected_credit_items),
        "explicit_feature_evidence": bool(hints.get("featured")),
        "explicit_remixer_evidence": bool(hints.get("remixers")),
    }


def _review_title_can_be_promoted_for_roles(title_plan, roles, current_title, styled_title):
    """Whether a REVIEW title may become SAFE for role-only formatting.

    ``plan_title`` intentionally marks a lone Recording.title as REVIEW when
    no concrete release Track.title is available.  Once identity itself is
    SAFE, structured artist-role evidence may still safely move a confirmed
    featured guest from Artist into Title.  We never use this shortcut for a
    review caused by version loss/conflict.
    """
    if (title_plan or {}).get("status") != "REVIEW":
        return False

    # If canonical role formatting lands exactly on the existing tag, there
    # is nothing to write.  Let _restyle_plan_value collapse it to KEEP rather
    # than manufacturing a no-op SAFE operation.
    if display_values_equal(current_title or "", styled_title or ""):
        return False

    reason = str((title_plan or {}).get("reason") or "").casefold()
    if any(token in reason for token in (
        "теряет информацию о версии",
        "version",
        "remix/vip/edit",
    )):
        return False

    # This promotion is only for the conservative "Recording.title exists,
    # but release Track.title is absent" path.
    recording_only = (
        "recording.title" in reason
        or "официальный track.title" in reason
    )
    if not recording_only:
        return False

    if not (
        roles.get("structured")
        or roles.get("explicit_feature_evidence")
        or roles.get("explicit_remixer_evidence")
    ):
        return False

    # Role formatting must not silently change the recording version.
    # Compare semantically rather than requiring identical marker lists: a
    # dirty current title may contain an uploader prefix (``Artist - Title``)
    # that makes the low-level extractor emit an extra noisy marker even
    # though the actual ``Grabbitz Edit``/``Faytal Remix`` is unchanged.
    current_versions = extract_version_markers(current_title or "")
    styled_versions = extract_version_markers(styled_title or "")
    if current_versions or styled_versions:
        _version_score, version_mismatch = version_similarity(
            current_title or "",
            styled_title or "",
        )
        if version_mismatch:
            return False

    # Featured placement and a confirmed trailing remixer are both
    # presentation-only role changes once the SAFE identity/version is fixed.
    return bool(roles.get("featured") or roles.get("remixers"))


def _promote_review_title_if_canonical_equivalent(title_plan, safety):
    """Promote a REVIEW title when the difference is presentation-only.

    This is deliberately narrower than ordinary fuzzy similarity.  The
    identity must already be SAFE, the REVIEW must come from the conservative
    Recording.title-only path, and canonical formatting of the current and
    proposed values must become exactly the same display string.  Example:
    ``Bad Drip - Faytal Remix`` -> ``Bad Drip (Faytal Remix)``.
    """
    field_plan = dict(title_plan or {})
    if safety != "SAFE" or field_plan.get("status") != "REVIEW":
        return field_plan

    current = field_plan.get("current") or ""
    proposed = field_plan.get("proposed") or ""
    if not current or not proposed:
        return field_plan

    # Exact canonical equivalence is safe regardless of which conservative
    # planner branch produced REVIEW, but never collapse a real version
    # difference (Original/Remix/VIP/etc.) into a cosmetic rewrite.
    _version_score, version_mismatch = version_similarity(current, proposed)
    if version_mismatch:
        return field_plan

    current_styled = canonical_track_title(current)
    proposed_styled = canonical_track_title(proposed)
    if not current_styled or not proposed_styled:
        return field_plan

    if not display_values_equal(current_styled, proposed_styled):
        return field_plan

    if display_values_equal(current, proposed_styled):
        return make_field(
            current=current,
            proposed=proposed_styled,
            status="KEEP",
            reason=_append_reason(
                field_plan.get("reason"),
                "После канонизации Title уже совпадает с текущим тегом.",
            ),
        )

    return make_field(
        current=current,
        proposed=proposed_styled,
        status="SAFE",
        reason=_append_reason(
            field_plan.get("reason"),
            "SAFE identity: различие между текущим Title и Recording.title "
            "только в каноническом оформлении версии трека.",
        ),
    )


def _restyle_plan_value(
    field_plan,
    styled,
    *,
    safety,
    field_label,
    reason,
    allow_keep_to_review=False,
):
    field_plan = dict(field_plan or {})
    status = field_plan.get("status") or "KEEP"
    if status in {"ASK", "BLOCKED"}:
        return field_plan

    current = field_plan.get("current") or ""
    proposed = field_plan.get("proposed")
    base = proposed if status in {"SAFE", "REVIEW"} and proposed is not None else current

    if not styled:
        return field_plan

    # If role-aware formatting cancels a bad intermediate proposal and the
    # resulting value already equals the current tag, there is nothing to
    # write.  Keep identity safety in the parent plan, not as a fake field op.
    if display_values_equal(current, styled):
        return make_field(
            current=current,
            proposed=styled,
            status="KEEP",
            reason=_append_reason(field_plan.get("reason"), reason),
        )

    if display_values_equal(base, styled):
        return field_plan

    if status == "REVIEW":
        field_plan["proposed"] = styled
        field_plan["reason"] = _append_reason(field_plan.get("reason"), reason)
        return field_plan

    if status == "SAFE":
        field_plan["proposed"] = styled
        field_plan["reason"] = _append_reason(field_plan.get("reason"), reason)
        return field_plan

    if status == "KEEP" and safety == "SAFE":
        return make_field(
            current=current,
            proposed=styled,
            status="SAFE",
            reason=reason,
        )

    if status == "KEEP" and safety == "REVIEW" and allow_keep_to_review:
        return make_field(
            current=current,
            proposed=styled,
            status="REVIEW",
            reason=_append_reason(
                field_plan.get("reason"),
                reason,
            ),
        )

    return field_plan


def apply_final_naming_style(
    *,
    data,
    safety,
    artist_plan,
    title_plan,
    album_plan,
    albumartist_plan,
    release_context_selection=None,
):
    """Role-aware final presentation layer.

    Search/matching continues to accept source-specific spellings. Only the
    write plan is normalized. Primary collaborators, featuring/with artists
    and remixers are kept as different roles instead of flattening every name
    into one Artist string.
    """
    current_artist = data.get("artist") or ""
    filename_artist = _filename_artist_text(data.get("path"))
    artist_references = [current_artist, filename_artist]

    # Canonicalize the chosen title first; role placement may then add feat.
    title_plan = _apply_style_to_field_plan(
        title_plan,
        canonicalizer=canonical_track_title,
        identity_safety=safety,
        field_label="Title",
    )
    title_plan = _promote_review_title_if_canonical_equivalent(
        title_plan,
        safety,
    )

    artist_status = (artist_plan or {}).get("status") or "KEEP"
    title_status = (title_plan or {}).get("status") or "KEEP"

    # A SAFE identity is allowed to resolve roles even when Title is REVIEW
    # solely because only Recording.title (not release Track.title) exists.
    # This is crucial for cases such as GTA feat. Sam Bruno and Polarr feat.
    # Bien: writing "GTA feat. Sam Bruno" into Artist would violate our
    # canonical role model.
    if (
        safety in {"SAFE", "REVIEW"}
        and artist_status in {"KEEP", "SAFE", "REVIEW"}
        and title_status in {"KEEP", "SAFE", "REVIEW"}
        and (
            safety == "SAFE"
            or artist_status == "REVIEW"
            or title_status == "REVIEW"
        )
    ):
        artist_value = (
            artist_plan.get("proposed")
            if artist_status in {"SAFE", "REVIEW"}
            and artist_plan.get("proposed") is not None
            else artist_plan.get("current") or ""
        )
        title_value = (
            title_plan.get("proposed")
            if title_status in {"SAFE", "REVIEW"}
            and title_plan.get("proposed") is not None
            else title_plan.get("current") or ""
        )

        roles = _resolve_write_artist_roles(
            data,
            artist_value,
            title_value,
            artist_references,
            release_context_selection=release_context_selection,
        )

        styled_artist = canonical_entity_list(roles.get("primary"))
        if not styled_artist:
            styled_artist = canonical_artist_credit(
                artist_value,
                references=artist_references,
            )

        styled_title = canonical_track_title(roles.get('authoritative_title',title_value))
        if roles.get('authoritative_primary'):
            from core.release_credits import remove_primary_features
            styled_title=remove_primary_features(styled_title,roles.get('primary_aliases') or roles['primary'])
        featured = canonical_entity_list(roles.get("featured"))
        # A flat structured primary credit does not justify dropping an
        # explicit title credit from the same recording. Keep those names in
        # Artist until their role is resolved, but retain feat. in Title too.
        if not featured and not roles.get('conflict'):
            if roles.get('authoritative_primary'):
                from core.naming import extract_feature_entities_from_title
                hints={'featured':extract_feature_entities_from_title(title_value).get('artists') or []}
            else:
                hints = _title_role_hints(data, title_value, artist_value)
            retained_guests = [name for name in hints.get('featured') or []
                               if not _entity_matches_any(name, roles.get('remixers')) and not _entity_matches_any(name,roles.get('primary_aliases') or roles.get('primary'))]
            featured = canonical_entity_list(retained_guests)
        if featured:
            styled_title = ensure_feature_credit_in_title(
                styled_title,
                roles.get("feature_role") or "feat.",
                featured,
            )

        if roles.get('conflict'):
            preserved=roles.get('preserve_identity') or {'artist':artist_value,'title':title_value}
            styled_artist=preserved['artist']
            styled_title=preserved['title']
        artist_plan = _restyle_plan_value(
            artist_plan,
            styled_artist,
            safety=safety,
            field_label="Artist",
            reason=(
                "Role-aware стиль: в Artist остаются только primary "
                "исполнители; featured/remixer роли не смешиваются с ними."
            ),
            allow_keep_to_review=(safety == "REVIEW"),
        )

        if (
            safety == "SAFE"
            and title_status == "REVIEW"
            and _review_title_can_be_promoted_for_roles(
                title_plan, roles, title_plan.get("current") or "", styled_title
            )
        ):
            title_plan = make_field(
                current=title_plan.get("current") or "",
                proposed=styled_title,
                status="SAFE",
                reason=_append_reason(
                    title_plan.get("reason"),
                    "SAFE identity и подтверждённые роли артистов позволяют "
                    "выполнить только каноническое размещение featured-credit.",
                ),
            )
        else:
            title_plan = _restyle_plan_value(
                title_plan,
                styled_title,
                safety=safety,
                field_label="Title",
                reason=(
                    "Role-aware стиль: featured/with оформлены в Title, "
                    "а Remix/VIP/Edit остаются версией трека."
                ),
                allow_keep_to_review=(safety == "REVIEW"),
            )

            # Never allow a SAFE-only write to remove a guest from Artist
            # while the coordinated Title placement is still REVIEW.
            if (
                title_plan.get("status") == "REVIEW"
                and roles.get("featured")
                and not display_values_equal(
                    title_plan.get("current") or "",
                    styled_title,
                )
                and artist_plan.get("status") == "SAFE"
            ):
                artist_plan["status"] = "REVIEW"
                artist_plan["reason"] = _append_reason(
                    artist_plan.get("reason"),
                    "Artist/Title role-credit должны изменяться вместе; Title "
                    "пока требует REVIEW.",
                )
        if roles.get('conflict'):
            for plan in (artist_plan,title_plan):
                plan.update(status='REVIEW',reason=_append_reason(plan.get('reason'),roles['conflict']))
    else:
        # REVIEW values are still shown in a consistent spelling, but role
        # changes are never invented while identity itself needs review.
        artist_plan = _apply_style_to_field_plan(
            artist_plan,
            canonicalizer=canonical_artist_credit,
            identity_safety=safety,
            references=artist_references,
            field_label="Artist",
        )

    album_plan = _apply_style_to_field_plan(
        album_plan,
        canonicalizer=canonical_album_name,
        identity_safety=safety,
        field_label="Album",
    )

    from core.album_artist import primary_credit
    albumartist_plan = _apply_style_to_field_plan(
        albumartist_plan,
        canonicalizer=primary_credit,
        identity_safety=safety,
        field_label="Album Artist",
    )

    return (
        artist_plan,
        title_plan,
        album_plan,
        albumartist_plan,
    )


def protect_historical_artist_name(
    *,
    data,
    artist_plan,
    release_context_selection=None,
):
    """Do not retroactively rename historical artist credits.

    A stable artist entity may change its stage name or preferred casing over
    time.  Unless a concrete selected release/track gives us an exact
    release-specific credit, an existing Artist that agrees with the filename
    is treated as plausible historical credit rather than something to
    silently modernize.

    Examples this protects:
      * Mastadon -> MARAUDA
      * Spag Heddy -> SPAG
      * Uber -> UBUR
      * Barely Alive -> BARELY ALIVE

    Search can still map these aliases to one identity; this guard only
    controls what is written back into the file.
    """
    artist_plan = dict(artist_plan or {})
    if artist_plan.get("status") != "SAFE":
        return artist_plan
    if data.get("_artist_name_preference") == "current":
        items = (_musicbrainz_for_final_identity(data).get("artist-credit") or [])
        if any(identity.get("id") and identity.get("canonical_name") != identity.get("credited_name")
               for identity in artist_identity_records_from_items(items)):
            return artist_plan

    current = clean_spaces(artist_plan.get("current") or data.get("artist") or "")
    proposed = clean_spaces(artist_plan.get("proposed") or "")
    filename_artist = clean_spaces(_filename_artist_text(data.get("path")))

    if not (current and proposed and filename_artist):
        return artist_plan

    selection = release_context_selection or {}
    selected_context = (
        selection.get("selected")
        if selection.get("status") == "SELECTED"
        else None
    ) or {}

    # A concrete release-specific credit is authoritative for historical
    # spelling/casing and is therefore allowed to change the tag.
    if clean_spaces(selected_context.get("track_artist_credit") or ""):
        return artist_plan

    current_key = _artist_entity_key(current)
    filename_key = _artist_entity_key(filename_artist)
    proposed_key = _artist_entity_key(proposed)

    from core.credit_style import credit_values
    primary_without_guest=credit_values(current,data.get('title') or '')['artist']
    if primary_without_guest!=current and _artist_entity_key(primary_without_guest)==proposed_key:
        return artist_plan

    # Only protect a value that is corroborated by the filename itself.
    if not current_key or current_key != filename_key:
        return artist_plan

    # Case/stylisation-only difference with no release-specific authority:
    # keep the historical spelling exactly as stored.
    if current_key == proposed_key and current != proposed:
        return make_field(
            current=current,
            proposed=current,
            status="KEEP",
            reason=(
                "Artist отличается только историческим регистром/стилизацией, "
                "а конкретный release credit не выбран; текущее написание сохранено."
            ),
        )

    # A materially different stage name with the same local evidence is a
    # potential rebrand/alias, not an automatic typo fix.
    if current_key != proposed_key:
        artist_plan["status"] = "REVIEW"
        artist_plan["reason"] = _append_reason(
            artist_plan.get("reason"),
            "Текущий Artist совпадает с именем файла, но внешний источник "
            "предлагает другое имя артиста. Это может быть исторический "
            "псевдоним/ребрендинг; без конкретного release credit автоматическая "
            "перезапись запрещена.",
        )

    return artist_plan


def apply_local_cleanup_fallback(
    field_name,
    current_value,
    field_plan,
):
    """
    Техническая чистка имеет право работать
    даже при REJECT идентификации.

    Но она не должна перебивать настоящие
    REVIEW / ASK / SAFE решения по метаданным.
    """

    cleanup = (
        analyze_local_cleanup(
            current_value
        )
    )

    if not cleanup["changed"]:
        return field_plan

    current_status = (
        field_plan.get(
            "status"
        )
        or "KEEP"
    )

    # -------------------------------------------------
    # Если уже есть содержательное решение,
    # не вмешиваемся.
    #
    # Например:
    #
    # Artist REVIEW:
    # Zomboy -> Zomboy feat. Kato
    #
    # Это важнее простого cleanup.
    # -------------------------------------------------

    if current_status not in {
        "KEEP",
        "BLOCKED",
    }:
        return field_plan

    reasons = ", ".join(
        cleanup["reasons"]
    )

    return make_field(
        current=current_value,
        proposed=cleanup["value"],
        status="SAFE",
        reason=(
            "Безопасная локальная чистка "
            f"поля {field_name}: "
            f"{reasons}. "
            "Идентификация записи "
            "для этого не требуется."
        ),
    )


def plan_artist(
    current_artist,
    official_artist_credit,
    safety,
):
    if safety == "REJECT":
        return make_field(
            current=current_artist,
            status="BLOCKED",
            reason=(
                "Запись недостаточно надёжно "
                "идентифицирована."
            ),
        )

    if not official_artist_credit:
        return make_field(
            current=current_artist,
            status="KEEP",
            reason=(
                "Нет подтверждённого "
                "официального artist credit."
            ),
        )

    if display_values_equal(
        current_artist,
        official_artist_credit,
    ):
        return make_field(
            current=current_artist,
            proposed=official_artist_credit,
            status="KEEP",
            reason=(
                "Artist уже совпадает "
                "с официальным credit."
            ),
        )

    current_names = split_artist_names(
        current_artist
    )

    official_names = split_artist_names(
        official_artist_credit
    )

    # Те же артисты, отличается только
    # оформление &, feat., регистр и т.п.
    if (
        current_names
        and current_names
        == official_names
    ):
        return make_field(
            current=current_artist,
            proposed=official_artist_credit,
            status="SAFE",
            reason=(
                "Состав исполнителей совпадает; "
                "отличается оформление кредита."
            ),
        )

    # Artist отсутствует вообще.
    if (
        not current_names
        and safety == "SAFE"
    ):
        return make_field(
            current=current_artist,
            proposed=official_artist_credit,
            status="SAFE",
            reason=(
                "Artist отсутствует, "
                "официальный credit найден."
            ),
        )

    # Если сама Recording уже подтверждена на SAFE-уровне, официальный
    # MusicBrainz artist credit имеет право исправить неполный локальный
    # Artist (например Krimer -> Krimer feat. Mikey Ceaser или
    # 12th Planet -> 12th Planet, BARELY ALIVE & PhaseOne).
    # Раньше planner безусловно понижал такие изменения до REVIEW, даже
    # после ACRCloud -> ISRC -> MusicBrainz, из-за чего recognition и writer
    # противоречили друг другу.
    if safety == "SAFE":
        return make_field(
            current=current_artist,
            proposed=official_artist_credit,
            status="SAFE",
            reason=(
                "Recording подтверждена на SAFE-уровне; Artist приводится "
                "к официальному MusicBrainz artist credit."
            ),
        )

    # Добавление / удаление исполнителя
    # при REVIEW-identity требует проверки.
    return make_field(
        current=current_artist,
        proposed=official_artist_credit,
        status="REVIEW",
        reason=(
            "Официальный состав исполнителей "
            "отличается от текущего."
        ),
    )


def _filename_artist_names(file_path):
    if not file_path:
        return []

    stem = Path(str(file_path)).stem
    if " - " not in stem:
        return []

    artist_part = stem.split(" - ", 1)[0].strip()
    if not artist_part:
        return []

    raw = re.sub(
        r"\b(?:feat\.?|ft\.?|featuring|with|vs\.?)\b",
        ",",
        artist_part,
        flags=re.IGNORECASE,
    )
    raw = re.sub(r"\s+[x×]\s+", ",", raw, flags=re.IGNORECASE)
    raw = raw.replace("&", ",").replace(";", ",")
    return [part.strip() for part in raw.split(",") if part.strip()]


def protect_filename_artist_credit(file_path, artist_plan):
    """
    Последний барьер против потери соисполнителя в SAFE Artist.

    Filename не является абсолютной истиной, поэтому мы не подменяем им
    внешний metadata. Но если filename явно перечисляет несколько артистов,
    автоматическая запись не должна молча удалить одного из них. В таком
    случае SAFE понижается до REVIEW.
    """
    artist_plan = artist_plan or {}
    if artist_plan.get("status") != "SAFE":
        return artist_plan

    proposed = artist_plan.get("proposed") or ""
    if not proposed:
        return artist_plan

    filename_names = _filename_artist_names(file_path)
    if len(filename_names) < 2:
        return artist_plan

    proposed_names = list(split_artist_names(proposed))
    if not proposed_names:
        return artist_plan

    missing = []
    for filename_name in filename_names:
        normalized = normalize_value(filename_name)
        if not normalized:
            continue

        matched = False
        for proposed_name in proposed_names:
            if normalized == proposed_name:
                matched = True
                break
            # Небольшой alias-barrier: JXL <-> Junkie XL и похожие
            # сокращения не должны создавать ложный REVIEW.
            if strict_similarity(normalized, proposed_name) >= 0.52:
                matched = True
                break

        if not matched:
            missing.append(filename_name)

    if not missing:
        return artist_plan

    return {
        **artist_plan,
        "status": "REVIEW",
        "reason": (
            "Предлагаемый Artist не содержит исполнителей, явно указанных "
            "в имени файла: " + ", ".join(missing) + ". Автоматически "
            "удалять такой credit нельзя."
        ),
    }


def plan_title(
    current_title,
    official_title,
    recording_title,
    safety,
    release_context_selection=None,
):
    cleaned_current_title = (
        clean_duplicate_brackets(
            current_title
        )
    )
    if (
        cleaned_current_title
        != current_title
        and safety == "REJECT"
    ):
        return make_field(
            current=current_title,
            proposed=cleaned_current_title,
            status="SAFE",
            reason=(
                "Исправлено только оформление: "
                "убраны лишние двойные скобки. "
                "Идентификация записи для этого "
                "не требуется."
            ),
        )
    if safety == "REJECT":
        return make_field(
            current=current_title,
            status="BLOCKED",
            reason=(
                "Запись недостаточно надёжно "
                "идентифицирована."
            ),
        )

    if (
        cleaned_current_title
        != current_title
    ):
        current_title = (
            cleaned_current_title
        )

    official_status = (
        official_title.get("status")
        if official_title
        else None
    )

    resolved_official = (
        official_status
        in {
            "RESOLVED",
            "RESOLVED_BY_RECORDING",
        }
    )

    selection = (
        release_context_selection
        or {}
    )

    selection_status = (
        selection.get("status")
    )

    selected_context = (
        selection.get("selected")
        or {}
    )

    # Title, зависящий от конкретного издания, можно брать только
    # после реального выбора ReleaseContext. Иначе варианты из
    # соседних изданий одной Recording могут просочиться в SAFE
    # (например обычный трек -> Instrumental).
    selected_track_title = (
        selected_context.get("track_title")
        if selection_status == "SELECTED"
        else None
    )

    if selected_track_title:
        resolved_official = True
        proposed = selected_track_title
        source = "selected_release_context"

    elif resolved_official:
        proposed = (
            official_title.get("title")
        )

        source = (
            official_title.get("source")
        )

        # Если конкретный ReleaseContext не выбран, а release-derived
        # Track.title спорит с подтверждённым Recording.title, не даём
        # релизу переопределить запись. Recording.title здесь более
        # надёжный источник именно identity трека.
        if (
            selection_status != "SELECTED"
            and recording_title
            and proposed
            and not values_equal(
                proposed,
                recording_title,
            )
        ):
            proposed = recording_title
            source = "recording_title_guard"

    if resolved_official:

        current_versions = set(
            extract_version_markers(
                current_title or ""
            )
        )

        recording_versions = set(
            extract_version_markers(
                recording_title or ""
            )
        )

        proposed_versions = set(
            extract_version_markers(
                proposed or ""
            )
        )

        required_versions = (
            current_versions
            | recording_versions
        )

        if (
            required_versions
            and not required_versions.issubset(
                proposed_versions
            )
        ):
            safer_title = (
                recording_title
                or proposed
            )

            if values_equal(
                current_title,
                safer_title,
            ):
                return make_field(
                    current=current_title,
                    proposed=safer_title,
                    status="KEEP",
                    reason=(
                        "Название на релизе теряет "
                        "информацию о версии, "
                        "поэтому сохраняется текущее "
                        "корректное название."
                    ),
                )

            return make_field(
                current=current_title,
                proposed=safer_title,
                status="REVIEW",
                reason=(
                    "Название на релизе теряет "
                    "информацию о версии трека "
                    "(remix/VIP/edit и т.п.). "
                    "Автоматически менять нельзя."
                ),
            )

        if values_equal(
            current_title,
            proposed,
        ):
            return make_field(
                current=current_title,
                proposed=proposed,
                status="KEEP",
                reason=(
                    "Title уже совпадает "
                    "с официальным."
                ),
            )

        if safety == "SAFE":
            if (
                source
                == "selected_release_group"
            ):
                reason = (
                    "Название взято с "
                    "выбранного официального релиза."
                )
            else:
                reason = (
                    "Название подтверждено "
                    "официальными релизами."
                )

            return make_field(
                current=current_title,
                proposed=proposed,
                status="SAFE",
                reason=reason,
            )

        return make_field(
            current=current_title,
            proposed=proposed,
            status="REVIEW",
            reason=(
                "Официальное название найдено, "
                "но идентификация требует проверки."
            ),
        )

    # Официального Track.title нет.
    # Recording.title можно показать,
    # но автоматически писать его не надо.

    if recording_title:
        if values_equal(
            current_title,
            recording_title,
        ):
            return make_field(
                current=current_title,
                proposed=recording_title,
                status="KEEP",
                reason=(
                    "Title совпадает "
                    "с Recording.title."
                ),
            )

        similarity = strict_similarity(
            current_title,
            recording_title,
        )

        if (
            similarity >= 0.95
            and safety == "SAFE"
        ):
            return make_field(
                current=current_title,
                proposed=recording_title,
                status="REVIEW",
                reason=(
                    "Названия очень похожи, "
                    "но официальный Track.title "
                    "не найден."
                ),
            )

        return make_field(
            current=current_title,
            proposed=recording_title,
            status="REVIEW",
            reason=(
                "Есть только Recording.title; "
                "официальное название на релизах "
                "не подтверждено."
            ),
        )

    return make_field(
        current=current_title,
        status="KEEP",
        reason=(
            "Надёжное предлагаемое "
            "название отсутствует."
        ),
    )


def plan_album_from_release_context(
    current_album,
    release_context_selection,
    safety,
):
    """
    План Album на основе ReleaseContext v2.

    Release Group здесь больше не является
    источником решения.
    """

    if safety == "REJECT":
        return make_field(
            current=current_album,
            status="BLOCKED",
            reason=(
                "Запись недостаточно надёжно "
                "идентифицирована."
            ),
        )

    selection = (
        release_context_selection
        or {}
    )

    selection_status = (
        selection.get("status")
        or "NONE"
    )

    # =========================================
    # КОНКРЕТНЫЙ RELEASECONTEXT ВЫБРАН
    # =========================================

    if selection_status == "SELECTED":
        context = (
            selection.get("selected")
            or {}
        )

        from core.release_titles import title as release_title
        proposed = release_title(context)

        if not proposed:
            return make_field(
                current=current_album,
                status="KEEP",
                reason=(
                    "У выбранного ReleaseContext "
                    "нет названия релиза."
                ),
            )

        if display_values_equal(
            canonical_album_name(
                current_album
            ),
            proposed,
        ):
            return make_field(
                current=current_album,
                proposed=proposed,
                status="KEEP",
                reason=(
                    "Album уже совпадает "
                    "с выбранным ReleaseContext."
                ),
            )

        if safety == "SAFE":
            return make_field(
                current=current_album,
                proposed=proposed,
                status="SAFE",
                reason=(
                    "Album взят из выбранного "
                    "конкретного ReleaseContext."
                ),
            )

        return make_field(
            current=current_album,
            proposed=proposed,
            status="REVIEW",
            reason=(
                "ReleaseContext выбран, "
                "но идентификация записи "
                "требует проверки."
            ),
        )

    # =========================================
    # ЕСТЬ НЕСКОЛЬКО ВАРИАНТОВ
    # =========================================

    if selection_status == "ASK":
        contexts = (
            selection.get("options")
            or []
        )

        options = []

        for context in contexts:
            from core.release_titles import title as release_title
            title = release_title(context)

            if (
                title
                and title not in options
            ):
                options.append(title)

        current_normalized = (
            normalize_value(
                canonical_album_name(
                    current_album
                )
            )
        )

        # Если текущий Album уже один из
        # подтверждённых вариантов,
        # менять его не нужно.
        for option in options:
            if (
                current_normalized
                and current_normalized
                == normalize_value(
                    option
                )
            ):
                return make_field(
                    current=current_album,
                    proposed=option,
                    status="KEEP",
                    reason=(
                        "Текущий Album является "
                        "подтверждённым вариантом "
                        "ReleaseContext."
                    ),
                    options=options,
                )

        return make_field(
            current=current_album,
            status="ASK",
            reason=(
                selection.get("reason")
                or (
                    "Есть несколько "
                    "ReleaseContext."
                )
            ),
            options=options,
        )

    # =========================================
    # КОНКРЕТНЫХ RELEASE НЕТ
    # =========================================

    return make_field(
        current=current_album,
        status="KEEP",
        reason=(
            "Конкретный ReleaseContext "
            "не выбран."
        ),
    )


def plan_album(
    current_album,
    releases,
    selected_release,
    safety,
):
    if safety == "REJECT":
        return make_field(
            current=current_album,
            status="BLOCKED",
            reason=(
                "Запись недостаточно надёжно "
                "идентифицирована."
            ),
        )

    releases = releases or []

    # -------------------------------------------------
    # Уже выбран конкретный релиз
    # -------------------------------------------------

    if selected_release:
        from core.release_titles import title as release_title
        proposed = release_title(selected_release)

        if values_equal(
            canonical_album_name(
                current_album
            ),
            proposed,
        ):
            return make_field(
                current=current_album,
                proposed=proposed,
                status="KEEP",
                reason=(
                    "Album уже совпадает "
                    "с выбранным релизом."
                ),
            )

        if safety == "SAFE":
            return make_field(
                current=current_album,
                proposed=proposed,
                status="SAFE",
                reason=(
                    "Для записи выбран "
                    "однозначный релиз."
                ),
            )

        return make_field(
            current=current_album,
            proposed=proposed,
            status="REVIEW",
            reason=(
                "Релиз найден, но запись "
                "требует проверки."
            ),
        )

    # -------------------------------------------------
    # Релиз не выбран
    # -------------------------------------------------

    if not releases:
        return make_field(
            current=current_album,
            status="KEEP",
            reason=(
                "Подтверждённых релизов нет."
            ),
        )

    options = []

    for release in releases:
        from core.release_titles import title as release_title
        title = release_title(release)

        if (
            title
            and title not in options
        ):
            options.append(title)

    # Если текущий Album уже является одним
    # из допустимых вариантов — не трогаем.
    current_normalized = normalize_value(
        canonical_album_name(
            current_album
        )
    )

    for option in options:
        if (
            current_normalized
            and current_normalized
            == normalize_value(option)
        ):
            return make_field(
                current=current_album,
                proposed=option,
                status="KEEP",
                reason=(
                    "Текущий Album является "
                    "одним из допустимых релизов."
                ),
                options=options,
            )

    return make_field(
        current=current_album,
        status="ASK",
        reason=(
            "У записи есть несколько "
            "допустимых релизов."
        ),
        options=options,
    )


def _format_release_position(
    number,
    total,
):
    """
    Формирует стандартное значение:

    1 + 5 -> 1/5
    1 + None -> 1

    Поддерживает также A1 / B2.
    """

    if number is None:
        return ""

    number = str(
        number
    ).strip()

    if not number:
        return ""

    if total is None:
        return number

    total = str(
        total
    ).strip()

    if not total:
        return number

    return (
        f"{number}/{total}"
    )


def _make_release_preview_field(
    current,
    proposed,
):
    current = (
        str(current)
        if current is not None
        else ""
    )

    proposed = (
        str(proposed)
        if proposed is not None
        else ""
    )

    return {
        "current": current,
        "proposed": proposed,

        "changed": (
            not display_values_equal(
                current,
                proposed,
            )
        ),
    }


def build_release_metadata_preview(
    data,
    selected_release_context,
    release_context_selection,
):
    """
    READ-ONLY preview полей,
    зависящих от выбранного ReleaseContext.

    В change_plan эти данные пока
    НЕ попадают.

    Writer их пока не видит.
    """

    selection_status = (
        (
            release_context_selection
            or {}
        ).get(
            "status"
        )
        or "NONE"
    )

    reason = (
        (
            release_context_selection
            or {}
        ).get(
            "reason"
        )
        or ""
    )

    result = {
        "status": selection_status,

        "reason": reason,

        "release_id": None,

        "fields": {},
    }

    if not selected_release_context:
        return result

    context = (
        selected_release_context
    )

    result["release_id"] = (
        context.get(
            "release_id"
        )
    )

    # -----------------------------------------
    # ALBUM
    # -----------------------------------------

    from core.release_titles import title as release_title
    proposed_album = release_title(context)

    # -----------------------------------------
    # ALBUM ARTIST
    # -----------------------------------------

    from core.album_artist import release_credit
    proposed_albumartist = release_credit(context)

    # -----------------------------------------
    # DATE
    #
    # Именно дата выбранного Release,
    # а НЕ first-release-date.
    # -----------------------------------------

    proposed_date = (
        context.get("date")
        or ""
    )

    # -----------------------------------------
    # TRACK
    # -----------------------------------------

    proposed_tracknumber = (
        _format_release_position(
            context.get(
                "track_number"
            ),
            context.get(
                "track_total"
            ),
        )
    )

    # -----------------------------------------
    # DISC
    #
    # Если релиз однодисковый и Disc Number
    # сейчас отсутствует — пока НЕ начинаем
    # массово добавлять 1/1.
    #
    # Но:
    # - существующий Disc Number проверяем;
    # - для многодискового релиза добавляем.
    # -----------------------------------------

    current_discnumber = (
        data.get(
            "discnumber"
        )
        or ""
    )

    disc_number = (
        context.get(
            "disc_number"
        )
    )

    disc_total = (
        context.get(
            "disc_total"
        )
    )

    proposed_discnumber = ""

    if (current_discnumber or (str(disc_total or "").isdigit() and int(disc_total) > 1)):
        proposed_discnumber = (
            _format_release_position(
                disc_number,
                disc_total,
            )
        )

    result["fields"] = {
        "album": (
            _make_release_preview_field(
                data.get(
                    "album"
                )
                or "",
                proposed_album,
            )
        ),

        "albumartist": (
            _make_release_preview_field(
                data.get(
                    "albumartist"
                )
                or "",
                proposed_albumartist,
            )
        ),

        "date": (
            _make_release_preview_field(
                data.get(
                    "date"
                )
                or "",
                proposed_date,
            )
        ),

        "tracknumber": (
            _make_release_preview_field(
                data.get(
                    "tracknumber"
                )
                or "",
                proposed_tracknumber,
            )
        ),

        "discnumber": (
            _make_release_preview_field(
                current_discnumber,
                proposed_discnumber,
            )
        ),
    }

    return result


def plan_release_dependent_fields(
    data,
    release_context_selection,
    safety,
):
    """
    Строит change_plan для полей,
    зависящих от конкретного Release.

    Пока writer эти поля НЕ записывает.
    """

    selection = (
        release_context_selection
        or {}
    )

    selected_context = (
        selection.get(
            "selected"
        )
    )

    preview = (
        build_release_metadata_preview(
            data=data,

            selected_release_context=(
                selected_context
            ),

            release_context_selection=(
                selection
            ),
        )
    )

    preview_fields = (
        preview.get(
            "fields"
        )
        or {}
    )

    result = {}

    field_names = (
        "albumartist",
        "date",
        "tracknumber",
        "discnumber",
    )

    # -----------------------------------------
    # RELEASE НЕ ВЫБРАН
    #
    # Не пытаемся независимо выбирать
    # Date / Track / Disc / Album Artist.
    #
    # Сначала должен быть выбран Album /
    # ReleaseContext.
    # -----------------------------------------

    if (
        selection.get("status")
        != "SELECTED"
    ):
        for field_name in field_names:
            current = (
                data.get(
                    field_name
                )
                or ""
            )

            result[field_name] = (
                make_field(
                    current=current,
                    status="KEEP",
                    reason=(
                        "Конкретный ReleaseContext "
                        "не выбран. Поле зависит "
                        "от выбора релиза."
                    ),
                )
            )

        return result

    # -----------------------------------------
    # RELEASE ВЫБРАН
    # -----------------------------------------

    for field_name in field_names:
        preview_field = (
            preview_fields.get(
                field_name
            )
            or {}
        )

        current = (
            preview_field.get(
                "current"
            )
            or ""
        )

        proposed = (
            preview_field.get(
                "proposed"
            )
        )

        changed = bool(
            preview_field.get(
                "changed"
            )
        )

        if not changed:
            result[field_name] = (
                make_field(
                    current=current,
                    proposed=proposed,
                    status="KEEP",
                    reason=(
                        "Поле уже соответствует "
                        "выбранному ReleaseContext."
                    ),
                )
            )

            continue

        # Если proposal пустой, автоматически
        # удалять существующее значение нельзя.
        #
        # Особенно важно для Disc Number.
        if not proposed:
            result[field_name] = (
                make_field(
                    current=current,
                    status="KEEP",
                    reason=(
                        "Выбранный ReleaseContext "
                        "не требует нового значения. "
                        "Существующее поле "
                        "автоматически не удаляется."
                    ),
                )
            )

            continue

        if safety == "SAFE":
            status = "SAFE"

        else:
            status = "REVIEW"

        result[field_name] = (
            make_field(
                current=current,
                proposed=proposed,
                status=status,
                reason=(
                    "Значение получено из "
                    "выбранного конкретного "
                    "ReleaseContext."
                ),
            )
        )

    return result


def release_is_type(
    release,
    wanted_type,
):
    if not release:
        return False

    release_type = (
        release.get("type")
        or ""
    ).casefold()

    return (
        release_type
        == wanted_type.casefold()
    )


def plan_cover(
    releases,
    selected_release,
    preferences,
    safety,
):
    if safety == "REJECT":
        return make_field(
            status="BLOCKED",
            reason=(
                "Запись недостаточно надёжно "
                "идентифицирована."
            ),
        )

    releases = releases or []

    # Cover пока является только планом выбора источника и физически
    # не записывается writer'ом. Но статус всё равно должен корректно
    # отражать уверенность идентификации и не падать в ветках
    # prefer_single / prefer_album.
    auto_status = (
        "SAFE"
        if safety == "SAFE"
        else "REVIEW"
    )

    preference = preferences.get(
        "cover_preference",
        "ask",
    )

    if preference == "keep_current":
        return make_field(
            status="KEEP",
            reason=(
                "Настройка: сохранять "
                "текущую обложку."
            ),
        )

    if not releases:
        return make_field(
            status="KEEP",
            reason=(
                "Нет известного релиза, "
                "откуда можно брать обложку."
            ),
        )

    if preference == "ask":
        return make_field(
            status="ASK",
            reason=(
                "Источник обложки "
                "должен выбрать пользователь."
            ),
            options=[
                release.get("title")
                for release in releases
                if release.get("title")
            ],
        )

    # -------------------------------------------------
    # ПРЕДПОЧИТАТЬ СИНГЛ
    # -------------------------------------------------

    if preference == "prefer_single":
        for release in releases:
            if release_is_type(
                release,
                "Single",
            ):
                return make_field(
                    proposed=(
                        "Обложка сингла: "
                        + (
                            release.get("title")
                            or "—"
                        )
                    ),
                    status=auto_status,
                    reason=(
                        "Настройка пользователя: "
                        "предпочитать сингловые "
                        "обложки."
                    ),
                )

        # Сингла нет — если релиз выбран,
        # используем его как запасной вариант.
        if selected_release:
            return make_field(
                proposed=(
                    "Обложка релиза: "
                    + (
                        selected_release.get(
                            "title"
                        )
                        or "—"
                    )
                ),
                status=auto_status,
                reason=(
                    "Синглового релиза нет; "
                    "используется выбранный релиз."
                ),
            )

        return make_field(
            status="ASK",
            reason=(
                "Сингловая обложка не найдена."
            ),
        )

    # -------------------------------------------------
    # ПРЕДПОЧИТАТЬ АЛЬБОМ / EP
    # -------------------------------------------------

    if preference == "prefer_album":
        for wanted_type in (
            "Album",
            "EP",
        ):
            for release in releases:
                if release_is_type(
                    release,
                    wanted_type,
                ):
                    return make_field(
                        proposed=(
                            "Обложка релиза: "
                            + (
                                release.get(
                                    "title"
                                )
                                or "—"
                            )
                        ),
                        status=auto_status,
                        reason=(
                            "Настройка пользователя: "
                            "предпочитать "
                            "альбомные обложки."
                        ),
                    )

        return make_field(
            status="ASK",
            reason=(
                "Альбомный или EP-релиз "
                "не найден."
            ),
        )

    return make_field(
        status="ASK",
        reason=(
            "Неизвестная настройка "
            "источника обложки."
        ),
    )


def build_change_plan(
    data,
    best_match,
    artist_credit,
    official_title,
    releases,
    selected_release,
    safety,
    preferences,
    release_context_selection=None,
):
    data["_artist_name_preference"] = (preferences or {}).get("artist_name_preference", "historical")
    data.pop("_provisional_audio_artist_names", None)
    candidate = (
        best_match.get(
            "candidate",
            {},
        )
    )

    recording_title = (
        candidate.get("title")
        or ""
    )

    current_artist = (
        data.get("artist")
        or ""
    )

    current_title = (
        data.get("title")
        or ""
    )

    current_album = (
        data.get("album")
        or ""
    )

    # Consensus fallback may deliberately preserve a richer Artist credit
    # than the MusicBrainz Recording itself. Example:
    #   catalog/MB: WoodenToaster
    #   ACRCloud: WoodenToaster and Mic the Microphone
    #   consensus: WoodenToaster + Mic the Microphone
    # In that path the final writer must honor the consensus result instead
    # of falling back to the shorter MB artist-credit. Recognition has already
    # decided whether that richer credit is SAFE/REVIEW.
    effective_artist_credit = (
        artist_credit
        or ""
    )

    fallback_fusion = (
        data.get("fallback_fusion")
        or {}
    )

    if (
        fallback_fusion.get("status")
        in {"SAFE", "REVIEW"}
        and fallback_fusion.get("artist")
    ):
        effective_artist_credit = (
            fallback_fusion.get("artist")
            or effective_artist_credit
        )

    artist_plan = plan_artist(
        current_artist=(
            current_artist
        ),
        official_artist_credit=(
            effective_artist_credit
        ),
        safety=safety,
    )

    if not effective_artist_credit and safety != "REJECT":
        # An unavailable MusicBrainz credit is not permission to treat the
        # dirty local Artist as authoritative and strip its remixers with SAFE.
        # Show the audio candidate as a provisional choice instead; do not
        # silently replace a verified historical alias with its current name.
        candidate_artists = [str(a) for a in candidate.get("artists") or [] if isinstance(a, str) and a.strip()]
        provisional_artist = canonical_entity_list(candidate_artists)
        from core.artist_aliases import same_artist
        if (provisional_artist
                and not same_artist(current_artist, provisional_artist)
                and not display_values_equal(canonical_artist_credit(current_artist), provisional_artist)):
            artist_plan = make_field(
                current=current_artist, proposed=provisional_artist, status="REVIEW",
                reason="Полный artist credit MusicBrainz недоступен. Исполнитель аудиокандидата "
                       "предложен для проверки; локальный тег не подтверждает роли артистов.")
            artist_plan["evidence_source"] = "audio_candidate_unenriched"
            data["_provisional_audio_artist_names"] = candidate_artists

    artist_plan = protect_filename_artist_credit(
        data.get("path"),
        artist_plan,
    )

    title_plan = plan_title(
        current_title=(
            current_title
        ),
        official_title=(
            official_title
            or {}
        ),
        recording_title=(
            recording_title
        ),
        safety=safety,
        release_context_selection=(
            release_context_selection
        ),
    )

    if release_context_selection is not None:
        album_plan = (
            plan_album_from_release_context(
                current_album=(
                    current_album
                ),

                release_context_selection=(
                    release_context_selection
                ),

                safety=safety,
            )
        )

    else:
        # Старый путь оставлен только
        # для обратной совместимости.
        album_plan = plan_album(
            current_album=(
                current_album
            ),

            releases=releases,

            selected_release=(
                selected_release
            ),

            safety=safety,
        )

    release_fields = (
        plan_release_dependent_fields(
            data=data,

            release_context_selection=(
                release_context_selection
            ),

            safety=safety,
        )
    )

    albumartist_plan = (
        release_fields[
            "albumartist"
        ]
    )

    date_plan = (
        release_fields[
            "date"
        ]
    )

    tracknumber_plan = (
        release_fields[
            "tracknumber"
        ]
    )

    discnumber_plan = (
        release_fields[
            "discnumber"
        ]
    )

    cover_plan = plan_cover(
        releases=releases,
        selected_release=(
            selected_release
        ),
        preferences=preferences,
        safety=safety,
    )

    # =========================================
    # LOCAL CLEANUP
    #
    # Технический мусор исправляется отдельно
    # от идентификации записи.
    # =========================================

    artist_plan = (
        apply_local_cleanup_fallback(
            "Artist",
            current_artist,
            artist_plan,
        )
    )

    title_plan = (
        apply_local_cleanup_fallback(
            "Title",
            current_title,
            title_plan,
        )
    )

    album_plan = (
        apply_local_cleanup_fallback(
            "Album",
            current_album,
            album_plan,
        )
    )

    # -------------------------------------------------
    # ЗАВИСИМОСТЬ ARTIST <-> TITLE
    #
    # Старая быстрая проверка для нормальных
    # feat. / ft. / featuring.
    # -------------------------------------------------

    proposed_title = (
        title_plan.get(
            "proposed"
        )
        or ""
    )

    if (
        title_plan.get("status")
        == "SAFE"
        and has_feature_credit(
            current_title
        )
        and not has_feature_credit(
            proposed_title
        )
        and artist_plan.get("status")
        in {
            "REVIEW",
            "BLOCKED",
        }
    ):
        title_plan = {
            **title_plan,

            "status": "REVIEW",

            "dependency_group": (
                "artist_title_credit"
            ),

            "reason": (
                "Новое название убирает "
                "feat./ft. из Title, "
                "но перенос featured-артиста "
                "в Artist ещё не подтверждён. "
                "Поля должны изменяться вместе."
            ),
        }

    # =========================================
    # ФИНАЛЬНАЯ ЗАЩИТА ARTIST <-> TITLE
    #
    # Эта проверка запускается ВСЕГДА.
    # Она ловит даже испорченные:
    #
    # feat.
    # ft.
    # аeat
    # fеat
    # и другие странные разделители.
    # =========================================

    title_plan = (
        protect_title_artist_credits(
            current_title=(
                current_title
            ),
            title_plan=(
                title_plan
            ),
            artist_plan=(
                artist_plan
            ),
            artist_credit=(
                effective_artist_credit
            ),
        )
    )

    # =========================================
    # FINAL NAMING STYLE
    #
    # Recognition above intentionally accepts many source-specific forms.
    # Only now, immediately before the plan is returned to the writer, values
    # are formatted to one library-wide style.
    # =========================================
    if (preferences or {}).get("normalize_names", True):
        (
            artist_plan,
            title_plan,
            album_plan,
            albumartist_plan,
        ) = apply_final_naming_style(
            data=data,
            safety=safety,
            artist_plan=artist_plan,
            title_plan=title_plan,
            album_plan=album_plan,
            albumartist_plan=albumartist_plan,
            release_context_selection=release_context_selection,
        )

        artist_plan = protect_historical_artist_name(
            data=data,
            artist_plan=artist_plan,
            release_context_selection=release_context_selection,
        )

    if data.get("_artist_name_preference") == "ask":
        selection = release_context_selection or {}
        items = ((selection.get("selected") or {}).get("track_artist_credit_items")
                 or _musicbrainz_for_final_identity(data).get("artist-credit") or [])
        historic = canonical_artist_credit_from_items(items)
        current_items = [({**item, "name": (item.get("artist") or {}).get("name") or item.get("name")}
                          if isinstance(item, dict) else item) for item in items]
        modern = canonical_artist_credit_from_items(current_items)
        if historic and modern and historic != modern:
            artist_plan = {**artist_plan, "status": "REVIEW", "options": list(dict.fromkeys([historic, modern, current_artist])),
                           "reason": _append_reason(artist_plan.get("reason"), "Выберите исторический credit или текущее имя того же MBID.")}

    selected=(release_context_selection or {}).get('selected') or {}
    from core.duration_policy import duration_requires_review
    if duration_requires_review(data.get('duration'),selected.get('duration')):
        safety='REVIEW'
        for plan in (artist_plan,title_plan,album_plan,albumartist_plan,date_plan,tracknumber_plan,discnumber_plan):
            if plan.get('status') in ('SAFE','REVIEW'):
                plan.update(status='REVIEW',reason=_append_reason(plan.get('reason'),'Длительность файла не соответствует выбранной записи. Проверьте аудио и версию; автоматическая запись заблокирована.'))

    return {
        "identity": safety,

        "artist": artist_plan,

        "title": title_plan,

        "album": album_plan,

        "albumartist": (
            albumartist_plan
        ),

        "date": date_plan,

        "tracknumber": (
            tracknumber_plan
        ),

        "discnumber": (
            discnumber_plan
        ),

        "cover": cover_plan,
    }


def build_local_cleanup_plan(
    data,
):
    """
    План только для безопасной локальной чистки.

    Используется даже тогда, когда
    AcoustID вообще не распознал запись.
    """

    current_artist = (
        data.get("artist")
        or ""
    )

    current_title = (
        data.get("title")
        or ""
    )

    current_album = (
        data.get("album")
        or ""
    )

    artist_plan = make_field(
        current=current_artist,
        status="BLOCKED",
        reason=(
            "Запись не идентифицирована. "
            "Содержательные изменения Artist "
            "запрещены."
        ),
    )

    title_plan = make_field(
        current=current_title,
        status="BLOCKED",
        reason=(
            "Запись не идентифицирована. "
            "Содержательные изменения Title "
            "запрещены."
        ),
    )

    album_plan = make_field(
        current=current_album,
        status="BLOCKED",
        reason=(
            "Запись не идентифицирована. "
            "Содержательные изменения Album "
            "запрещены."
        ),
    )

    # -----------------------------------------
    # Но безопасный технический cleanup
    # всё равно разрешён.
    # -----------------------------------------

    artist_plan = (
        apply_local_cleanup_fallback(
            "Artist",
            current_artist,
            artist_plan,
        )
    )

    title_plan = (
        apply_local_cleanup_fallback(
            "Title",
            current_title,
            title_plan,
        )
    )

    album_plan = (
        apply_local_cleanup_fallback(
            "Album",
            current_album,
            album_plan,
        )
    )

    cover_plan = make_field(
        status="BLOCKED",
        reason=(
            "Запись не идентифицирована. "
            "Обложка автоматически "
            "не выбирается."
        ),
    )

    return {
        "identity": "REJECT",
        "artist": artist_plan,
        "title": title_plan,
        "album": album_plan,
        "cover": cover_plan,
    }
