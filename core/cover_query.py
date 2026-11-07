"""A search-only filename hint may bootstrap missing saved artist/title tags."""


def default_query(data):
    query = {key: str(data.get(key) or '') for key in ('artist', 'title', 'album')}
    if not query['artist'] or not query['title']:
        from core.filename_identity import proposal
        hint = proposal(data.get('file') or data.get('path') or '')
        if hint:
            query.update({key: hint['values'][key] for key in ('artist', 'title')})
    return query
