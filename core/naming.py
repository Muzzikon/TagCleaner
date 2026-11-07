import re


def clean_spaces(value):
    if not value:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value),
    ).strip()


ALBUM_SERIES_RULES = [
    (
        re.compile(
            r"^never\s+say\s+die"
            r"\s*[,:\-]?\s*"
            r"(?:vol\.?|volume)?\s*"
            r"(\d+)$",
            re.IGNORECASE,
        ),
        lambda match: (
            "Never Say Die, "
            f"Volume {int(match.group(1))}"
        ),
    ),

    (
        re.compile(
            r"^black\s+friday"
            r"\s*[,:\-]?\s*"
            r"(?:vol\.?|volume)\s*"
            r"(\d+)$",
            re.IGNORECASE,
        ),
        lambda match: (
            "Black Friday, "
            f"Vol. {int(match.group(1))}"
        ),
    ),
]


FEATURE_JOIN_RE = re.compile(
    r"\s+"
    r"(?:feat\.?|ft\.?|featuring)"
    r"\s+",
    re.IGNORECASE,
)

WITH_JOIN_RE = re.compile(
    r"\s+with\s+",
    re.IGNORECASE,
)

X_JOIN_RE = re.compile(
    r"\s+[x×]\s+",
    re.IGNORECASE,
)

AND_JOIN_RE = re.compile(
    r"\s+and\s+",
    re.IGNORECASE,
)

VS_JOIN_RE = re.compile(
    r"\s+vs\.?\s+",
    re.IGNORECASE,
)


VERSION_WORD_REPLACEMENTS = (
    (re.compile(r"\bremix\b", re.IGNORECASE), "Remix"),
    (re.compile(r"\bvip\b", re.IGNORECASE), "VIP"),
    (re.compile(r"\bedit\b", re.IGNORECASE), "Edit"),
    (re.compile(r"\bbootleg\b", re.IGNORECASE), "Bootleg"),
    (re.compile(r"\bflip\b", re.IGNORECASE), "Flip"),
    (re.compile(r"\brework\b", re.IGNORECASE), "Rework"),
    (re.compile(r"\breboot\b", re.IGNORECASE), "Reboot"),
    (re.compile(r"\binstrumental\b", re.IGNORECASE), "Instrumental"),
    (re.compile(r"\bremastered\b", re.IGNORECASE), "Remastered"),
    (re.compile(r"\bremaster\b", re.IGNORECASE), "Remaster"),
    (re.compile(r"\boriginal\s+mix\b", re.IGNORECASE), "Original Mix"),
    (re.compile(r"\bextended\s+mix\b", re.IGNORECASE), "Extended Mix"),
    (re.compile(r"\bextended\s+edit\b", re.IGNORECASE), "Extended Edit"),
    (re.compile(r"\bextended\s+version\b", re.IGNORECASE), "Extended Version"),
    (re.compile(r"\bradio\s+mix\b", re.IGNORECASE), "Radio Mix"),
    (re.compile(r"\bradio\s+edit\b", re.IGNORECASE), "Radio Edit"),
    (re.compile(r"\bradio\s+version\b", re.IGNORECASE), "Radio Version"),
    (re.compile(r"\bep\s+version\b", re.IGNORECASE), "EP Version"),
    (re.compile(r"\balbum\s+version\b", re.IGNORECASE), "Album Version"),
    (re.compile(r"\bvocal\s+mix\b", re.IGNORECASE), "Vocal Mix"),
    (re.compile(r"\bdub\s+mix\b", re.IGNORECASE), "Dub Mix"),
    (re.compile(r"\bclub\s+mix\b", re.IGNORECASE), "Club Mix"),
)

VERSION_HINT_RE = re.compile(
    r"\b(?:"
    r"remix|vip|edit|bootleg|flip|rework|reboot|instrumental|"
    r"remaster(?:ed)?|version|live|acoustic|original\s+mix|(?:extended|radio|vocal|dub|club)\s+(?:mix|edit|version)"
    r")\b|\b(?:dusted|busted)\s+by\b",
    re.IGNORECASE,
)


