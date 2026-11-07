from core.naming import (
    canonical_artist_credit_from_items,
    artist_identity_records_from_items,
)
from datetime import date


def format_artist_credit(
    artist_credit,
):
    return canonical_artist_credit_from_items(
        artist_credit
    )


def normalize_secondary_types(
    release_group,
):
    values = (
        release_group.get(
            "secondary-types"
        )
        or []
    )

    return [
        str(value)
        for value in values
        if value
    ]


def parse_date(
    value,
):
    """
    Для сравнения дат.

    MusicBrainz может вернуть:
    2018
    2018-04
    2018-04-27
    """

    if not value:
        return None

    text = str(value).strip()

    parts = text.split("-")

    try:
        year = int(parts[0])

        month = (
            int(parts[1])
            if len(parts) > 1
            else 1
        )

        day = (
            int(parts[2])
            if len(parts) > 2
            else 1
        )

        return date(
            year,
            month,
            day,
        )

    except (
        ValueError,
        TypeError,
    ):
        return None


def find_recording_track(
    release,
    recording_id,
):
    """
    Находит конкретный Track,
    который указывает на нашу Recording.

    Recording и Track в MusicBrainz —
    разные сущности.
    """

    media = (
        release.get("media")
        or []
    )

    disc_total = len(media)

    for medium_index, medium in enumerate(
        media,
        start=1,
    ):
        tracks = (
            medium.get("tracks")
        )

        if tracks is None:
            tracks = (
                medium.get(
                    "track",
                    [],
                )
            )

        tracks = (
            tracks
            or []
        )

        for track_index, track in enumerate(
            tracks,
            start=1,
        ):
            recording = (
                track.get("recording")
                or {}
            )

            if (
                recording.get("id")
                != recording_id
            ):
                continue

            track_position = (
                track.get("position")
                or track_index
            )

            track_number = (
                track.get("number")
                or str(track_position)
            )

            track_total = (
                medium.get("track-count")
                or len(tracks)
            )

            disc_number = (
                medium.get("position")
                or medium_index
            )

            return {
                "track_id": (
                    track.get("id")
                ),

                "track_title": (
                    track.get("title")
                    or recording.get("title")
                    or ""
                ),

                **({'duration':float(track.get('length') or recording.get('length'))/1000}
                   if track.get('length') or recording.get('length') else {}),

                "track_artist_credit": (
                    format_artist_credit(
                        track.get(
                            "artist-credit"
                        )
                        or recording.get(
                            "artist-credit"
                        )
                    )
                ),

                # Keep the structured, release-specific credit as well.
                # ``name`` is the credited-as spelling for this exact
                # track/release, while ``artist.name`` may be the artist's
                # current/global name.  This distinction is essential for
                # historical aliases/rebrands and intentional casing such as
                # Mastadon -> MARAUDA or Spag Heddy -> SPAG.
                "track_artist_credit_items": (
                    track.get(
                        "artist-credit"
                    )
                    or recording.get(
                        "artist-credit"
                    )
                    or []
                ),

                # Search/index foundation: one stable MBID may expose both
                # the current entity name and the historical credited name.
                # We do NOT write all aliases into Artist; they are kept as
                # identity/search metadata for the future library UI.
                "track_artist_identities": (
                    artist_identity_records_from_items(
                        track.get(
                            "artist-credit"
                        )
                        or recording.get(
                            "artist-credit"
                        )
                        or []
                    )
                ),

                "track_number": (
                    str(track_number)
                ),

                "track_position": (
                    track_position
                ),

                "track_total": (
                    track_total
                ),

                "disc_number": (
                    disc_number
                ),

                "disc_total": (
                    disc_total
                ),

                "medium_format": (
                    medium.get("format")
                    or ""
                ),
            }

    return None


def classify_release(
    primary_type,
    secondary_types,
):
    primary = (
        primary_type
        or "Other"
    )

    secondary = {
        str(value).casefold()
        for value
        in secondary_types
    }

    flags = []

    if "compilation" in secondary:
        flags.append(
            "Compilation"
        )

    if "remix" in secondary:
        flags.append(
            "Remix"
        )

    if "dj-mix" in secondary:
        flags.append(
            "DJ-mix"
        )

    if "soundtrack" in secondary:
        flags.append(
            "Soundtrack"
        )

    if "live" in secondary:
        flags.append(
            "Live"
        )

    if "demo" in secondary:
        flags.append(
            "Demo"
        )

    if "mixtape/street" in secondary:
        flags.append(
            "Mixtape"
        )

    if not flags:
        return primary

    return (
        primary
        + " / "
        + " / ".join(flags)
    )


