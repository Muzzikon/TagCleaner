import html
import re


def decode_html_entities(value):
    """
    Исправляет техническое HTML-кодирование.

    &amp;  -> &
    &quot; -> "
    &#39;  -> '
    """

    if not value:
        return value

    value = str(value)

    previous = None

    # Иногда встречается:
    # &amp;amp;
    #
    # Несколько проходов позволяют
    # безопасно распаковать такое кодирование.
    for _ in range(3):
        if value == previous:
            break

        previous = value

        value = html.unescape(
            value
        )

    return value


def clean_duplicate_brackets(value):
    """
    Убирает только очевидные
    дублирующиеся оболочки.

    ((VIP)) -> (VIP)
    [[VIP]] -> [VIP]

    Вложенные конструкции вроде:
    Remix (VIP (Edit))
    специально не ломаем.
    """

    if not value:
        return value

    value = str(value)

    previous = None

    while value != previous:
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


def clean_whitespace(value):
    if not value:
        return value

    value = str(value)

    # NBSP
    value = value.replace(
        "\u00a0",
        " ",
    )

    # Zero-width space
    value = value.replace(
        "\u200b",
        "",
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def clean_local_text(value):
    if value is None:
        return ""

    value = str(value)

    value = decode_html_entities(
        value
    )

    value = clean_duplicate_brackets(
        value
    )

    value = clean_whitespace(
        value
    )

    return value


def analyze_local_cleanup(value):
    original = (
        ""
        if value is None
        else str(value)
    )

    current = original

    reasons = []

    # -----------------------------------------
    # HTML
    # -----------------------------------------

    new_value = decode_html_entities(
        current
    )

    if new_value != current:
        reasons.append(
            "HTML-кодирование"
        )

        current = new_value

    # -----------------------------------------
    # СКОБКИ
    # -----------------------------------------

    new_value = clean_duplicate_brackets(
        current
    )

    if new_value != current:
        reasons.append(
            "двойные скобки"
        )

        current = new_value

    # -----------------------------------------
    # ПРОБЕЛЫ
    # -----------------------------------------

    new_value = clean_whitespace(
        current
    )

    if new_value != current:
        reasons.append(
            "лишние пробелы"
        )

        current = new_value

    return {
        "changed": (
            current != original
        ),
        "original": original,
        "value": current,
        "reasons": reasons,
    }