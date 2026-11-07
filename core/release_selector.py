from core.audd_matcher import normalize_text

from core.naming import (
    canonical_album_name,
    deduplicate_release_groups,
)

from core.release_context import (
    release_context_key,
)


# =================================================
# LEGACY RELEASE-GROUP SELECTOR
#
# Пока оставляем старый интерфейс,
# чтобы существующий app.py продолжал работать.
# =================================================


def prepare_releases(
    releases,
):
    return deduplicate_release_groups(
        releases or []
    )


def select_release(
    releases,
    preference,
):
    """
    Старый selector по Release Group.

    Пока используется существующим app.py.
    Будет удалён после подключения
    ReleaseContext v2.
    """

    releases = prepare_releases(
        releases
    )

    if not releases:
        return None

    if len(releases) == 1:
        release = releases[0]

        secondary_types = {
            str(value).casefold()
            for value in release.get(
                "secondary_types",
                [],
            )
        }

        risky_secondary_types = {
            "compilation",
            "dj-mix",
        }

        if (
            secondary_types
            & risky_secondary_types
        ):
            return None

        return release

    if preference == "ask":
        return None

    if preference == "keep_current":
        return None

    if preference == "prefer_single":
        for release in releases:
            release_type = (
                release.get("type")
                or ""
            ).casefold()

            if release_type == "single":
                return release

        return None

    if preference == "prefer_album":
        for wanted_type in (
            "ep",
            "album",
        ):
            for release in releases:
                release_type = (
                    release.get("type")
                    or ""
                ).casefold()

                if (
                    release_type
                    == wanted_type
                ):
                    return release

        return None

    return None


# =================================================
# RELEASE CONTEXT V2
# =================================================


RISKY_SECONDARY_TYPES = {
    "compilation",
    "dj-mix",
}


def normalize_album_value(
    value,
):
    """
    Нормализация только для сравнения Album.
    """

    value = canonical_album_name(
        value or ""
    )

    # Для сравнения Album пунктуация и типографика не должны создавать
    # ложную неоднозначность: Rott N' Roll Pt. 1 и
    # Rott n’ Roll, Pt. 1 — одно и то же название релиза.
    return normalize_text(value)


def context_primary_type(
    context,
):
    return (
        context.get(
            "primary_type"
        )
        or ""
    ).strip().casefold()


def context_is_risky(
    context,
):
    """
    Compilation и DJ-mix автоматически
    не выбираем.

    Сам факт нахождения такого релиза
    абсолютно нормален — просто требуется
    осознанный выбор.
    """

    secondary_types = {
        str(value).casefold()
        for value
        in (
            context.get(
                "secondary_types"
            )
            or []
        )
    }

    return bool(
        secondary_types
        & RISKY_SECONDARY_TYPES
    )


def group_release_contexts(
    contexts,
):
    """
    Группирует конкретные MusicBrainz Release
    по фактическому результату тегирования.

    Например два разных release_id могут
    приводить к совершенно одинаковым:

    Album
    Album Artist
    Date
    Track
    Disc
    Track Title

    Для тегов это один вариант.

    Но сами concrete release_id сохраняются
    внутри equivalent_releases — они ещё
    пригодятся для обложек и provenance.
    """

    result = []
    by_key = {}

    for context in (
        contexts or []
    ):
        if not context:
            continue

        key = release_context_key(
            context
        )

        group = by_key.get(
            key
        )

        if group is not None:
            group[
                "equivalent_releases"
            ].append(
                context
            )

            continue

        group = {
            "context": context,

            "tag_key": key,

            "equivalent_releases": [
                context
            ],
        }

        by_key[key] = group

        result.append(
            group
        )

    return result


def prepare_release_contexts(
    contexts,
):
    return group_release_contexts(
        contexts or []
    )


def context_matches_album(
    context,
    current_album,
):
    if not current_album:
        return False

    from core.release_titles import is_single,same_album
    if is_single(context) and same_album(context,current_album):return True

    context_album = (
        normalize_album_value(
            context.get(
                "title"
            )
        )
    )

    current_album = (
        normalize_album_value(
            current_album
        )
    )

    return bool(
        context_album
        and current_album
        and context_album
        == current_album
    )


