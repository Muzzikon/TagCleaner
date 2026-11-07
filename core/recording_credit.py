"""Whole-credit compatibility for edition selection, never artist overlap."""
from core.naming import split_feature_credit,extract_feature_entities_from_title
from core.matcher import split_artists,compact


def source_names(artist, title, known_entities=()):
    """Decode a slash list only when every member is independently named.

    A literal project name such as AC/DC stays atomic. A version caption can
    explain a redundant remixer, but cannot explain an unknown extra artist.
    """
    from core.version_authors import version_author_options
    authors=[n for o in version_author_options(title) for n in split_artists(o['author'])]
    known=[*known_entities,*authors]
    if '/' in artist and compact(artist) not in {compact(n) for n in known}:
        parts=[p.strip() for p in artist.split('/')]
        if all(p and compact(p) in {compact(n) for n in known} for p in parts):
            return parts
    return split_artists(artist,known_entities=known_entities)


def roles(artist,title,known_entities=()):
    primary,role,guest=split_feature_credit(artist)
    guests=split_artists(guest,known_entities=known_entities) if guest else []
    guests+=extract_feature_entities_from_title(title,known_entities=known_entities).get('artists') or []
    main=source_names(primary,title,known_entities)
    featured={compact(g) for g in guests}
    main=[n for n in main if compact(n) not in featured]
    return main,main+guests


def same_names(left,right):
    left=list({compact(n):n for n in left if compact(n)}.values())
    right=list({compact(n):n for n in right if compact(n)}.values())
    if not left or len(left)!=len(right):return False
    if {compact(n) for n in left}=={compact(n) for n in right}:return True
    from core.artist_aliases import same_artist
    remaining=list(right)
    for name in left:
        match=next((n for n in remaining if compact(name)==compact(n) or same_artist(name,n)),None)
        if match is None:return False
        remaining.remove(match)
    return True


def compatible(artist,title,other_artist,other_title,known_entities=()):
    main,all_names=roles(artist,title,known_entities)
    other_main,other_all=roles(other_artist,other_title,known_entities)
    # Missing explicit guests can be supplied for the same primary act.
    # A flat list with extra primary acts, or a guest alone, cannot establish it.
    if same_names(main,other_main) or same_names(all_names,other_all):return True
    from core.version_authors import version_author_options
    # A remix author repeated in Artist is not a new principal act. Require
    # the same explicit version author on both sides before excluding it.
    def authors(t):return {compact(n) for o in version_author_options(t) for n in split_artists(o['author'])}
    shared=authors(title)&authors(other_title)
    return bool(shared and same_names([n for n in all_names if compact(n) not in shared],
                                     [n for n in other_all if compact(n) not in shared]))
