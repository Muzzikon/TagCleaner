import re


URL_RE = re.compile(
    r"(?i)"
    r"(?:https?://|www\.)"
)


JUNK_PHRASES = (
    "downloaded from",
    "downloaded at",
    "free download",
    "visit ",
    "follow us",
    "subscribe",
    "youtube.com/",
    "youtu.be/",
    "soundcloud.com/",
    "vk.com/",
    "bandcamp.com/",
    "facebook.com/",
    "twitter.com/",
    "instagram.com/",
)


def normalize_key(
    raw_key,
):
    return (
        str(raw_key)
        .strip()
        .casefold()
    )


def normalize_values(
    values,
):
    result = []

    for value in (
        values or []
    ):
        text = str(
            value
        ).strip()

        if text:
            result.append(
                text
            )

    return result


def joined_text(
    values,
):
    return " ".join(
        normalize_values(
            values
        )
    )


def is_empty_tag(
    values,
):
    return not normalize_values(
        values
    )


def is_url_only(
    values,
):
    values = normalize_values(
        values
    )

    if not values:
        return False

    for value in values:
        compact = (
            value.strip()
        )

        if not URL_RE.search(
            compact
        ):
            return False

        # URL + большой осмысленный текст
        # автоматически не стираем.
        without_urls = re.sub(
            r"https?://\S+",
            "",
            compact,
            flags=re.IGNORECASE,
        )

        without_urls = re.sub(
            r"www\.\S+",
            "",
            without_urls,
            flags=re.IGNORECASE,
        )

        if without_urls.strip(
            " -:;,.()[]"
        ):
            return False

    return True


def looks_like_download_junk(
    values,
):
    text = (
        joined_text(
            values
        )
        .casefold()
    )

    if not text:
        return False

    return any(
        phrase in text
        for phrase
        in JUNK_PHRASES
    )


def detect_logical_tag(
    raw_key,
):
    key = normalize_key(
        raw_key
    )

    # =========================================
    # ОСНОВНЫЕ ID3-ТЕГИ
    # Они уже проверяются главным planner.
    # =========================================

    if key.startswith("tit2"):
        return "core_title"

    if key.startswith("tpe1"):
        return "core_artist"

    if key.startswith("talb"):
        return "core_album"

    if key.startswith("tpe2"):
        return "albumartist"

    # =========================================
    # RELEASE / IDENTIFIERS
    # =========================================

    if key.startswith("trck"):
        return "tracknumber"

    if key.startswith("tpos"):
        return "discnumber"

    if key.startswith("tdrc"):
        return "date"

    if key.startswith("tdor"):
        return "original_date"

    if key.startswith("tyer"):
        return "date"

    if key.startswith("tsrc"):
        return "isrc"

    if key.startswith(
        "txxx:originalyear"
    ):
        return "original_date"

    if key.startswith(
        "txxx:barcode"
    ):
        return "barcode"

    if key.startswith("tmed"):
        return "media_type"

    # =========================================
    # SORT / STRUCTURE
    # =========================================

    if (
        key.startswith("tsop")
        or key.startswith("tso2")
    ):
        return "sort_order"

    if key.startswith(
        "txxx:script"
    ):
        return "script"

    if key.startswith(
        "txxx:artists"
    ):
        return "structured_artists"

    # =========================================
    # USER DATA
    # =========================================

    if key.startswith("popm"):
        return "rating"

    if key.startswith("comm"):
        return "comment"

    if key.startswith(
        "txxx:description"
    ):
        return "description"

    if key.startswith(
        "txxx:synopsis"
    ):
        return "description"

    if key.startswith(
        "txxx:purl"
    ):
        return "source_url"

    if key.startswith("tcon"):
        return "genre"

    if key.startswith("uslt"):
        return "lyrics"

    if key.startswith("tcop"):
        return "copyright"

    if key.startswith("tpub"):
        return "publisher"

    # =========================================
    # TECHNICAL
    # =========================================

    if key.startswith("tenc"):
        return "encoded_by"

    if key.startswith("tsse"):
        return "encoder_settings"

    # =========================================
    # URL
    # =========================================

    if (
        key.startswith("wo")
        or key.startswith("wxxx")
    ):
        return "source_url"

    # =========================================
    # SIMPLE / VORBIS / EASY
    # =========================================

    simple_map = {
        "artist": "core_artist",
        "title": "core_title",
        "album": "core_album",

        "albumartist": "albumartist",
        "album_artist": "albumartist",

        "tracknumber": "tracknumber",
        "track": "tracknumber",

        "discnumber": "discnumber",
        "disc": "discnumber",

        "date": "date",
        "year": "date",

        "originaldate": "original_date",
        "originalyear": "original_date",

        "isrc": "isrc",
        "barcode": "barcode",

        "comment": "comment",
        "description": "description",

        "genre": "genre",
        "lyrics": "lyrics",
        "unsyncedlyrics": "lyrics",

        "copyright": "copyright",
        "publisher": "publisher",
        "label": "publisher",
        "organization": "publisher",

        "encodedby": "encoded_by",
        "encoded_by": "encoded_by",
        "encoder": "encoder_settings",

        "url": "source_url",
        "website": "source_url",
    }

    if key in simple_map:
        return simple_map[
            key
        ]

    # =========================================
    # MP4
    # =========================================

    if key == "©nam":
        return "core_title"

    if key == "©art":
        return "core_artist"

    if key == "©alb":
        return "core_album"

    if key == "aart":
        return "albumartist"

    if key == "trkn":
        return "tracknumber"

    if key == "disk":
        return "discnumber"

    if key == "©day":
        return "date"

    if key == "©cmt":
        return "comment"

    if key == "©gen":
        return "genre"

    if key == "©lyr":
        return "lyrics"

    if key == "©too":
        return "encoder_settings"

    if key == "cprt":
        return "copyright"

    # =========================================
    # MUSICBRAINZ / ACOUSTID
    # =========================================

    if key.startswith(
        "txxx:musicbrainz"
    ):
        return "external_id"

    if key.startswith(
        "txxx:acoustid"
    ):
        return "external_id"

    if key.startswith(
        "ufid:http://musicbrainz.org"
    ):
        return "external_id"


    # =========================================
    # RELEASE IDENTIFIERS
    # =========================================

    if key.startswith(
        "txxx:catalognumber"
    ):
        return "catalog_number"

    if key.startswith(
        "txxx:catalog number"
    ):
        return "catalog_number"


    # =========================================
    # CHAPTERS
    # =========================================

    if key.startswith("chap:"):
        return "chapter"

    if key.startswith("ctoc:"):
        return "chapter"

    return "unknown"


