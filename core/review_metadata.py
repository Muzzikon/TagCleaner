"""Read-only identity and artwork choices for a future library UI."""
from core.naming import artist_identity_records_from_items


def build_review_metadata(data, preferences):
    selection = data.get("release_context_selection") or {}
    selected = selection.get("selected") or {}
    recording = data.get("musicbrainz") or {}
    identities = artist_identity_records_from_items(
        selected.get("track_artist_credit_items") or recording.get("artist-credit") or [])
    artwork = []
    for context in data.get("release_contexts") or []:
        artwork.append({"kind": "release_artwork", "release_id": context.get("release_id"),
                        "release_group_id": context.get("release_group_id"),
                        "title": context.get("title"), "verification": "not_fetched"})
    # Individual track artwork need not imply a standalone single. Its
    # existence is unknown until a future artwork provider checks it.
    return {"artist_identities": identities,
            "artist_name_preference": preferences.get("artist_name_preference", "historical"),
            "local_artist": data.get("artist"),
            "release_selection": selection,
            "artwork": {"independent_of_album": True,
                        "preference": preferences.get("cover_preference", "ask"),
                        "selected_release_id": selected.get("release_id"),
                        "candidates": artwork,
                        "track_artwork": {"recording_id": recording.get("id"),
                                          "verification": "unknown", "implies_single": False}}}
