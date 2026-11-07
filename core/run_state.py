from __future__ import annotations

import json
import os
import pickle
import sqlite3
import threading
import time
import uuid
import zlib
from pathlib import Path

from core.cache_utils import cache_ttl_days, file_signature, stable_digest


ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "cache" / "run_state.sqlite3"

# Schema 2 supports semantic decision keys. V13 changes role/version decisions,
# so final V11/V12 plans must be rebuilt while raw provider caches remain usable.
SCHEMA_VERSION = 2
DECISION_COMPAT_VERSION = "artist-identity-v26-verified-name-recheck"
# Atomic artist parsing changed. Reevaluate stages
# from raw responses; raw fingerprint/provider caches remain untouched.
STAGE_COMPAT_VERSION = "artist-identity-v13.24"
# A write-plan correction must not forget the user's durable fresh-Shazam
# generation. Keep the V13.25 check scope while invalidating completed plans.
CHECK_GENERATION_COMPAT_VERSION = "artist-identity-v13.24"
LEGACY_V11_SCHEMA_VERSION = 1


def _resume_max_age_days() -> float:
    raw = (os.getenv("TAGCLEANER_RESUME_DAYS") or "7").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 7.0


_lock = threading.RLock()
_connection = None
_active_generations = {}


def _canonical_path(file_path):
    return os.path.normcase(str(Path(file_path).resolve()))


def activate_checkpoint_run(files, preferences, *, recheck=False, refresh_shazam=False):
    """Persist a fresh decision generation before starting any workers.

    Generations are per file and preference/semantic context. A reordered,
    extended or smaller selection can therefore resume the same recheck.
    Raw fingerprints and ordinary provider caches remain independent. An
    explicit Shazam refresh carries its policy in the durable generation.
    """
    global _active_generations
    prefs = _preferences_signature(preferences)
    pipeline = _decision_signature(CHECK_GENERATION_COMPAT_VERSION)
    paths = sorted({_canonical_path(path) for path in files})
    scopes = {path: stable_digest({'path': path, 'preferences': prefs, 'pipeline': pipeline}) for path in paths}
    generation = (('shazam-refresh-' if refresh_shazam else '') + uuid.uuid4().hex) if recheck else None
    conn = _connect()
    with _lock:
        with conn:
            conn.execute('CREATE TABLE IF NOT EXISTS checkpoint_generations '
                         '(scope_key TEXT PRIMARY KEY, generation TEXT NOT NULL, updated_at REAL NOT NULL)')
            if recheck:
                conn.executemany('INSERT OR REPLACE INTO checkpoint_generations VALUES (?, ?, ?)',
                                 [(scope, generation, time.time()) for scope in scopes.values()])
            stored = dict(conn.execute('SELECT scope_key, generation FROM checkpoint_generations'))
        # Publish only after the durable transaction succeeds. A failure
        # must not silently run a requested recheck against old decisions.
        _active_generations = {(path, prefs): stored[scope] for path, scope in scopes.items() if scope in stored}
    return {'mode': ('recheck' if refresh_shazam else 'recheck_cached') if recheck else 'resume', 'generation': generation,
            'files': len(paths), 'files_with_recheck_history': len(_active_generations),
            'shazam_refresh_files': sum(bool(shazam_refresh_generation(path, preferences)) for path in paths)}


def shazam_refresh_generation(file_path, preferences):
    """Return the same fresh-response scope after a resumed/crashed check.

    Older rechecks have an unprefixed UUID and retain their original policy.
    A new refresh cannot see replies from an older generation; continuing it
    can reuse the replies already obtained in that exact check.
    """
    generation = _active_generations.get((_canonical_path(file_path), _preferences_signature(preferences)))
    return generation if generation and generation.startswith('shazam-refresh-') else None