def parse_position(
    values,
):
    values = normalize_values(
        values
    )

    if len(values) != 1:
        return None

    match = re.fullmatch(
        r"\s*(\d+)"
        r"(?:\s*/\s*(\d+))?"
        r"\s*",
        values[0],
    )

    if not match:
        return None

    number = int(
        match.group(1)
    )

    total = (
        int(match.group(2))
        if match.group(2)
        else None
    )

    return {
        "number": number,
        "total": total,
    }


def classify_tag(
    raw_key,
    values,
):
    """
    Возвращает:

    KEEP
    SAFE_DELETE
    REVIEW
    RELEASE_DEPENDENT
    """

    logical = detect_logical_tag(
        raw_key
    )

    values = normalize_values(
        values
    )

    if is_empty_tag(
        values
    ):
        return {
            "status": (
                "SAFE_DELETE"
            ),
            "logical": logical,
            "reason": (
                "Пустое поле."
            ),
        }

        # =========================================
    # CORE
    # =========================================

    if logical in {
        "core_artist",
        "core_title",
        "core_album",
    }:
        return {
            "status": "CORE",
            "logical": logical,
            "reason": (
                "Поле проверяется "
                "основным TagCleaner planner."
            ),
        }

    # =========================================
    # TRACK / DISC
    # =========================================

    if logical in {
        "tracknumber",
        "discnumber",
    }:
        position = parse_position(
            values
        )

        if position is None:
            return {
                "status": "REVIEW",
                "logical": logical,
                "reason": (
                    "Непонятный формат "
                    "номера."
                ),
            }

        number = position[
            "number"
        ]

        total = position[
            "total"
        ]

        if number < 1:
            return {
                "status": "REVIEW",
                "logical": logical,
                "reason": (
                    "Номер должен быть "
                    "больше нуля."
                ),
            }

        if (
            total is not None
            and number > total
        ):
            return {
                "status": "REVIEW",
                "logical": logical,
                "reason": (
                    f"Номер {number} "
                    f"больше общего "
                    f"количества {total}."
                ),
            }

        return {
            "status": (
                "RELEASE_DEPENDENT"
            ),
            "logical": logical,
            "reason": (
                "Проверяется относительно "
                "выбранного релиза."
            ),
        }

        # =========================================
    # IDENTIFIERS
    # =========================================

    if logical in {
        "isrc",
        "barcode",
        "external_id",
        "catalog_number",
    }:
        return {
            "status": "KEEP",
            "logical": logical,
            "reason": (
                "Полезный идентификатор "
                "записи или релиза."
            ),
        }

    # =========================================
    # ORIGINAL DATE
    # =========================================

    if logical == "original_date":
        return {
            "status": "KEEP",
            "logical": logical,
            "reason": (
                "Дата первоначального "
                "выпуска может отличаться "
                "от даты выбранного "
                "переиздания."
            ),
        }

    # =========================================
    # USER DATA
    # =========================================

    if logical in {
        "rating",
        "sort_order",
        "script",
        "structured_artists",
        "media_type",
        "chapter",
    }:
        return {
            "status": "KEEP",
            "logical": logical,
            "reason": (
                "Поле может содержать "
                "полезные структурированные "
                "данные."
            ),
        }

    # ----------------------------------
    # СОДЕРЖАТЕЛЬНЫЕ ПОЛЯ
    # ----------------------------------

    if logical in {
        "lyrics",
        "copyright",
        "publisher",
        "genre",
    }:
        return {
            "status": "KEEP",
            "logical": logical,
            "reason": (
                "Поле может содержать "
                "полезные данные."
            ),
        }

    # ----------------------------------
    # ЗАВИСЯТ О РЕЛИЗЕ
    # ----------------------------------

    if logical in {
        "date",
        "albumartist",
        "tracknumber",
        "discnumber",
    }:
        return {
            "status": (
                "RELEASE_DEPENDENT"
            ),
            "logical": logical,
            "reason": (
                "Значение должно "
                "проверяться относительно "
                "выбранного релиза."
            ),
        }

    # ----------------------------------
    # COMMENTS
    # ----------------------------------

    if logical == "comment":
        if is_url_only(
            values
        ):
            return {
                "status": (
                    "SOURCE_REFERENCE"
                ),
                "logical": logical,
                "reason": (
                    "Комментарий содержит "
                    "только ссылку. "
                    "Возможно, это источник "
                    "трека."
                ),
            }

        if looks_like_download_junk(
            values
        ):
            return {
                "status": "REVIEW",
                "logical": logical,
                "reason": (
                    "Похоже на рекламный "
                    "или download-мусор."
                ),
            }

        return {
            "status": "REVIEW",
            "logical": logical,
            "reason": (
                "Комментарий может "
                "содержать полезную "
                "информацию."
            ),
        }

    if logical == "description":
        return {
            "status": "REVIEW",
            "logical": logical,
            "reason": (
                "Описание может сочетать "
                "полезный текст и "
                "рекламный мусор."
            ),
        }

    # ----------------------------------
    # URL
    # ----------------------------------

    if logical == "source_url":
        return {
            "status": (
                "SOURCE_REFERENCE"
            ),
            "logical": logical,
            "reason": (
                "Ссылка может быть "
                "источником происхождения "
                "трека. Сначала сохранить "
                "как provenance, потом "
                "решать вопрос удаления."
            ),
        }

    # ----------------------------------
    # ENCODER
    #
    # Пока не стираем автоматически:
    # это техническая информация,
    # а не всегда мусор.
    # ----------------------------------

    if logical in {
        "encoded_by",
        "encoder_settings",
    }:
        return {
            "status": "REVIEW",
            "logical": logical,
            "reason": (
                "Технический тег "
                "кодировщика. "
                "Можно удалить по "
                "политике библиотеки."
            ),
        }

    # ----------------------------------
    # UNKNOWN
    # ----------------------------------

    return {
        "status": "REVIEW",
        "logical": logical,
        "reason": (
            "Неизвестное дополнительное "
            "поле. Автоматически "
            "не удаляется."
        ),
    }


def analyze_raw_tags(
    raw_tags,
):
    findings = []

    for raw_key, values in (
        raw_tags or {}
    ).items():

        classification = (
            classify_tag(
                raw_key,
                values,
            )
        )

        findings.append({
            "raw_key": raw_key,
            "values": (
                normalize_values(
                    values
                )
            ),
            **classification,
        })

    return findings