TYPE_ALBUM_SUFFIXES = {
    "ep": (
        " ep",
    ),

    "single": (
        " single",
    ),

    "album": (
        " album",
    ),
}


def context_matches_typed_album_alias(
    context,
    current_album,
):
    """
    Консервативное совпадение Album.

    Разрешаем только случай:

        current:
            Fiber Optic EP

        MusicBrainz title:
            Fiber Optic

        MusicBrainz primary_type:
            EP

    То есть пользовательский Album содержит
    явный суффикс типа, который подтверждается
    самим ReleaseContext.

    Обратное преобразование намеренно
    запрещено:

        current:
            Life As We Know It

        release:
            Life As We Know It EP

    Здесь нельзя предполагать, что речь
    именно об EP.
    """

    if not current_album:
        return False

    release_type = (
        context_primary_type(
            context
        )
    )

    suffixes = (
        TYPE_ALBUM_SUFFIXES.get(
            release_type
        )
    )

    if not suffixes:
        return False

    context_album = (
        normalize_album_value(
            context.get(
                "title"
            )
        )
    )

    current_value = (
        normalize_album_value(
            current_album
        )
    )

    if (
        not context_album
        or not current_value
    ):
        return False

    # Точное совпадение обрабатывается
    # обычным context_matches_album().
    if (
        current_value
        == context_album
    ):
        return False

    for suffix in suffixes:
        if not current_value.endswith(
            suffix
        ):
            continue

        base_value = (
            current_value[
                :-len(suffix)
            ]
            .strip()
        )

        if (
            base_value
            == context_album
        ):
            return True

    return False


def find_related_same_type_competitors(
    selected_group,
    groups,
):
    """
    Ищет другие ReleaseContext,
    которые слишком похожи на вариант,
    выбранный только через typed Album alias.

    Например:

        current:
            Internet Streets EP

        alias candidate:
            Internet Streets (EP)

        competitor:
            Internet Streets Remixed
            (EP / Remix)

    В такой ситуации alias уже недостаточно
    для автоматического выбора.

    Совершенно посторонний EP вариантом-
    конкурентом не считается.
    """

    selected_context = (
        selected_group.get(
            "context"
        )
        or {}
    )

    selected_type = (
        context_primary_type(
            selected_context
        )
    )

    selected_title = (
        normalize_album_value(
            selected_context.get(
                "title"
            )
        )
    )

    if (
        not selected_type
        or not selected_title
    ):
        return []

    competitors = []

    for group in (
        groups or []
    ):
        if group is selected_group:
            continue

        context = (
            group.get(
                "context"
            )
            or {}
        )

        # Compilation / DJ-mix здесь
        # не считаем конкурентом обычному
        # релизу.
        if context_is_risky(
            context
        ):
            continue

        if (
            context_primary_type(
                context
            )
            != selected_type
        ):
            continue

        other_title = (
            normalize_album_value(
                context.get(
                    "title"
                )
            )
        )

        if not other_title:
            continue

        related = (
            other_title
            == selected_title
            or other_title.startswith(
                selected_title + " "
            )
            or selected_title.startswith(
                other_title + " "
            )
        )

        if related:
            competitors.append(
                group
            )

    return competitors


def _result_none(
    reason,
):
    return {
        "status": "NONE",

        "selected": None,

        "options": [],

        "equivalent_releases": [],

        "concrete_release_ambiguous": (
            False
        ),

        "reason": reason,
    }


def _result_ask(
    groups,
    reason,
):
    return {
        "status": "ASK",

        "selected": None,

        "options": [
            group["context"]
            for group in groups
        ],

        "equivalent_releases": [],

        "concrete_release_ambiguous": (
            False
        ),

        "reason": reason,
    }


