import re
from pathlib import Path


PLACEHOLDER_ARTISTS = {
    "unknown artist",
    "unknown",
    "various",
    "various artists",
}

PLACEHOLDER_TITLES = {
    "unknown",
    "unknown title",
    "track",
    "track 01",
    "audio",
}

PROMO_PATTERNS = [
    r"\[free download\]",
    r"\(free download\)",
    r"\[official audio\]",
    r"\[official video\]",
    r"\[premiere\]",
    r"\[exclusive\]",
]

PREFIX_GARBAGE = [
    r"^\s*dubstep\s*::\s*",
    r"^\s*drum\s*&\s*bass\s*::\s*",
    r"^\s*edm\s*::\s*",
]


def add_issue(issues, severity, code, message, suggestion=None):
    issues.append({
        "severity": severity,
        "code": code,
        "message": message,
        "suggestion": suggestion,
    })


def normalize_text(text):
    return re.sub(r"\s+", " ", text or "").strip().lower()


def split_artists(value):
    if not value:
        return set()

    parts = re.split(
        r"\s*(?:&|,|\bx\b|\bvs\.?\b)\s*",
        value,
        flags=re.IGNORECASE,
    )

    return {
        normalize_text(part)
        for part in parts
        if normalize_text(part)
    }


def remove_extension(filename):
    return Path(filename).stem


def split_filename(filename):
    """
    Пытаемся разобрать стандартный filename:
    Artist - Title.mp3
    """
    stem = remove_extension(filename)

    if " - " not in stem:
        return None, None

    artist, title = stem.split(" - ", 1)

    return artist.strip(), title.strip()


def audit_tags(data):
    issues = []

    artist = (data.get("artist") or "").strip()
    title = (data.get("title") or "").strip()
    filename = data.get("file") or ""

    file_artist, file_title = split_filename(filename)

    # --------------------------------------------------
    # 1. Отсутствующие данные
    # --------------------------------------------------

    if not artist:
        add_issue(
            issues,
            "ERROR",
            "MISSING_ARTIST",
            "Не указан исполнитель.",
        )

    if not title:
        add_issue(
            issues,
            "ERROR",
            "MISSING_TITLE",
            "Не указано название трека.",
        )

    # --------------------------------------------------
    # 2. Заглушки
    # --------------------------------------------------

    if artist.lower() in PLACEHOLDER_ARTISTS:
        add_issue(
            issues,
            "WARNING",
            "PLACEHOLDER_ARTIST",
            f'Исполнитель выглядит как заглушка: "{artist}".',
        )

    if title.lower() in PLACEHOLDER_TITLES:
        add_issue(
            issues,
            "WARNING",
            "PLACEHOLDER_TITLE",
            f'Название выглядит как заглушка: "{title}".',
        )


    # --------------------------------------------------
    # 3. Сравнение filename и Artist
    # --------------------------------------------------

    if file_artist and artist:
        file_artists = split_artists(file_artist)
        tag_artists = split_artists(artist)

        if file_artists and tag_artists:
            if file_artists == tag_artists:
                pass

            elif file_artists.issuperset(tag_artists):
                missing = file_artists - tag_artists

                add_issue(
                    issues,
                    "WARNING",
                    "POSSIBLE_MISSING_PRIMARY_ARTIST",
                    (
                        "В имени файла указаны исполнители, "
                        "которых нет в теге Artist."
                    ),
                    (
                        "Возможно, отсутствуют: "
                        + ", ".join(sorted(missing))
                        + ". Требуется проверка."
                    ),
                )

            elif file_artists.isdisjoint(tag_artists):
                add_issue(
                    issues,
                    "WARNING",
                    "ARTIST_FILENAME_MISMATCH",
                    (
                        f'Исполнитель в имени файла ("{file_artist}") '
                        f'не совпадает с Artist ("{artist}").'
                    ),
                    "Проверить данные по независимому источнику.",
                )

            else:
                add_issue(
                    issues,
                    "WARNING",
                    "ARTIST_PARTIAL_MISMATCH",
                    (
                        f'Состав исполнителей различается: '
                        f'"{file_artist}" / "{artist}".'
                    ),
                    "Проверить роли и состав основных исполнителей.",
                )


    # --------------------------------------------------
    # 4. Artist оказался внутри Title
    # --------------------------------------------------

    if artist and title:
        # Например:
        # Artist = MadRats Fans
        # Title  = MadRats - DJ SKILLZ

        title_prefix_match = re.match(
            r"^(.+?)\s+-\s+(.+)$",
            title,
        )

        if title_prefix_match:
            embedded_artist = title_prefix_match.group(1).strip()
            embedded_title = title_prefix_match.group(2).strip()

            if normalize_text(embedded_artist) != normalize_text(artist):
                add_issue(
                    issues,
                    "WARNING",
                    "EMBEDDED_ARTIST_IN_TITLE",
                    (
                        f'Название похоже на строку "Artist - Title": '
                        f'"{title}". При этом Artist содержит "{artist}".'
                    ),
                    (
                        f"Возможный исполнитель: {embedded_artist}; "
                        f"возможное название: {embedded_title}. "
                        "Требуется проверка."
                    ),
                )

    # --------------------------------------------------
    # 5. feat / ft / featuring
    # --------------------------------------------------

    feat_pattern = re.compile(
        r"\b(?:ft\.?|feat\.?|featuring)\s+([^\[\]()]+)",
        re.IGNORECASE,
    )

    feat_match = feat_pattern.search(title)

    if feat_match:
        featured_artist = feat_match.group(1).strip()

        # Убираем мусор после имени
        featured_artist = featured_artist.rstrip(" -")

        normalized_title = re.sub(
            feat_pattern,
            f"feat. {featured_artist}",
            title,
            count=1,
        )

        # Если feat стоит без скобок
        if not re.search(
            r"\(feat\.\s+.+?\)",
            normalized_title,
            re.IGNORECASE,
        ):
            before = title[:feat_match.start()].rstrip()

            after = title[feat_match.end():].strip()

            normalized_title = (
                f"{before} (feat. {featured_artist})"
            )

            if after:
                normalized_title += f" {after}"

        if normalize_text(normalized_title) != normalize_text(title):
            add_issue(
                issues,
                "WARNING",
                "FEATURED_FORMAT",
                "Обозначение приглашённого исполнителя можно привести к единому виду.",
                normalized_title,
            )

    # --------------------------------------------------
    # 6. prod. by
    # --------------------------------------------------

    producer_pattern = re.compile(
        r"\(?\bprod(?:uced)?\.?\s+by\s+([^)]+)\)?",
        re.IGNORECASE,
    )

    producer_match = producer_pattern.search(title)

    if producer_match:
        producer = producer_match.group(1).strip()

        add_issue(
            issues,
            "INFO",
            "PRODUCER_IN_TITLE",
            (
                f'В названии найдено указание продюсера: "{producer}". '
                "Это может быть официальной частью названия."
            ),
            (
                "Не изменять автоматически. Проверить официальный источник "
                "и роль участника."
            ),
        )

    # --------------------------------------------------
    # 7. Рекламный / технический мусор
    # --------------------------------------------------

    for pattern in PROMO_PATTERNS:
        if re.search(pattern, title, re.IGNORECASE):
            cleaned = re.sub(
                pattern,
                "",
                title,
                flags=re.IGNORECASE,
            )

            cleaned = re.sub(r"\s+", " ", cleaned).strip()

            add_issue(
                issues,
                "WARNING",
                "PROMO_TEXT",
                f'В названии обнаружен служебный текст: "{title}".',
                (
                    f"Возможное чистое название: {cleaned}. "
                    "Перед изменением требуется проверка."
                ),
            )

            break

    # --------------------------------------------------
    # 8. Жанр / источник в начале Title
    # --------------------------------------------------

    for pattern in PREFIX_GARBAGE:
        if re.search(pattern, title, re.IGNORECASE):
            cleaned = re.sub(
                pattern,
                "",
                title,
                flags=re.IGNORECASE,
            ).strip()

            add_issue(
                issues,
                "WARNING",
                "SOURCE_PREFIX",
                f'В начале Title найден подозрительный префикс: "{title}".',
                (
                    f"Возможный вариант без префикса: {cleaned}. "
                    "Требуется проверка."
                ),
            )

            break

    # --------------------------------------------------
    # 9. FINAL / copy и т.п.
    # --------------------------------------------------

    garbage_patterns = [
        r"\bfinal\s*\d*\b",
        r"\bcopy\s*\d*\b",
        r"\bnew\s*mix\s*\d*\b",
    ]

    for pattern in garbage_patterns:
        if re.search(pattern, title, re.IGNORECASE):
            add_issue(
                issues,
                "WARNING",
                "POSSIBLE_GARBAGE",
                f'Название содержит подозрительный технический текст: "{title}".',
                "Проверить название по внешнему источнику.",
            )
            break

    return issues

