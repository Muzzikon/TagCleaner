import json
from datetime import datetime
from pathlib import Path
from core.cache_utils import atomic_write_json
from core.review_metadata import build_review_metadata


def export_change_plan(results, reports_dir, *, preferences=None, write_results=None, review_decisions=None):
    reports_dir = Path(reports_dir)
    stamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
    path = reports_dir / f'TagCleaner_plan_{stamp}.json'
    files = []
    for data in results:
        plan = data.get('change_plan')
        if not plan:
            continue
        item = {'path': str(data.get('path') or ''), 'file': data.get('file') or '', **plan}
        item.update({
            'review_metadata': build_review_metadata(data, preferences or {}),
            'release_contexts': data.get('release_contexts') or [],
            'selected_release_context': data.get('selected_release_context'),
            'official_title': data.get('official_title'),
            'diagnostics': data.get('track_diagnostics') or [],
            'tag_findings': data.get('tag_findings') or [],
            'retry_required': bool(data.get('_checkpoint_retry_required')),
            'retry_reasons': data.get('_checkpoint_retry_reasons') or [],
            'analysis_file_signature': data.get('analysis_file_signature'),
            'evidence': {key: data.get(key) for key in (
                'best_match', 'ranked_candidates', 'recognition_provider', 'metadata_agreement',
                'action_safety', 'fallback_fusion', 'source_url_identity',
                'catalog_identity', 'acrcloud_identity', 'audd_musicbrainz', 'fingerprint_decode',
                'acoustid_multipass', 'shazam_identity', 'audio_timing',
                'duration', 'duration_header', 'duration_source', 'fingerprint_duration',
            )},
        })
        files.append(item)
    payload = {'schema_version': 2, 'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
               'mode': 'ANALYSIS_PLAN', 'preferences': preferences or {}, 'files': files,
               'write_results': write_results or [], 'review_decisions': review_decisions or []}
    atomic_write_json(path, payload)
    return path
