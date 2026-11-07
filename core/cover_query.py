"""A search-only filename hint may bootstrap missing saved artist/title tags."""


def default_query(data):
    query = {key: str(data.get(key) or '') for key in ('artist', 'title', 'album')}
    if not query['artist'] or not query['title']:
        from core.filename_identity import proposal
        hint = proposal(data.get('file') or data.get('path') or '')
        if hint:
            query.update({key: hint['values'][key] for key in ('artist', 'title')})
    return query


def same_cover_track(artist, title, other_artist, other_title):
    from core.release_discovery import same_track

    artist, other_artist = artist or '', other_artist or ''
    if same_track(artist, title, other_artist, other_title):
        return True
    if '/' not in artist and '/' not in other_artist:
        return False

    from core.recording_credit import roles

    # A slash list can be decoded when its members are independently named
    # in the catalog credit or explicit feat. captions.
    known = [name for name in roles(artist, title)[1] + roles(other_artist, other_title)[1]
             if '/' not in name]
    return same_track(artist, title, other_artist, other_title, known_entities=known)