# --------------------------------------------------
# СИНХРОНИЗАЦИЯ ЛОКАЛЬНОГО АУДИТА С ФИНАЛЬНЫМ PLAN
# --------------------------------------------------

_ARTIST_RESOLVED_CODES = {
    "MISSING_ARTIST",
    "PLACEHOLDER_ARTIST",
    "POSSIBLE_MISSING_PRIMARY_ARTIST",
    "ARTIST_FILENAME_MISMATCH",
    "ARTIST_PARTIAL_MISMATCH",
}

_TITLE_RESOLVED_CODES = {
    "MISSING_TITLE",
    "PLACEHOLDER_TITLE",
    "FEATURED_FORMAT",
    "PROMO_TEXT",
    "SOURCE_PREFIX",
    "POSSIBLE_GARBAGE",
    "PRODUCER_IN_TITLE",
}

_BOTH_RESOLVED_CODES = {
    "EMBEDDED_ARTIST_IN_TITLE",
}


def reconcile_issues_with_change_plan(issues, change_plan):
    """
    Убирает из итогового аудита только те локальные предупреждения,
    которые уже гарантированно закрываются SAFE-операцией финального
    change_plan.

    REVIEW/ASK/BLOCKED намеренно ничего не скрывают: пока пользователь
    не подтвердил изменение, локальная проблема остаётся актуальной.
    """

    issues = list(issues or [])
    change_plan = change_plan or {}

    artist_safe = (change_plan.get("artist") or {}).get("status") == "SAFE"
    title_safe = (change_plan.get("title") or {}).get("status") == "SAFE"

    if not artist_safe and not title_safe:
        return issues

    result = []

    for issue in issues:
        code = str((issue or {}).get("code") or "").strip().upper()

        if artist_safe and code in _ARTIST_RESOLVED_CODES:
            continue

        if title_safe and code in _TITLE_RESOLVED_CODES:
            continue

        if artist_safe and title_safe and code in _BOTH_RESOLVED_CODES:
            continue

        result.append(issue)

    return result
