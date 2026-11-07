"""Explicit artist spelling corrections, backed by primary sources.

Do not infer artist identity or split names at commas here.
"""
import re

SOURCES = {
    "Hi I'm Ghost": (
        'https://www.hiimghostsound.com/',
        'https://firepowerrecords.com/artists/hi-im-ghost/',
    ),
}
_GHOST = re.compile(r"(?<![\w'])hi\s*,?\s+i['’`]m\s+ghost(?![\w])", re.I)


def artist_credit(value):
    text = str(value or '')
    def replace(match):
        before, after = text[:match.start()], text[match.end():]
        # Exact artist token inside a credit, not a longer artist's name.
        left = not before.strip(' ([‘“') or re.search(
            r'(?i)(?:[,;&]|\b(?:feat\.?|ft\.?|featuring|with|vs\.?|x|and))\s*$', before)
        right = not after.strip(' )]’”') or re.match(
            r"(?i)\s*(?:[,;&)\]]|\b(?:feat\.?|ft\.?|featuring|with|vs\.?|x|and|remix|edit|flip|bootleg|rework|reboot)\b|['’]s\b)", after)
        return "Hi I'm Ghost" if left and right else match[0]
    return _GHOST.sub(replace, text)


def title_credit(value):
    """Only explicit feat./with and version credits; leave song prose alone."""
    text = str(value or '')
    text = re.sub(r'(?i)(\b(?:feat\.?|ft\.?|featuring|with)\s+)([^()\[\]]+)',
                  lambda m: m[1] + artist_credit(m[2]), text)
    text = re.sub(r'\([^()]*\)|\[[^\[\]]*\]',
                  lambda m: artist_credit(m[0]) if re.search(
                      r'(?i)\b(?:remix|edit|flip|bootleg|rework|reboot)\b', m[0]) else m[0], text)
    return re.sub(r'(\s[-–—]\s)([^()\[\]]+)$',
                  lambda m: m[1] + artist_credit(m[2]) if re.search(
                      r'(?i)\b(?:remix|edit|flip|bootleg|rework|reboot)\b', m[2]) else m[0], text)


def field_value(field, value):
    if field in ('artist', 'albumartist', 'remixer'):
        return artist_credit(value)
    if field == 'title':
        return title_credit(value)
    return value