def _normalize_name_key(value):
    value = clean_spaces(value).casefold()
    value = value.replace("’", "'").replace("`", "'")
    value = re.sub(r"[^\w'!+]+", " ", value, flags=re.UNICODE)
    return clean_spaces(value)


def _split_unambiguous_credit(value):
    """Split only separators that are very likely collaboration markers.

    Plain ``and`` and commas are intentionally *not* split here because
    they may be part of a single artist name. They are normalized only when
    another representation of the same credit corroborates the split.
    """
    if not value:
        return []

    text = clean_spaces(value)
    text = FEATURE_JOIN_RE.sub(" | ", text)
    text = X_JOIN_RE.sub(" | ", text)
    text = re.sub(r"\s*&\s*", " | ", text)
    text = re.sub(r"\s*;\s*", " | ", text)

    return [
        part.strip()
        for part in text.split("|")
        if part.strip()
    ]


def _split_with_ambiguous(value, *, split_and=False, split_comma=False):
    if not value:
        return []

    text = clean_spaces(value)
    text = FEATURE_JOIN_RE.sub(" | ", text)
    text = X_JOIN_RE.sub(" | ", text)
    text = re.sub(r"\s*&\s*", " | ", text)
    text = re.sub(r"\s*;\s*", " | ", text)

    if split_and:
        text = AND_JOIN_RE.sub(" | ", text)

    if split_comma:
        text = re.sub(r"\s*,\s*", " | ", text)

    return [
        part.strip()
        for part in text.split("|")
        if part.strip()
    ]


def _parts_key(parts):
    return {
        _normalize_name_key(part)
        for part in parts
        if _normalize_name_key(part)
    }


def _ambiguous_join_is_corroborated(value, references, *, kind):
    """Whether replacing ``and``/comma is supported by another credit.

    This is what lets us safely turn
    ``WoodenToaster and Mic the Microphone`` into ``... & ...`` while not
    blindly rewriting every band name containing the English word "and".
    """
    if not value:
        return False

    candidate_parts = _split_with_ambiguous(
        value,
        split_and=(kind == "and"),
        split_comma=(kind == "comma"),
    )
    candidate_key = _parts_key(candidate_parts)

    if len(candidate_key) < 2:
        return False

    for reference in references or []:
        reference = clean_spaces(reference)
        if not reference:
            continue

        # The exact same text does not constitute independent evidence that
        # an ambiguous comma/"and" is a join phrase.
        if clean_spaces(reference).casefold() == clean_spaces(value).casefold():
            continue

        explicit_parts = _split_unambiguous_credit(reference)
        explicit_key = _parts_key(explicit_parts)

        if len(explicit_key) >= 2 and explicit_key == candidate_key:
            return True

        # A differently formatted reference may itself use comma/and but also
        # contain an explicit collaboration marker. In that case it is still
        # useful corroboration for multi-artist credit composition.
        has_explicit_join = bool(
            re.search(r"\s&\s|\s[x×]\s|\b(?:feat\.?|ft\.?|featuring)\b", reference, re.IGNORECASE)
        )
        if has_explicit_join:
            broad_key = _parts_key(
                _split_with_ambiguous(
                    reference,
                    split_and=True,
                    split_comma=True,
                )
            )
            if broad_key == candidate_key:
                return True

    return False


