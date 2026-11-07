"""Version identity shared by audio, catalog and provenance validators.

Display titles are never rewritten here. Bare suffixes are retained for search;
only an explicit signature can establish that two versions agree.
"""
import re
import unicodedata
from core.text_cache import cached_text

_KINDS = r"remix|vip|edit|flip|bootleg|rework|reboot|tribute|demo|version|remaster(?:ed)?|instrumental|live|acoustic"
_WORD = re.compile(rf"\b({_KINDS})\b", re.I)
_CUSTOM = re.compile(r"\b(busted|dusted)\s+by\s+(.+)", re.I)
_FORM = re.compile(r"\b(extended|radio|vocal|dub|club|vip(?=\s+mix\b))\s+(?:mix|edit|version)\b", re.I)
_NAMED_MIX = re.compile(r"(.+?)\s+mix\s*$", re.I)


def _key(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Providers spell the same collaboration with '&' or 'and'. Preserve
    # every artist name; spelling of the conjunction is not version evidence.
    value = re.sub(r"\band\b", " ", value)
    # A collaboration join written as a spaced x/× has the same authors.
    # Keep x inside names such as xKore, and keep a standalone artist X.
    value = re.sub(r"(?<=\S)\s+[x×]\s+(?=\S)", " ", value)
    return " ".join(re.findall(r"\w+", value))


def _phrase(value, bare=False):
    text = str(value or "").strip()
    custom = _CUSTOM.fullmatch(text)
    if custom:
        kind = "remix" if custom.group(1).casefold() == "busted" else "flip"
        return {"kind": kind, "author": _key(custom.group(2)), "bare": bare}
    forms = {m.group(1).casefold() for m in _FORM.finditer(text)}
    remainder = _FORM.sub(" ", text)
    kinds = forms | {m.group(1).casefold() for m in _WORD.finditer(remainder)}
    kinds = {"remaster" if k == "remastered" else "rework" if k == "reboot" else k for k in kinds}
    if not kinds:
        named_mix = _NAMED_MIX.fullmatch(text)
        if not named_mix or named_mix.group(1).strip().casefold() == 'original':
            return None
        return {'kind': 'mix', 'author': _key(named_mix.group(1)), 'bare': bare}
    # 'bootleg remix' describes one remix; VIP is a distinct variant even
    # when it also says remix. Never let a common 'remix' hide that VIP.
    if "remix" in kinds:
        kinds.discard("bootleg")
    author = _WORD.sub(" ", remainder)
    author = re.sub(r"\b(?:mix|remixed|by)\b", " ", author, flags=re.I)
    return {"kind": "+".join(sorted(kinds)), "author": _key(author), "bare": bare}


def version_descriptors(title):
    # Callers may modify their returned dictionaries. Cache only immutable
    # triples, and return a fresh list/dicts on every public call.
    return [{"kind": kind, "author": author, "bare": bare}
            for kind, author, bare in _descriptor_tuples(str(title or ""))]


@cached_text
def _descriptor_tuples(text):
    result = []
    for match in re.finditer(r"[\(\[]([^()\[\]]+)[\)\]]", text):
        item = _phrase(match.group(1))
        if item:
            result.append(item)
    # Do not turn 'Title (X Remix)' into a second noisy suffix including
    # the title itself. A suffix outside the brackets is still significant.
    outside = re.sub(r"[\(\[][^()\[\]]*[\)\]]", " ", text).strip()
    custom = re.search(r"(?:^|\s|[-–—])((?:busted|dusted)\s+by\s+[^()\[\]]+)$", outside, re.I)
    if custom:
        item = _phrase(custom.group(1), bare=True)
        if item:
            result.append(item)
    elif (form := re.search(_FORM.pattern + r"\s*$", outside, re.I)):
        # Explicit mix length/form, not a remixer named after the base title.
        suffix = re.split(r"\s+[-–—]\s+", outside)[-1] if re.search(r"\s+[-–—]\s+", outside) else form.group(0)
        result.append(_phrase(suffix, bare=True))
    elif re.search(rf"\b(?:{_KINDS})\s*$", outside, re.I):
        # A dash explicitly separates base title from version author.
        suffix = re.split(r"\s+[-–—]\s+", outside)[-1]
        qualified = re.search(r"\b((?:ep|album|single|studio)\s+version)\s*$", suffix, re.I)
        if qualified:
            suffix = qualified.group(1)
        elif re.search(r"\b(?:vip|instrumental|demo|remaster(?:ed)?|version|live|acoustic)\s*$", suffix, re.I):
            # 'Casket VIP' has no remixer named Casket. For Remix/Edit/Flip
            # keep the preceding name and resolve the bare prefix pairwise.
            m = re.search(rf"\b({_KINDS})\s*$", suffix, re.I)
            suffix = m.group(1)
        item = _phrase(suffix, bare=True)
        if item:
            result.append(item)
    elif _NAMED_MIX.fullmatch(outside) and not re.search(r'\boriginal\s+mix\s*$', outside, re.I):
        suffix = re.split(r"\s+[-–—]\s+", outside)[-1]
        item = _phrase(suffix, bare=True)
        if item:
            result.append(item)
    unique = {}
    for item in result:
        unique.setdefault((item["kind"], item["author"]), item)
    return tuple((item["kind"], item["author"], item["bare"]) for item in unique.values())


def version_kinds(title):
    return {k for d in version_descriptors(title) for k in d["kind"].split("+")}


def version_base_title(title):
    """Remove explicit version formatting only, never ordinary subtitles.

    Callers must compare full signatures BEFORE using this base. Unseparated
    bare author-bearing Remix/Mix phrases cannot be stripped without guessing.
    """
    text = str(title or '')
    text = re.sub(r'[\(\[]([^()\[\]]+)[\)\]]',
                  lambda m: ' ' if _phrase(m.group(1)) else m.group(0), text)
    if re.search(r'\s+[-–—]\s+', text):
        head, suffix = re.split(r'\s+[-–—]\s+', text, maxsplit=1)
        descriptor = _phrase(suffix, bare=True)
        signatures = {(item['kind'], item['author']) for item in version_descriptors(title)}
        if descriptor and (descriptor['kind'], descriptor['author']) in signatures:
            text = head
    text = re.sub(r'\s+(?:vip(?:\s+(?:mix|edit|version))?|instrumental|demo|remaster(?:ed)?|'
                  r'(?:ep|album|single|studio)\s+version|(?:extended|radio|vocal|dub|club)\s+(?:mix|edit|version))\s*$',
                  '', text, flags=re.I)
    return ' '.join(text.split())


def _authors_match(left, right):
    a, b = left["author"], right["author"]
    if a == b:
        return True
    # A bare 'Flicker Mat Zo Remix' can agree with '(Mat Zo Remix)'.
    # Two bracketed authors must match in full; token overlap is insufficient.
    if a and b:
        if left["bare"] and not right["bare"]:
            return a.endswith(" " + b)
        if right["bare"] and not left["bare"]:
            return b.endswith(" " + a)
    return False


@cached_text
def compare_versions(left_title, right_title):
    left, right = version_descriptors(left_title), version_descriptors(right_title)
    if not left and not right:
        return 1.0, False
    if not left or not right:
        return 0.0, True
    def same(a, b):
        return a["kind"] == b["kind"] and _authors_match(a, b)
    matched = (all(any(same(a, b) for b in right) for a in left)
               and all(any(same(a, b) for a in left) for b in right))
    return (1.0, False) if matched else (0.0, True)


def local_version_conflict(local_titles, remote_title):
    """Explicit local evidence takes precedence over a tag missing its suffix."""
    titles = [t for t in local_titles if t]
    explicit = [t for t in titles if version_descriptors(t)]
    if explicit:
        return all(compare_versions(t, remote_title)[1] for t in explicit)
    return bool(titles and version_descriptors(remote_title))
