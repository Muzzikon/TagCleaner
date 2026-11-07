import re
from collections import Counter


def normalize_title_key(value):
    if not value:
        return ""

    value = value.strip()

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

    return value.casefold()


def resolve_official_track_title(
    release_tracks,
    recording_title=None,
    selected_release=None,
):
    """
    Определяет наиболее подходящее официальное
    название записи.

    Приоритет:

    1. Если пользователь уже выбрал release group,
       смотрим названия именно внутри него.

    2. Иначе смотрим все официальные релизы.

    3. Голосуем по независимым release groups,
       чтобы 20 региональных переизданий одного
       альбома не перевесили один настоящий релиз.
    """

    tracks = [
        item
        for item in (
            release_tracks
            or []
        )
        if item.get(
            "track_title"
        )
    ]

    if not tracks:
        return {
            "status": (
                "NO_OFFICIAL_TRACKS"
            ),
            "title": None,
            "source": None,
            "variants": [],
        }

    source = (
        "all_official_releases"
    )

    # -----------------------------------------
    # ЕСЛИ ВЫБРАН КОНКРЕТНЫЙ RELEASE GROUP
    # -----------------------------------------

    if (
        selected_release
        and selected_release.get("id")
    ):
        selected_group_id = (
            selected_release["id"]
        )

        selected_tracks = [
            item
            for item in tracks
            if item.get(
                "release_group_id"
            )
            == selected_group_id
        ]

        if selected_tracks:
            tracks = (
                selected_tracks
            )

            source = (
                "selected_release_group"
            )

    # -----------------------------------------
    # СОБИРАЕМ ВАРИАНТЫ НАЗВАНИЯ
    # -----------------------------------------

    buckets = {}

    for item in tracks:
        title = (
            item["track_title"]
            .strip()
        )

        key = normalize_title_key(
            title
        )

        if not key:
            continue

        if key not in buckets:
            buckets[key] = {
                "titles": Counter(),
                "release_groups": set(),
                "releases": set(),
            }

        bucket = buckets[key]

        bucket[
            "titles"
        ][title] += 1

        group_id = (
            item.get(
                "release_group_id"
            )
        )

        release_id = (
            item.get(
                "release_id"
            )
        )

        if group_id:
            bucket[
                "release_groups"
            ].add(group_id)

        elif release_id:
            # Если group ID почему-то отсутствует,
            # считаем сам release отдельным источником.
            bucket[
                "release_groups"
            ].add(
                f"release:{release_id}"
            )

        if release_id:
            bucket[
                "releases"
            ].add(release_id)

    variants = []

    for key, bucket in (
        buckets.items()
    ):
        display_title = (
            bucket[
                "titles"
            ].most_common(1)[0][0]
        )

        variants.append({
            "key": key,
            "title": display_title,

            "release_group_count": len(
                bucket[
                    "release_groups"
                ]
            ),

            "release_count": len(
                bucket[
                    "releases"
                ]
            ),
        })

    variants.sort(
        key=lambda item: (
            -item[
                "release_group_count"
            ],
            -item[
                "release_count"
            ],
            item[
                "title"
            ].casefold(),
        )
    )

    if not variants:
        return {
            "status": (
                "NO_OFFICIAL_TRACKS"
            ),
            "title": None,
            "source": None,
            "variants": [],
        }

    # -----------------------------------------
    # ЛУЧШИЙ ВАРИАНТ
    # -----------------------------------------

    best = variants[0]

    best_score = (
        best[
            "release_group_count"
        ],
        best[
            "release_count"
        ],
    )

    winners = [
        item
        for item in variants
        if (
            item[
                "release_group_count"
            ],
            item[
                "release_count"
            ],
        )
        == best_score
    ]

    # Один явный победитель
    if len(winners) == 1:
        return {
            "status": "RESOLVED",
            "title": best["title"],
            "source": source,
            "variants": variants,
        }

    # -----------------------------------------
    # НИЧЬЯ
    #
    # Если Recording.title совпадает с одним
    # из официальных вариантов — используем его.
    # -----------------------------------------

    recording_key = (
        normalize_title_key(
            recording_title
        )
    )

    if recording_key:
        for item in winners:
            if (
                item["key"]
                == recording_key
            ):
                return {
                    "status": (
                        "RESOLVED_BY_RECORDING"
                    ),
                    "title": (
                        item["title"]
                    ),
                    "source": (
                        source
                    ),
                    "variants": (
                        variants
                    ),
                }

    # -----------------------------------------
    # ДЕЙСТВИТЕЛЬНО НЕОДНОЗНАЧНО
    # -----------------------------------------

    return {
        "status": "AMBIGUOUS",
        "title": None,
        "source": source,
        "variants": variants,
    }