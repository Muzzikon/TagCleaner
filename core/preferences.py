import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "preferences.json"


DEFAULT_PREFERENCES = {
    # Что делать, если одна запись есть
    # одновременно на сингле, EP, альбоме, сборнике
    #
    # ask
    # prefer_single
    # prefer_album
    # keep_current
    "release_preference": "ask",

    # Какую обложку предпочитать в будущем
    #
    # ask
    # prefer_single
    # prefer_album
    # keep_current
    "cover_preference": "ask",

    # Единый финальный стиль тегов (Apple-like):
    # A x B / A and B -> A & B, ft. -> feat.,
    # единое оформление Remix/VIP/Edit и известных серий Album.
    # Поиск/сопоставление при этом остаются гибкими.
    "normalize_names": True,
    "artist_name_preference": "historical",
}


def load_preferences():
    CONFIG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not CONFIG_FILE.exists():
        save_preferences(
            DEFAULT_PREFERENCES.copy()
        )

        return DEFAULT_PREFERENCES.copy()

    try:
        with CONFIG_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

    except Exception:
        data = {}

    result = DEFAULT_PREFERENCES.copy()
    result.update(data)

    return result


def save_preferences(preferences):
    CONFIG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    with CONFIG_FILE.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            preferences,
            file,
            ensure_ascii=False,
            indent=2,
        )


def configure_preferences(
    current,
):
    print()
    print("Настройки TagCleaner")
    print()

    print("Как выбирать релиз, если вариантов несколько?")
    print("1 — не выбирать автоматически")
    print("2 — предпочитать отдельный сингл")
    print("3 — предпочитать альбом / EP")
    print("4 — сохранять текущий Album")

    release_choice = input(
        "Выбор [Enter = оставить как есть]: "
    ).strip()

    release_map = {
        "1": "ask",
        "2": "prefer_single",
        "3": "prefer_album",
        "4": "keep_current",
    }

    if release_choice in release_map:
        current["release_preference"] = (
            release_map[release_choice]
        )

    print()
    print("Какую обложку предпочитать в будущем?")
    print("1 — спрашивать")
    print("2 — сингловую")
    print("3 — альбомную")
    print("4 — оставлять текущую")

    cover_choice = input(
        "Выбор [Enter = оставить как есть]: "
    ).strip()

    cover_map = {
        "1": "ask",
        "2": "prefer_single",
        "3": "prefer_album",
        "4": "keep_current",
    }

    if cover_choice in cover_map:
        current["cover_preference"] = (
            cover_map[cover_choice]
        )

    print()
    print("Единый стиль написания тегов?")
    print("1 — да, Apple-like стиль TagCleaner")
    print("2 — нет, сохранять оформление источников")

    naming_choice = input(
        "Выбор [Enter = оставить как есть]: "
    ).strip()

    if naming_choice == "1":
        current["normalize_names"] = True
    elif naming_choice == "2":
        current["normalize_names"] = False

    print()
    print("Исторические имена артистов (только при подтверждённом MBID):")
    print("1 — сохранять credit выбранного релиза")
    print("2 — использовать текущее имя той же сущности")
    print("3 — предложить оба варианта для ручной проверки")
    choice = input("Выбор [Enter = оставить как есть]: ").strip()
    names = {"1": "historical", "2": "current", "3": "ask"}
    if choice in names:
        current["artist_name_preference"] = names[choice]
    save_preferences(current)

    return current