def canonical_artist_credit(value, references=None):
    """Apple-like final formatting for an already identified Artist credit.

    Recognition is deliberately more permissive elsewhere in TagCleaner.
    This function is only for the value that is going to be displayed/written.

    Canonical style v1:
      * equal collaborators: ``A & B``;
      * featured artists: ``A feat. B``;
      * ``x``/``×`` -> ``&``;
      * ``ft.``/``featuring`` -> ``feat.``;
      * ambiguous ``and``/comma are rewritten only when another credit
        representation corroborates the split.

    Artist-name capitalization/stylization is never Title-Cased here.
    """
    from core.verified_spellings import artist_credit
    text = artist_credit(clean_spaces(value))
    if not text:
        return ""

    references = [
        clean_spaces(item)
        for item in (references or [])
        if clean_spaces(item)
    ]

    text = FEATURE_JOIN_RE.sub(" feat. ", text)
    text = X_JOIN_RE.sub(" & ", text)
    text = re.sub(r"\s*&\s*", " & ", text)
    text = re.sub(r"\s*;\s*", " & ", text)

    # Preserve semantic "vs." instead of flattening it to a collaboration.
    text = VS_JOIN_RE.sub(" vs. ", text)

    if AND_JOIN_RE.search(text) and _ambiguous_join_is_corroborated(
        text,
        references,
        kind="and",
    ):
        text = AND_JOIN_RE.sub(" & ", text)

    if "," in text and _ambiguous_join_is_corroborated(
        text,
        references,
        kind="comma",
    ):
        text = re.sub(r"\s*,\s*", " & ", text)

    # Do not allow a mixture like "A & B & C feat. D, E" when comma is
    # clearly inside the featured part and another reference confirms the
    # same artists.
    text = re.sub(r"\s*&\s*", " & ", text)
    text = re.sub(r"\s+feat\.\s+", " feat. ", text, flags=re.IGNORECASE)

    return clean_spaces(text)


def _canonical_joinphrase(joinphrase):
    text = clean_spaces(joinphrase)
    if not text:
        return ""

    if re.search(r"\b(?:feat\.?|ft\.?|featuring)\b", text, re.IGNORECASE):
        return " feat. "

    if re.search(r"\bwith\b", text, re.IGNORECASE):
        return " with "

    if re.search(r"\bvs\.?\b", text, re.IGNORECASE):
        return " vs. "

    return " & "


def _contains_internal_ampersand(name):
    return bool(re.search(r"\s&\s", clean_spaces(name)))


def canonical_entity_list(names):
    """Render already-separated artist entities into a readable flat tag.

    Apple/Spotify keep artist entities structurally separate. A local ID3
    Artist field is usually just one display string, so we serialize it with
    familiar punctuation while never splitting an entity such as
    ``Dodge & Fuski``.

    * 1 entity: ``A``
    * 2 simple entities: ``A & B``
    * 3+ simple entities: ``A, B & C``
    * if an entity itself contains `` & ``, use commas between top-level
      entities to avoid ``Dodge & Fuski & PhaseOne`` ambiguity.
    """
    values = [canonical_artist_name(x) for x in (names or []) if clean_spaces(x)]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]

    if len(values) == 2:
        # If one entity already contains an ampersand, another top-level
        # ampersand becomes ambiguous: ``Dodge & Fuski & PhaseOne``.
        # A comma clearly separates the two artist entities.
        if any(_contains_internal_ampersand(x) for x in values):
            return ", ".join(values)
        return f"{values[0]} & {values[1]}"

    # Normal mainstream list punctuation for 3+ entities.  If one of the
    # final two entities itself contains ``&``, using another final ampersand
    # is ambiguous, so fall back to commas for the top-level list.
    if any(_contains_internal_ampersand(x) for x in values[-2:]):
        return ", ".join(values)

    return ", ".join(values[:-1]) + " & " + values[-1]


def artist_roles_from_items(artist_credit, *, unique_entities=False):
    """Return structured primary/featured roles from MusicBrainz artist-credit.

    Each artist-credit item is already an artist entity, so names such as
    ``Dodge & Fuski`` remain atomic.  The joinphrase *after* an item tells us
    how the following artist is related to the current credit.
    """
    if not artist_credit:
        return {
            "primary": [],
            "featured": [],
            "feature_role": "",
        }

    primary = []
    featured = []
    feature_role = ""
    mode = "primary"
    seen = set()

    for item in artist_credit:
        if isinstance(item, str):
            name = clean_spaces(item)
            joinphrase = ""
        elif isinstance(item, dict):
            name = clean_spaces(
                item.get("name")
                or (item.get("artist") or {}).get("name")
                or ""
            )
            joinphrase = clean_spaces(item.get("joinphrase") or "")
        else:
            continue

        artist_id = ((item.get('artist') or {}).get('id') or '') if isinstance(item,dict) else ''
        entity = ('id',artist_id) if artist_id else ('name',name.casefold())
        if name and (not unique_entities or entity not in seen):
            seen.add(entity)
            if mode == "featured":
                featured.append(name)
            else:
                primary.append(name)

        if re.search(r"\b(?:feat\.?|ft\.?|featuring)\b", joinphrase, re.IGNORECASE):
            feature_role = "feat."
            mode = "featured"
        elif re.search(r"\bwith\b", joinphrase, re.IGNORECASE):
            feature_role = "with"
            mode = "featured"

    return {
        "primary": primary,
        "featured": featured,
        "feature_role": feature_role,
    }