def _result_selected(
    group,
    reason,
):
    equivalent_releases = (
        group.get(
            "equivalent_releases"
        )
        or []
    )

    return {
        "status": "SELECTED",

        # Это контекст для музыкальных тегов.
        #
        # Если concrete releases несколько,
        # они дают одинаковый tag result.
        "selected": (
            group.get(
                "context"
            )
        ),

        "options": [
            group.get(
                "context"
            )
        ],

        # Конкретные MusicBrainz Release
        # специально НЕ теряем.
        "equivalent_releases": (
            equivalent_releases
        ),

        "concrete_release_ambiguous": (
            len(
                equivalent_releases
            )
            > 1
        ),

        "reason": reason,
    }


def select_release_context(
    contexts,
    preference,
    current_album="",
    safety="SAFE",
):
    """
    Release selector v2.

    Он выбирает не просто строку Album
    и не Release Group, а полноценный
    ReleaseContext.

    Возвращает структуру:

    {
        status:
            SELECTED
            ASK
            NONE

        selected:
            ReleaseContext | None

        options:
            уникальные варианты тегов

        equivalent_releases:
            конкретные release_id,
            эквивалентные для тегов

        concrete_release_ambiguous:
            True, если для тегов вариант
            один, но конкретных изданий
            несколько

        reason:
            объяснение решения
    }
    """

    groups = (
        prepare_release_contexts(
            contexts
        )
    )

    if not groups:
        return _result_none(
            "Подтверждённых конкретных "
            "релизов MusicBrainz нет."
        )

    # -------------------------------------------------
    # НЕНАДЁЖНАЯ ИДЕНТИФИКАЦИЯ
    # -------------------------------------------------

    if safety != "SAFE":
        return _result_ask(
            groups,
            (
                "Recording определена "
                "недостаточно надёжно для "
                "автоматического выбора релиза."
            ),
        )

    # -------------------------------------------------
    # ОДИН РЕАЛЬНЫЙ ВАРИАНТ ТЕГОВ
    # -------------------------------------------------

    if len(groups) == 1:
        group = groups[0]

        context = (
            group["context"]
        )

        if context_is_risky(
            context
        ):
            return _result_ask(
                groups,
                (
                    "Единственный найденный "
                    "вариант является Compilation "
                    "или DJ-mix. Автоматически "
                    "назначать его нельзя."
                ),
            )

        # keep_current означает именно «не перетаскивать трек на другой
        # релиз без подтверждения». Раньше единственный MusicBrainz context
        # автоматически побеждал даже явно заполненный другой Album. Это
        # опасно для старых single/remix-релизов, которые позже появились
        # на reissue/делюкс-альбомах.
        if preference == "keep_current" and current_album:
            exact_match = context_matches_album(
                context,
                current_album,
            )
            alias_match = context_matches_typed_album_alias(
                context,
                current_album,
            )

            if not (exact_match or alias_match):
                return _result_ask(
                    groups,
                    (
                        "Найден один ReleaseContext, но текущий Album уже "
                        "заполнен и указывает на другой релиз. При "
                        "keep_current требуется подтверждение пользователя."
                    ),
                )

        return _result_selected(
            group,
            (
                "После объединения "
                "эквивалентных изданий остался "
                "один однозначный вариант тегов."
            ),
        )

    # -------------------------------------------------
    # ASK
    # -------------------------------------------------

    if preference == "ask":
        return _result_ask(
            groups,
            (
                "Есть несколько реально "
                "различающихся вариантов релиза."
            ),
        )

    # -------------------------------------------------
    # KEEP CURRENT
    # -------------------------------------------------

    if preference == "keep_current":
        if not current_album:
            return _result_ask(
                groups,
                (
                    "Настройка требует сохранять "
                    "текущий Album, но Album "
                    "сейчас пуст."
                ),
            )

        # =========================================
        # 1. СНАЧАЛА ТОЛЬКО ТОЧНОЕ СОВПАДЕНИЕ
        #
        # Это принципиально:
        # exact всегда сильнее любых alias.
        # =========================================

        matching_groups = [
            group
            for group in groups
            if context_matches_album(
                group["context"],
                current_album,
            )
        ]

        match_reason = (
            "ReleaseContext однозначно "
            "совпадает с текущим Album."
        )

        # =========================================
        # 2. ТОЛЬКО ЕСЛИ EXACT НЕТ —
        # ПРОБУЕМ TYPE-AWARE ALIAS
        # =========================================

        if not matching_groups:
            matching_groups = [
                group
                for group in groups
                if (
                    context_matches_typed_album_alias(
                        group[
                            "context"
                        ],
                        current_album,
                    )
                )
            ]

            match_reason = (
                "Текущий Album отличается "
                "только явным суффиксом типа "
                "релиза, подтверждённым "
                "ReleaseContext."
            )

            # Typed alias слабее точного
            # совпадения Album.
            #
            # Если рядом существует другой
            # связанный релиз того же типа,
            # автоматически выбирать один
            # из них нельзя.
            if len(
                matching_groups
            ) == 1:
                competitors = (
                    find_related_same_type_competitors(
                        matching_groups[0],
                        groups,
                    )
                )

                if competitors:
                    return _result_ask(
                        [
                            matching_groups[0],
                            *competitors,
                        ],
                        (
                            "Текущий Album совпал "
                            "только через суффикс "
                            "типа релиза, но найден "
                            "ещё один связанный "
                            "ReleaseContext того же "
                            "типа. Нужен выбор."
                        ),
                    )

        # =========================================
        # 3. ВСЁ ЕЩЁ НИЧЕГО
        # =========================================

        if not matching_groups:
            return _result_ask(
                groups,
                (
                    "Текущий Album не совпадает "
                    "ни с одним подтверждённым "
                    "конкретным релизом."
                ),
            )

        if len(matching_groups) > 1:
            return _result_ask(
                matching_groups,
                (
                    "Текущий Album совпадает "
                    "с несколькими различающимися "
                    "ReleaseContext."
                ),
            )

        group = (
            matching_groups[0]
        )

        if context_is_risky(
            group["context"]
        ):
            return _result_ask(
                matching_groups,
                (
                    "Текущий Album соответствует "
                    "Compilation или DJ-mix. "
                    "Нужно подтверждение."
                ),
            )

        return _result_selected(
            group,
            match_reason,
        )

    # -------------------------------------------------
    # PREFER SINGLE
    # -------------------------------------------------

    if preference == "prefer_single":
        matching_groups = [
            group
            for group in groups
            if (
                context_primary_type(
                    group["context"]
                )
                == "single"
                and not context_is_risky(
                    group["context"]
                )
            )
        ]

        if len(matching_groups) == 1:
            return _result_selected(
                matching_groups[0],
                (
                    "Настройка пользователя: "
                    "предпочитать Single."
                ),
            )

        if len(matching_groups) > 1:
            return _result_ask(
                matching_groups,
                (
                    "Найдено несколько "
                    "различающихся Single. "
                    "Нельзя выбрать один "
                    "только по типу релиза."
                ),
            )

        return _result_ask(
            groups,
            (
                "Подходящий однозначный "
                "Single не найден."
            ),
        )

    # -------------------------------------------------
    # PREFER ALBUM
    #
    # Как и раньше:
    # сначала EP, затем Album.
    # -------------------------------------------------

    if preference == "prefer_album":
        for wanted_type in (
            "ep",
            "album",
        ):
            matching_groups = [
                group
                for group in groups
                if (
                    context_primary_type(
                        group["context"]
                    )
                    == wanted_type
                    and not context_is_risky(
                        group["context"]
                    )
                )
            ]

            if len(
                matching_groups
            ) == 1:
                return _result_selected(
                    matching_groups[0],
                    (
                        "Настройка пользователя: "
                        "предпочитать EP / Album."
                    ),
                )

            if len(
                matching_groups
            ) > 1:
                return _result_ask(
                    matching_groups,
                    (
                        "Найдено несколько "
                        f"различающихся "
                        f"{wanted_type.upper()} "
                        "вариантов. Выбирать один "
                        "автоматически нельзя."
                    ),
                )

        return _result_ask(
            groups,
            (
                "Однозначный EP или Album "
                "не найден."
            ),
        )

    return _result_ask(
        groups,
        (
            "Неизвестная настройка "
            "выбора релиза."
        ),
    )