def _connect():
    global _connection
    with _lock:
        if _connection is not None:
            return _connection

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute('CREATE TABLE IF NOT EXISTS latest_checkpoint_keys (slot TEXT PRIMARY KEY, cache_key TEXT NOT NULL)')
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS completed (
                cache_key TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                pipeline_signature TEXT NOT NULL,
                preferences_signature TEXT NOT NULL,
                file_signature TEXT NOT NULL,
                payload BLOB NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()
        _connection = conn
        return conn


def pipeline_signature() -> str:
    """Decision-compatibility signature, not a performance-code hash.

    V11 tied checkpoints to every Python file, which meant a harmless scheduler
    optimization invalidated hours of finished work. Starting with V12 we key
    completed decisions to an explicit semantic version instead. Any future
    patch that changes recognition/SAFE/naming/release decisions MUST bump
    DECISION_COMPAT_VERSION. Performance/cache/output-only patches must not.
    """
    return _decision_signature(DECISION_COMPAT_VERSION)


def _decision_signature(version):
    from core.shazam_client import shazam_capability
    from core.audd_cache import audd_network_enabled
    signature = {'decision_compat': version, 'shazam': shazam_capability()}
    # Preserve the old namespace when AudD is explicitly enabled again.
    # Disabled mode must rebuild plans once so quota failures cannot linger.
    if not audd_network_enabled():
        signature['audd_policy'] = 'positive-cache-only-v1'
    from core.extra_catalogs import checkpoint_signature
    extras = checkpoint_signature()
    if extras:
        signature['extra_catalogs_v1'] = extras
    return stable_digest(signature)


# Keep the small cache_clear API used by the V11 test/reset helper.
pipeline_signature.cache_clear = lambda: None  # type: ignore[attr-defined]


def _preferences_signature(preferences) -> str:
    return stable_digest(preferences or {})


def _file_signature_text(file_path) -> str:
    return json.dumps(
        file_signature(file_path),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _cache_key(file_path, preferences, *, decision_version=None) -> tuple[str, str, str, str]:
    pipeline = (pipeline_signature() if decision_version is None
                else _decision_signature(decision_version))
    prefs = _preferences_signature(preferences)
    file_sig = _file_signature_text(file_path)
    context = {
        "schema": SCHEMA_VERSION,
        "pipeline": pipeline,
        "preferences": prefs,
        "file": file_sig,
    }
    generation = _active_generations.get((_canonical_path(file_path), prefs))
    if generation:
        context['generation'] = generation
    key = stable_digest(context)
    return key, pipeline, prefs, file_sig


def _deserialize_payload(blob):
    try:
        payload = pickle.loads(zlib.decompress(blob))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _payload_needs_retry(payload) -> bool:
    """Reject only checkpoints that are known to be incomplete.

    V12 could save a completed row after a transient MusicBrainz failure and
    could also hit an UnboundLocalError before ReleaseContext ran. V12.1 keeps
    every healthy V11/V12 checkpoint, but makes those incomplete rows eligible
    for reprocessing.
    """
    if not isinstance(payload, dict):
        return True

    if payload.get("_checkpoint_retry_required"):
        return True
    if any(not _stage_result_reusable(payload.get(key)) for key in (
            'recognition', 'shazam_identity', 'acoustid_multipass', 'acrcloud_identity',
            'catalog_identity', 'source_url_identity', 'acrcloud_musicbrainz',
            'audd_musicbrainz', 'secondary_recognition')):
        return True

    best_match = payload.get("best_match") or {}
    candidate = best_match.get("candidate") or {}
    recording_id = candidate.get("recording_id") or best_match.get("recording_id")

    if not recording_id:
        return False

    confidence = str(payload.get("confidence") or "").upper()
    if confidence and confidence != "LOW" and payload.get("musicbrainz") is None:
        return True

    safety = str(payload.get("action_safety") or "").upper()
    if safety and safety != "REJECT":
        selection = payload.get("release_context_selection")
        if isinstance(selection, dict):
            reason = str(selection.get("reason") or "")
            if reason == "ReleaseContext v2 не запускался.":
                return True
        elif "release_contexts" in payload:
            return True

    return False


def _legacy_v11_row(file_sig: str, prefs: str, cutoff: float):
    """Return the newest compatible V11 checkpoint for this exact file/prefs.

    Schema 1 only ever existed in V11. V12 did not change decision semantics,
    therefore importing it is safe. We do *not* accept arbitrary future schema
    versions or a file/preferences mismatch.
    """
    conn = _connect()
    with _lock:
        return conn.execute(
            "SELECT payload FROM completed "
            "WHERE schema_version=? AND preferences_signature=? "
            "AND file_signature=? AND updated_at>=? "
            "ORDER BY updated_at DESC LIMIT 1",
            (LEGACY_V11_SCHEMA_VERSION, prefs, file_sig, cutoff),
        ).fetchone()


def completed_result_exists(file_path, preferences) -> bool:
    return load_completed_result(file_path, preferences) is not None


def load_completed_result(file_path, preferences):
    try:
        key, pipeline, prefs, file_sig = _cache_key(file_path, preferences)
        conn = _connect()
        cutoff = time.time() - _resume_max_age_days() * 86400.0

        with _lock:
            row = conn.execute(
                "SELECT payload, updated_at FROM completed WHERE cache_key=? AND schema_version=? "
                "AND pipeline_signature=? AND preferences_signature=? "
                "AND file_signature=? AND updated_at>=?",
                (key, SCHEMA_VERSION, pipeline, prefs, file_sig, cutoff),
            ).fetchone()

        if row:
            payload = _deserialize_payload(row[0])
            # A candidate does not make other providers' negative evidence
            # permanent. Recheck expired misses even inside SAFE/REVIEW plans.
            negative_days = cache_ttl_days('TAGCLEANER_NEGATIVE_CACHE_DAYS', 1)
            if (payload is not None and _stage_has_negative_result(payload)
                    and negative_days is not None and time.time() - row[1] > negative_days * 86400):
                return None
            if payload is not None and not _payload_needs_retry(payload):
                return payload

        # Seamless V11 -> V12 resume. The user's existing run_state.sqlite3 is
        # left in place; completed V11 files are imported lazily as V12 touches
        # them, then re-saved under the V12 semantic key.
        # V13 fixes role/version decisions. V11/V12 final plans must not be
        # imported under the new semantic key. Raw provider caches remain valid.
        if DECISION_COMPAT_VERSION != "artist-identity-v10":
            return None
        legacy = _legacy_v11_row(file_sig, prefs, cutoff)
        if not legacy:
            return None

        payload = _deserialize_payload(legacy[0])
        if payload is None or _payload_needs_retry(payload):
            return None

        try:
            save_completed_result(file_path, preferences, payload)
        except Exception:
            # Migration failure must never destroy a usable legacy checkpoint.
            pass

        return payload
    except Exception:
        return None


def save_completed_result(file_path, preferences, data) -> None:
    key, pipeline, prefs, file_sig = _cache_key(file_path, preferences)
    blob = zlib.compress(
        pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL),
        level=1,
    )
    conn = _connect()
    with _lock:
        _replace_obsolete_key(conn,file_path,preferences,pipeline,key,'completed')
        conn.execute(
            """
            INSERT INTO completed (
                cache_key, schema_version, pipeline_signature,
                preferences_signature, file_signature, payload, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (
                key,
                SCHEMA_VERSION,
                pipeline,
                prefs,
                file_sig,
                sqlite3.Binary(blob),
                time.time(),
            ),
        )
        conn.commit()


def _replace_obsolete_key(conn,file_path,preferences,pipeline,key,table,stage=''):
    """One current generation per file/context; stale generations cannot resume."""
    slot=stable_digest({'path':_canonical_path(file_path),'preferences':preferences,'pipeline':pipeline,'table':table,'stage':stage})
    try:
        previous=conn.execute('SELECT cache_key FROM latest_checkpoint_keys WHERE slot=?',(slot,)).fetchone()
    except sqlite3.OperationalError as error:
        if 'no such table' not in str(error):raise
        conn.execute('CREATE TABLE IF NOT EXISTS latest_checkpoint_keys (slot TEXT PRIMARY KEY, cache_key TEXT NOT NULL)')
        previous=None
    if previous and previous[0]!=key:
        if table=='completed':conn.execute('DELETE FROM completed WHERE cache_key=?',(previous[0],))
        elif table=='stages':conn.execute('DELETE FROM stages WHERE cache_key=? AND stage=?',(previous[0],stage))
    conn.execute('INSERT OR REPLACE INTO latest_checkpoint_keys VALUES (?,?)',(slot,key))


def prune_old_checkpoints(max_age_days: float = 45.0) -> int:
    try:
        cutoff = time.time() - max(0.0, float(max_age_days)) * 86400.0
        conn = _connect()
        with _lock:
            cursor = conn.execute("DELETE FROM completed WHERE updated_at < ?", (cutoff,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='stages'").fetchone():
                conn.execute('DELETE FROM stages WHERE updated_at<?',(cutoff,))
            conn.commit()
            return int(cursor.rowcount or 0)
    except Exception:
        return 0


def _stage_result_reusable(value):
    """Never freeze API/network failures as a completed search stage."""
    if isinstance(value, dict):
        status = value.get('status')
        # ACRCloud embeds raw.status={code, msg, version}. It is metadata,
        # not our string status enum, and must never be hashed into a set.
        if isinstance(status, str) and status in {'ERROR', 'FALLBACK_ERROR'}:
            return False
        if value.get('pipeline_status') == 'PLAN_ERROR' or value.get('plan_error'):
            return False
        if (value.get('errors') or value.get('acoustid_error') or value.get('primary_fingerprint_error')
                or value.get('verification_incomplete') or value.get('deferred') or value.get('cooldown_skipped')):
            return False
        return all(_stage_result_reusable(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_stage_result_reusable(item) for item in value)
    return True


def load_stage_result(file_path, preferences, stage):
    try:
        key = _cache_key(file_path, preferences, decision_version=STAGE_COMPAT_VERSION)[0]
        conn = _connect()
        with _lock:
            conn.execute('CREATE TABLE IF NOT EXISTS stages (cache_key TEXT, stage TEXT, payload BLOB, updated_at REAL, PRIMARY KEY(cache_key, stage))')
            row = conn.execute('SELECT payload, updated_at FROM stages WHERE cache_key=? AND stage=? AND updated_at>=?',
                               (key, stage, time.time() - _resume_max_age_days() * 86400)).fetchone()
        if row:
            value = json.loads(zlib.decompress(row[0]))
            # Do not stretch the providers' short negative-cache TTL into
            # the seven-day resume window: catalog availability can change.
            negative_days = cache_ttl_days('TAGCLEANER_NEGATIVE_CACHE_DAYS', 1)
            if (_stage_has_negative_result(value) and negative_days is not None
                    and time.time() - row[1] > negative_days * 86400):
                return None
            if isinstance(value, dict) and _stage_result_reusable(value):
                return value
    except Exception:
        pass
    return None


def _stage_has_negative_result(value):
    if isinstance(value, dict):
        status = value.get('status')
        return ((isinstance(status, str) and status in {'NOT_FOUND', 'NO_SOURCE', 'NO_MATCH'})
                or any(_stage_has_negative_result(v) for v in value.values()))
    if isinstance(value, (list, tuple)):
        return any(_stage_has_negative_result(v) for v in value)
    return False


def save_stage_result(file_path, preferences, stage, value):
    if not isinstance(value, dict) or not _stage_result_reusable(value):
        return
    try:
        key,pipeline,_,_ = _cache_key(file_path, preferences, decision_version=STAGE_COMPAT_VERSION)
        blob = zlib.compress(json.dumps(value, ensure_ascii=False).encode('utf-8'), level=1)
        conn = _connect()
        with _lock:
            conn.execute('CREATE TABLE IF NOT EXISTS stages (cache_key TEXT, stage TEXT, payload BLOB, updated_at REAL, PRIMARY KEY(cache_key, stage))')
            _replace_obsolete_key(conn,file_path,preferences,pipeline,key,'stages',stage)
            conn.execute('INSERT OR REPLACE INTO stages VALUES (?, ?, ?, ?)', (key, stage, blob, time.time()))
            conn.commit()
    except Exception:
        # Raw provider caches and report still work if SQLite is unavailable.
        pass
