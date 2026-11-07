from importlib.resources import path
from pathlib import Path
from mutagen import File
from core.extra_tags import read_remixer
from core.tag_access import read_value
from core.cache_utils import file_signature
from core.tag_policy import (
    analyze_raw_tags,
)


AUDIO_EXTENSIONS = {
    ".mp3",
    ".flac",
    ".m4a",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".aiff",
    ".aif",
}


BINARY_TAG_PREFIXES = {
    "APIC",
    "PIC",
    "covr",
    "metadata_block_picture",
    "coverart",
}


def _stringify_tag_value(
    value,
):
    """
    Приводит значение Mutagen-тега
    к обычному списку строк.

    Бинарные картинки сюда не должны попадать.
    """

    if value is None:
        return []

    if hasattr(
        value,
        "text",
    ):
        text = value.text

        if isinstance(
            text,
            (list, tuple),
        ):
            return [
                str(item)
                for item in text
            ]

        return [
            str(text)
        ]

    if isinstance(
        value,
        (list, tuple),
    ):
        result = []

        for item in value:
            if isinstance(
                item,
                bytes,
            ):
                continue

            result.append(
                str(item)
            )

        return result

    if isinstance(
        value,
        bytes,
    ):
        return []

    return [
        str(value)
    ]


def _is_binary_tag(
    key,
    value,
):
    key_text = str(key)

    for prefix in (
        BINARY_TAG_PREFIXES
    ):
        if key_text.startswith(
            prefix
        ):
            return True

    if isinstance(
        value,
        bytes,
    ):
        return True

    return False


def extract_raw_tags(
    path,
    *,
    raw_audio=None,
):
    """
    Читает ВСЕ доступные текстовые теги,
    а не только easy=True поля.

    Возвращает:
    {
        "TIT2": ["Track"],
        "COMM::eng": ["..."],
        "TPOS": ["1/2"],
        ...
    }
    """

    if raw_audio is None:
        raw_audio = File(path, easy=False)

    if (
        raw_audio is None
        or raw_audio.tags is None
    ):
        # Файл без тегов — нормальный вход для TagCleaner.
        # Раньше здесь был рекурсивный вызов extract_raw_tags(),
        # из-за которого полностью нетегированный файл уходил
        # в бесконечную рекурсию вместо возврата пустого набора.
        return {}

    result = {}

    for key, value in (
        raw_audio.tags.items()
    ):
        if _is_binary_tag(
            key,
            value,
        ):
            continue

        values = (
            _stringify_tag_value(
                value
            )
        )

        if not values:
            continue

        result[
            str(key)
        ] = values

    return result


def first(tags, *names):
    if not tags:
        return None

    for name in names:
        value = tags.get(name)

        if value:
            if isinstance(value, list):
                return str(value[0])

            return str(value)

    return None


def scan_file(file_path, *, raw_audio=None):
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    if not path.is_file():
        raise ValueError(f"Ожидался файл: {path}")

    before = file_signature(path)
    audio = File(path, easy=True)

    if audio is None:
        raise ValueError("Формат файла не поддерживается")

    tags = audio.tags or {}

    raw_tags = extract_raw_tags(path, raw_audio=raw_audio)

    tag_findings = analyze_raw_tags(
        raw_tags
    )

    info = {
        "path": str(path),
        "file": path.name,
        "extension": path.suffix.lower(),

        "artist": read_value(audio, "artist"),
        "title": read_value(audio, "title"),
        "album": read_value(audio, "album"),
        "albumartist": read_value(audio, "albumartist"),
        "date": read_value(audio, "date") or first(tags, "year"),
        "tracknumber": read_value(audio, "tracknumber"),
        "discnumber": read_value(audio, "discnumber"),
        "genre": read_value(audio, "genre"),
        "remixer": read_remixer(audio),

        "raw_tags": raw_tags,
        "tag_findings": tag_findings,

        "duration": None,
        "bitrate": None,
    }

    if audio.info:
        length = getattr(audio.info, "length", None)
        bitrate = getattr(audio.info, "bitrate", None)

        if length is not None:
            info["duration"] = round(length, 2)

        if bitrate is not None:
            info["bitrate"] = round(bitrate / 1000)

    if file_signature(path) != before:
        raise RuntimeError('Файл изменился во время чтения тегов; анализ нужно повторить.')
    info['analysis_file_signature'] = before
    return info


def find_audio_files(input_path):
    path = Path(input_path)

    if not path.exists():
        raise FileNotFoundError(f"Путь не найден: {path}")

    if path.is_file():
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError("Это не поддерживаемый аудиофайл")

        return [path]

    files = []

    for file in path.rglob("*"):
        if file.is_file() and file.suffix.lower() in AUDIO_EXTENSIONS and '.tagcleaner-trash' not in [p.casefold() for p in file.parts]:
            files.append(file)

    return sorted(files)
