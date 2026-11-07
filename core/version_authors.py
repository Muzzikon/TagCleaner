"""Reviewable author suggestions from explicit local version captions only."""
import re

_KINDS = r'remix|bootleg|edit|flip|rework|reboot|vip|version|mix|remaster(?:ed)?'
_GENERIC = re.compile(r'^(?:original|extended|radio|club|dub|vocal|instrumental|'
                      r'album|single|studio|acoustic|live|official|unofficial|'
                      r'remix|bootleg|edit|flip|rework|reboot|vip|version|mix|'
                      r'remaster(?:ed)?)(?:\s+(?:mix|edit|version|vip|remix))*$', re.I)


def version_author_options(title):
    text = str(title or '')
    phrases = re.findall(r'[\(\[]([^()\[\]]+)[\)\]]', text)
    # Only an explicit dash may separate a bare version author from a song.
    if re.search(r'\s[-–—]\s', text):
        phrases.append(re.split(r'\s[-–—]\s', text)[-1])
    options, seen = [], set()
    for phrase in phrases:
        phrase = ' '.join(phrase.split())
        # An embedded featured singer is not part of the remixer's name.
        # Preserve the original title; isolate the author only for this field.
        original_phrase=phrase
        phrase=re.split(r'\s+(?:feat\.?|ft\.?|featuring)\s+',phrase,maxsplit=1,flags=re.I)[0]
        match = re.fullmatch(rf'(.+?)\s+((?:{_KINDS})(?:\s+(?:{_KINDS}))*)', phrase, re.I)
        custom = re.fullmatch(r'(busted|dusted)\s+by\s+(.+)', phrase, re.I)
        by = re.fullmatch(rf'({_KINDS})\s+by\s+(.+)', phrase, re.I)
        if custom or by:
            match_by = custom or by
            kind, author = match_by.group(1), match_by.group(2)
        elif match:
            author, kind = match.group(1), match.group(2)
        else:
            continue
        author = author.strip(' -–—')
        author = re.sub(r"['’]s$", '', author).strip()
        if not author or _GENERIC.fullmatch(author) or re.search(r'\b(?:feat\.?|ft\.?|featuring)\b', author, re.I):
            continue
        from core.naming import canonical_artist_name
        author = canonical_artist_name(author)
        key = author.casefold()
        if key in seen: continue
        seen.add(key)
        options.append({'author':author, 'kind':kind, 'caption':original_phrase})
    return options