def build_release_context(
    release,
    recording_id,
):
    release_group = (
        release.get("release-group")
        or {}
    )

    secondary_types = (
        normalize_secondary_types(
            release_group
        )
    )

    track = find_recording_track(
        release,
        recording_id,
    )

    # Если release не содержит
    # конкретный track нашей recording,
    # использовать его для Album/Track нельзя.
    if track is None:
        return None

    release_date = (
        release.get("date")
        or ""
    )

    first_release_date = (
        release_group.get(
            "first-release-date"
        )
        or ""
    )

    parsed_release_date = (
        parse_date(
            release_date
        )
    )

    parsed_first_date = (
        parse_date(
            first_release_date
        )
    )

    is_later_issue = False

    if (
        parsed_release_date
        and parsed_first_date
        and parsed_release_date
        > parsed_first_date
    ):
        is_later_issue = True

    primary_type = (
        release_group.get(
            "primary-type"
        )
        or release_group.get(
            "type"
        )
        or ""
    )

    return {
        # ---------------------------------
        # IDS
        # ---------------------------------

        "release_id": (
            release.get("id")
        ),

        "release_group_id": (
            release_group.get("id")
        ),

        # ---------------------------------
        # RELEASE
        # ---------------------------------

        "title": (
            release.get("title")
            or ""
        ),

        "status": (
            release.get("status")
            or ""
        ),

        "country": (
            release.get("country")
            or ""
        ),

        "date": release_date,

        "packaging": (
            release.get("packaging")
            or ""
        ),

        "barcode": (
            release.get("barcode")
            or ""
        ),

        # ---------------------------------
        # RELEASE GROUP
        # ---------------------------------

        "primary_type": (
            primary_type
        ),

        "secondary_types": (
            secondary_types
        ),

        "release_type": (
            classify_release(
                primary_type,
                secondary_types,
            )
        ),

        "first_release_date": (
            first_release_date
        ),

        "is_later_issue": (
            is_later_issue
        ),

        # ---------------------------------
        # ALBUM ARTIST
        # ---------------------------------

        "album_artist_credit": (
            format_artist_credit(
                release.get(
                    "artist-credit"
                )
            )
        ),
        "album_artist_credit_items": release.get("artist-credit") or [],

        # ---------------------------------
        # TRACK / DISC
        # ---------------------------------

        **track,

        # ---------------------------------
        # СЛУЖЕБНОЕ
        # ---------------------------------

        "source": "musicbrainz",
    }


def build_release_contexts(
    releases,
    recording_id,
):
    contexts = []

    for release in (
        releases or []
    ):
        context = (
            build_release_context(
                release,
                recording_id,
            )
        )

        if context is None:
            continue

        contexts.append(
            context
        )

    return contexts


def release_context_key(
    context,
):
    """
    Поля, которые реально меняют
    музыкальные теги файла.

    Два разных MusicBrainz Release
    считаются эквивалентными для TagCleaner,
    если эти значения совпадают.
    """

    return (
        context.get("title") or "",
        context.get(
            "album_artist_credit"
        ) or "",
        context.get("date") or "",
        context.get("track_title") or "",
        context.get("track_artist_credit") or "",
        str(
            context.get(
                "track_number"
            )
            or ""
        ),
        str(
            context.get(
                "track_total"
            )
            or ""
        ),
        str(
            context.get(
                "disc_number"
            )
            or ""
        ),
        str(
            context.get(
                "disc_total"
            )
            or ""
        ),
    )


def deduplicate_contexts(
    contexts,
):
    """
    Убирает технические дубликаты,
    но НЕ склеивает переиздания,
    если у них другая дата/позиция.
    """

    result = []
    seen = set()

    for context in contexts:
        key = release_context_key(
            context
        )

        if key in seen:
            continue

        seen.add(key)

        result.append(
            context
        )

    return result


def can_auto_select(
    contexts,
):
    """
    Автовыбор разрешён только если после
    удаления эквивалентных вариантов
    остался один реальный вариант тегов.
    """

    unique = (
        deduplicate_contexts(
            contexts
        )
    )

    return len(unique) == 1


def context_display_lines(
    context,
):
    """
    Человеческое представление
    для будущего интерактивного выбора.
    """

    lines = []

    title = (
        context.get("title")
        or "Без названия"
    )

    release_type = (
        context.get("release_type")
        or "тип не указан"
    )

    lines.append(
        f"{title} [{release_type}]"
    )

    if context.get("date"):
        lines.append(
            "Дата: "
            + context["date"]
        )

    if context.get(
        "first_release_date"
    ):
        lines.append(
            "Первый выпуск группы: "
            + context[
                "first_release_date"
            ]
        )

    if context.get(
        "album_artist_credit"
    ):
        lines.append(
            "Album Artist: "
            + context[
                "album_artist_credit"
            ]
        )

    track_number = (
        context.get("track_number")
    )

    track_total = (
        context.get("track_total")
    )

    if track_number:
        if track_total:
            lines.append(
                "Track: "
                f"{track_number}/"
                f"{track_total}"
            )
        else:
            lines.append(
                "Track: "
                f"{track_number}"
            )

    disc_number = (
        context.get("disc_number")
    )

    disc_total = (
        context.get("disc_total")
    )

    if disc_number:
        if disc_total:
            lines.append(
                "Disc: "
                f"{disc_number}/"
                f"{disc_total}"
            )
        else:
            lines.append(
                "Disc: "
                f"{disc_number}"
            )

    if context.get(
        "country"
    ):
        lines.append(
            "Страна: "
            + context["country"]
        )

    if context.get(
        "status"
    ):
        lines.append(
            "Статус: "
            + context["status"]
        )

    if context.get(
        "is_later_issue"
    ):
        lines.append(
            "Позднее издание / "
            "переиздание"
        )

    return lines