def split_credit_entities(value, references=None):
    """Best-effort split of a *flat* credit for write-side role inference.

    This is not used for recognition.  It is only a fallback when no
    structured artist-credit exists.  ``references`` may safely corroborate
    ambiguous ``and``/comma joins before splitting.
    """
    text = canonical_artist_credit(value, references=references)
    if not text:
        return []

    text = re.sub(r"\s+(?:feat\.?|with)\s+", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+[x×]\s+", " | ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*&\s*", " | ", text)
    text = re.sub(r"\s*,\s*", " | ", text)
    text = re.sub(r"\s*;\s*", " | ", text)

    return [clean_spaces(part) for part in text.split("|") if clean_spaces(part)]


def extract_feature_entities_from_title(value, *, known_entities=()):
    """Extract explicit ``feat.``/``with`` artists from a display title."""
    text = clean_spaces(value)
    if not text:
        return {"role": "", "artists": []}

    match = re.search(
        r"(?:(?:\(|\[)\s*(feat\.?|ft\.?|featuring|with)|"
        r"(?:^|\s)(feat\.?|ft\.?|featuring))\s+([^\)\]\[]+)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return {"role": "", "artists": []}

    role_raw = (match.group(1) or match.group(2)).casefold()
    role = "with" if role_raw == "with" else "feat."
    credit = clean_spaces(match.group(3))

    # A complete source title can be "Artist ft. Guest - Song (X Remix)".
    # The song is not part of Guest's name. Never consume arbitrary words
    # before a version keyword: that truncated "Maestro Chives" to "Maestro".
    credit = re.split(r"\s+[-–—]\s+", credit, maxsplit=1)[0].strip()
    if match.group(2) and '|' in credit:
        # Bare upload captions often continue as "feat. Guest | GENRE |
        # Remixer Remix". Those suffixes are not corroborated artist entities.
        # Preserve the raw caption as evidence instead of inventing guests.
        return {'role': '', 'artists': [], 'ambiguous': True, 'raw_credit': credit}
    credit = re.sub(
        r"\s+(?:remix|vip|edit|bootleg|flip|rework)\s*$", "", credit,
        flags=re.IGNORECASE,
    ).strip()

    if known_entities:
        from core.matcher import split_artists
        artists = split_artists(credit, known_entities=known_entities)
    else:
        artists = split_credit_entities(credit)
    return {"role": role, "artists": artists}


def extract_remixer_entities(value):
    """Extract names from explicit ``X Remix`` version references."""
    text = clean_spaces(value)
    if not text:
        return []

    phrases = []
    for inner in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", text):
        if re.search(r"\b(?:remix|reboot|rework|flip)\b|\b(?:busted|dusted)\s+by\b", inner, re.IGNORECASE):
            phrases.append(inner)

    # Also support a bare trailing ``- X Remix`` form.
    bare = re.search(r"[-–—]\s*([^\[\]()]+?)\s+Remix\s*$", text, re.IGNORECASE)
    if bare:
        phrases.append(bare.group(1) + " Remix")

    result = []
    seen = set()
    for phrase in phrases:
        name_text = re.sub(
            r"\b(?:(?:busted|dusted)\s+by|remix(?:ed)?(?:\s+by)?|reboot|rework|flip|mix)\b",
            "",
            phrase,
            flags=re.IGNORECASE,
        )
        name_text = clean_spaces(name_text.strip(" -–—"))
        for name in split_credit_entities(name_text):
            key = _normalize_name_key(name)
            if key and key not in seen:
                seen.add(key)
                result.append(name)

    return result


def split_feature_credit(value):
    """Return (primary, role, featured) from a flat artist credit."""
    text = clean_spaces(value)
    if not text:
        return "", "", ""

    m = re.search(
        r"\s+(feat\.?|ft\.?|featuring|with)\s+",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        return text, "", ""

    role_raw = m.group(1).casefold()
    role = "with" if role_raw == "with" else "feat."
    primary = clean_spaces(text[:m.start()])
    featured = clean_spaces(text[m.end():])
    return primary, role, featured


def canonical_artist_credit_from_items(artist_credit, *, unique_entities=False):
    """Canonicalize a structured MusicBrainz-style artist-credit list.

    Every list item is an artist entity.  An entity such as ``Dodge & Fuski``
    therefore stays intact even when another primary artist follows it.
    """
    roles = artist_roles_from_items(artist_credit,unique_entities=unique_entities)
    primary_text = canonical_entity_list(roles.get("primary"))
    featured_text = canonical_entity_list(roles.get("featured"))

    if featured_text:
        role = roles.get("feature_role") or "feat."
        return clean_spaces(f"{primary_text} {role} {featured_text}")

    return primary_text



def artist_identity_records_from_items(artist_credit):
    """Return stable artist identities plus release-specific credited names.

    MusicBrainz artist-credit items can contain both:
      * ``artist.id`` / ``artist.name``: the stable entity and its current name;
      * ``name``: how that same entity is credited on this recording/release.

    Keeping both is what lets TagCleaner preserve historical aliases and
    stylisation in file tags while still making every alias searchable as the
    same artist identity later.
    """
    from core.artist_aliases import register_credit
    register_credit(artist_credit)
    result = []
    seen = set()

    for item in artist_credit or []:
        if isinstance(item, str):
            credited = clean_spaces(item)
            canonical = credited
            artist_id = ""
        elif isinstance(item, dict):
            artist = item.get("artist") or {}
            credited = clean_spaces(
                item.get("name")
                or artist.get("name")
                or ""
            )
            canonical = clean_spaces(
                artist.get("name")
                or credited
            )
            artist_id = clean_spaces(artist.get("id") or "")
        else:
            continue

        if not credited and not canonical:
            continue

        key = artist_id or _normalize_name_key(credited or canonical)
        if key in seen:
            continue
        seen.add(key)

        aliases = []
        for value in (credited, canonical):
            if value and _normalize_name_key(value) not in {
                _normalize_name_key(x) for x in aliases
            }:
                aliases.append(value)

        result.append({
            "id": artist_id,
            "credited_name": credited or canonical,
            "canonical_name": canonical or credited,
            "aliases": aliases,
        })

    return result


def normalize_artist_search_key(value):
    """Case-insensitive key for future alias-aware library search.

    Display casing is deliberately *not* changed by this helper.  ``SKisM``,
    ``oddprophet`` and ``heRobust`` remain exactly as credited when written,
    while user search can match them case-insensitively.
    """
    return _normalize_name_key(value)


def artist_identity_matches_query(identity_record, query):
    query_key = normalize_artist_search_key(query)
    if not query_key:
        return False

    for value in (identity_record or {}).get("aliases") or []:
        if normalize_artist_search_key(value) == query_key:
            return True

    return False

def canonical_album_name(value):
    value = clean_spaces(value)

    if not value:
        return ""

    for pattern, formatter in ALBUM_SERIES_RULES:
        match = pattern.fullmatch(value)

        if match:
            return formatter(match)

    return value


def canonical_artist_name(value):
    """Whitespace and explicitly verified spellings, never automatic Title Case."""
    from core.verified_spellings import artist_credit
    return artist_credit(clean_spaces(value))


def _collapse_duplicate_brackets(value):
    previous = None
    text = str(value or "")

    while previous != text:
        previous = text
        text = re.sub(r"\(\(([^()]*)\)\)", r"(\1)", text)
        text = re.sub(r"\[\[([^\[\]]*)\]\]", r"[\1]", text)

    return text


def _version_square_brackets_to_parentheses(value):
    def repl(match):
        inner = clean_spaces(match.group(1))
        if VERSION_HINT_RE.search(inner):
            return f"({inner})"
        return match.group(0)

    return re.sub(r"\[([^\[\]]+)\]", repl, value)


def _remove_trailing_original_mix(value):
    text = clean_spaces(value)
    if not text:
        return ""

    # Apple Music treats the standard/original recording as the base title;
    # "Original Mix" is redundant version information for that case.
    patterns = (
        r"\s*\(\s*Original\s+Mix\s*\)\s*$",
        r"\s*\[\s*Original\s+Mix\s*\]\s*$",
        r"\s*[-–—]\s*Original\s+Mix\s*$",
    )
    for pattern in patterns:
        candidate = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()
        if candidate != text and candidate:
            return candidate
    return text


def canonical_track_title(value):
    """Conservative Apple-like final formatting for track titles.

    Version identity is not invented here. Recognition decides whether a
    Remix/VIP/Edit/etc. is the correct recording. This layer only formats the
    chosen identity and removes redundant ``Original Mix`` from the standard
    version title.
    """
    from core.verified_spellings import title_credit
    text = title_credit(clean_spaces(value))
    if not text:
        return ""

    text = _collapse_duplicate_brackets(text)
    text = re.sub(
        r"(?<!\w)(?:ft|feat|featuring)\.?(?=\s)",
        "feat.",
        text,
        flags=re.IGNORECASE,
    )

    for pattern, replacement in VERSION_WORD_REPLACEMENTS:
        text = pattern.sub(replacement, text)

    # Collapse only adjacent, identical version blocks. Arbitrary repeated
    # parentheses may be part of the song title and must remain intact.
    while True:
        blocks = list(re.finditer(r"\([^()]+\)|\[[^\[\]]+\]", text))
        for first, second in zip(blocks, blocks[1:]):
            a, b = clean_spaces(first.group(0)[1:-1]), clean_spaces(second.group(0)[1:-1])
            if (not text[first.end():second.start()].strip() and a.casefold() == b.casefold()
                    and VERSION_HINT_RE.search(a)):
                text = text[:first.end()] + text[second.end():]
                break
        else:
            break

    # Streaming/catalog providers often spell a trailing version as
    # ``Track - Artist Remix``.  For our canonical display style this is the
    # same version identity as ``Track (Artist Remix)``.  Only touch a true
    # trailing version suffix; ordinary hyphens inside the song title remain
    # untouched.
    dash_version = re.fullmatch(
        r"(.+?)\s+[-–—]\s+([^\[\]()]*?(?:Remix|Edit|VIP|Bootleg|Flip|Rework|Instrumental|Remaster(?:ed)?|Original Mix|(?:Extended|Radio)\s+(?:Mix|Edit|Version)))",
        text,
        flags=re.IGNORECASE,
    )
    if dash_version and VERSION_HINT_RE.search(dash_version.group(2)):
        base = clean_spaces(dash_version.group(1))
        version = clean_spaces(dash_version.group(2))
        text = f"{base} ({version})"

    text = _remove_trailing_original_mix(text)

    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\]", "]", text)
    text = re.sub(r"\[\s+", "[", text)

    return clean_spaces(text)



def _strip_duplicate_feature_from_version(version_text, featured):
    """Remove a duplicate guest from a secondary version block.

    This helper is used only after the same guest was already written as a
    separate leading feature credit (for example ``Song feat. Guest
    (A Remix feat. Guest)``).  A feature that is intentionally embedded in
    the *official/chosen* version title is preserved by
    ``ensure_feature_credit_in_title`` before this helper can run.
    """
    text = clean_spaces(version_text)
    featured = clean_spaces(featured)
    if not text or not featured:
        return text

    pattern = re.compile(
        r"\s+(?:feat\.?|ft\.?|featuring|with)\s+"
        + re.escape(featured)
        + r"\s*$",
        re.IGNORECASE,
    )
    return clean_spaces(pattern.sub("", text))


def ensure_feature_credit_in_title(title, role, featured):
    """Place featured/with credit in the title using Apple-like punctuation.

    Bare source strings such as ``Song feat. Guest`` are normalized instead
    of producing ``Song feat. Guest (feat. Guest)``.
    """
    text = canonical_track_title(title)
    featured = clean_spaces(featured)
    role = "with" if role == "with" else "feat."
    if not text or not featured:
        return text

    credit = f"{role} {featured}"

    # If the *official/chosen* version suffix itself contains the feature
    # credit, preserve that title structure.  Some releases intentionally use
    # forms such as ``Lacrimosa (Code:Pandorum x TenGraphs Remix feat. Qoiet)``.
    # Rewriting it to ``Lacrimosa (feat. Qoiet) [.. Remix]`` would change an
    # official release title rather than merely normalize punctuation.
    trailing_version = re.search(r"\s*[\(\[]([^\)\]]+)[\)\]]\s*$", text)
    if trailing_version and VERSION_HINT_RE.search(trailing_version.group(1)):
        embedded_feature = re.search(
            r"\b(?:feat\.?|ft\.?|featuring|with)\s+(.+?)\s*$",
            trailing_version.group(1),
            flags=re.IGNORECASE,
        )
        if embedded_feature:
            embedded_name = _normalize_name_key(embedded_feature.group(1))
            resolved_name = _normalize_name_key(featured)
            if embedded_name and embedded_name == resolved_name:
                return text

    # Already parenthesized/bracketed: do not duplicate.
    if re.search(r"[\(\[]\s*(?:feat\.|with)\s+", text, re.IGNORECASE):
        return text

    # Normalize an existing bare feature phrase.  We use the already resolved
    # ``featured`` value, so this is write-side formatting rather than a new
    # identity inference.
    bare_pattern = re.compile(
        r"\s+(?:feat\.?|ft\.?|featuring|with)\s+"
        + re.escape(featured)
        + r"(?=\s*(?:\(|\[|$))",
        re.IGNORECASE,
    )
    bare_match = bare_pattern.search(text)
    if bare_match:
        base = text[:bare_match.start()].rstrip()
        suffix = text[bare_match.end():].strip()
        if not suffix:
            return f"{base} ({credit})"

        # A following version reference becomes the second explanatory
        # reference in square brackets.
        m = re.fullmatch(r"\(([^()]*)\)", suffix)
        if m and VERSION_HINT_RE.search(m.group(1)):
            version = _strip_duplicate_feature_from_version(m.group(1), featured)
            return f"{base} ({credit}) [{version}]" if version else f"{base} ({credit})"
        m = re.fullmatch(r"\[([^\[\]]*)\]", suffix)
        if m:
            version = _strip_duplicate_feature_from_version(m.group(1), featured)
            return f"{base} ({credit}) [{version}]" if version else f"{base} ({credit})"
        return f"{base} ({credit}) {suffix}"

    # Existing square-bracket version: feature credit becomes the first
    # explanatory reference in parentheses.
    m = re.search(r"\s*(\[[^\[\]]+\])\s*$", text)
    if m:
        base = text[:m.start()].rstrip()
        return f"{base} ({credit}) {m.group(1)}"

    # Existing trailing parentheses. If it is a version marker, Apple style
    # places feat. first and the second reference in square brackets. If it is
    # part of the actual title (e.g. Part III), add feat. in brackets.
    m = re.search(r"\s*\(([^()]*)\)\s*$", text)
    if m:
        base = text[:m.start()].rstrip()
        inner = clean_spaces(m.group(1))
        if VERSION_HINT_RE.search(inner):
            version = _strip_duplicate_feature_from_version(inner, featured)
            return f"{base} ({credit}) [{version}]" if version else f"{base} ({credit})"
        return f"{text} [{credit}]"

    return f"{text} ({credit})"


def canonical_release_group(group):
    result = group.copy()

    result["title"] = canonical_album_name(
        result.get("title")
    )

    return result


def deduplicate_release_groups(groups):
    """Remove duplicate release groups after album-name normalization."""
    result = []
    seen = set()

    for group in groups:
        normalized = canonical_release_group(group)

        key = (
            normalized.get("title", "").casefold(),
            (normalized.get("type") or "").casefold(),
        )

        if key in seen:
            continue

        seen.add(key)
        result.append(normalized)

    return result
