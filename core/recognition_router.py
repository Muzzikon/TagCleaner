from pathlib import Path

from core.fingerprint import (
    generate_fingerprint,
)

from core.acoustid_client import (
    lookup_fingerprint,
    extract_candidates,
)

from core.matcher import (
    rank_candidates,
)

from core.audd_client import (
    recognize_with_audd,
)

from core.audd_matcher import (
    evaluate_audd_match,
)

from core.audd_cache import (
    load_cached_audd_result,
    save_audd_result,
    is_audd_quota_error,
    disable_audd_for_run,
    audd_disabled_reason,
    audd_network_enabled,
)


def recognize_with_fallback(
    file_path, data, *, fingerprint_data=None, allow_audd=True,
    primary_fingerprint_error=None,
):
    """Primary AcoustID, bounded alternate fingerprints, then optional AudD.

    Alternative windows retain every candidate and conflict. They are still
    ONE provider, and supplemental-only hits require independent confirmation.
    """
    from core.audio_multipass import additional_lookup
    from core.audio_timing import ensure_audio_timing

    file_path = Path(file_path)
    fingerprint_error = primary_fingerprint_error
    if fingerprint_data is None and not fingerprint_error:
        try:
            fingerprint_data = generate_fingerprint(file_path)
        except Exception as error:
            fingerprint_error = str(error)

    lookup_data, candidates, acoustid_error = None, [], None
    ensure_audio_timing(file_path, data, [(fingerprint_data or {}).get('duration')])
    if fingerprint_data is not None:
        try:
            lookup_data = lookup_fingerprint(fingerprint_data['duration'], fingerprint_data['fingerprint'])
            candidates = extract_candidates(lookup_data)
        except Exception as error:
            acoustid_error = str(error)

    ensure_audio_timing(file_path, data, [c.get('duration') for c in candidates if float(c.get('score') or 0) >= .85])
    candidates, multipass = additional_lookup(
        file_path, data, fingerprint_data, candidates, lookup=lookup_fingerprint,
    )
    ensure_audio_timing(file_path, data, [c.get('duration') for c in candidates if float(c.get('score') or 0) >= .85])
    common = {
        'fingerprint_data': fingerprint_data,
        'primary_fingerprint_error': fingerprint_error,
        'acoustid_lookup': lookup_data,
        'acoustid_candidates': candidates,
        'acoustid_multipass': multipass,
        'ranked_candidates': [],
        'acoustid_error': acoustid_error,
        'audd_result': None, 'audd_match': None, 'fallback_used': False,
    }
    try:
        ranked = rank_candidates(data, candidates)
    except Exception as error:
        return {**common, 'status': 'ERROR', 'provider': None,
                'error': f'Ошибка ранжирования AcoustID-кандидатов: {error}'}
    if ranked:
        return {**common, 'status': 'FOUND', 'provider': 'acoustid',
                'ranked_candidates': ranked, 'error': None}
    if not allow_audd:
        failed = bool(acoustid_error or fingerprint_error or multipass.get('errors'))
        return {**common, 'status': 'ERROR' if failed else 'NOT_FOUND',
                'provider': None, 'audd_deferred': True,
                'error': acoustid_error or fingerprint_error or
                         ('; '.join(multipass.get('errors') or []) if failed else None)}
    fallback = recognize_with_audd_only(file_path, data, fingerprint_data=fingerprint_data)
    return {**common, **fallback, 'fallback_used': True}


def recognize_with_audd_only(
    file_path,
    data,
    *,
    fingerprint_data,
):
    """Запускает AudD как последний дорогой fallback без повторного AcoustID.

    Кэш и отключение после quota error используются те же, что и в старом
    маршрутизаторе. Функция нужна, чтобы сначала исчерпать source/catalog/ACR.
    """

    file_path = Path(file_path)

    audd_result = load_cached_audd_result(fingerprint_data)
    cache_hit = audd_result is not None

    # Keep real positive audio evidence. An intentionally unused provider is
    # neither a failed request nor evidence that the song was not found.
    if not audd_network_enabled() and (audd_result or {}).get('status') != 'FOUND':
        return {'status': 'SKIPPED', 'provider': None, 'audd_result': None,
                'audd_match': None, 'audd_cache_hit': False, 'audd_skipped': False,
                'audd_disabled_by_policy': True,
                'reason': 'Новые запросы AudD отключены; поиск ограничен включёнными источниками.',
                'error': None}

    if audd_result is None:
        disabled_reason = audd_disabled_reason()
        if disabled_reason:
            return {
                "status": "FALLBACK_ERROR",
                "provider": None,
                "audd_result": None,
                "audd_match": None,
                "audd_cache_hit": False,
                "audd_skipped": True,
                "error": (
                    "AudD пропущен до конца текущего запуска: "
                    f"{disabled_reason}"
                ),
            }

        try:
            audd_result = recognize_with_audd(file_path)
        except Exception as error:
            return {
                "status": "FALLBACK_ERROR",
                "provider": None,
                "audd_result": None,
                "audd_match": None,
                "audd_cache_hit": False,
                "audd_skipped": False,
                "error": f"Ошибка AudD fallback: {error}",
            }

        if is_audd_quota_error(audd_result):
            error_info = audd_result.get("error") or audd_result
            disable_audd_for_run(error_info)
            return {
                "status": "FALLBACK_ERROR",
                "provider": None,
                "audd_result": audd_result,
                "audd_match": None,
                "audd_cache_hit": False,
                "audd_skipped": False,
                "error": audd_result.get("error"),
            }

        save_audd_result(fingerprint_data, audd_result)

    if audd_result.get("status") == "FOUND":
        try:
            match = evaluate_audd_match(file_path, data, audd_result)
        except Exception as error:
            return {
                "status": "FALLBACK_ERROR",
                "provider": None,
                "audd_result": audd_result,
                "audd_match": None,
                "audd_cache_hit": cache_hit,
                "audd_skipped": False,
                "error": f"Ошибка проверки AudD кандидата: {error}",
            }

        return {
            "status": "FOUND",
            "provider": "audd",
            "audd_result": audd_result,
            "audd_match": match,
            "audd_cache_hit": cache_hit,
            "audd_skipped": False,
            "error": None,
        }

    if audd_result.get("status") == "NOT_FOUND":
        return {
            "status": "NOT_FOUND",
            "provider": None,
            "audd_result": audd_result,
            "audd_match": None,
            "audd_cache_hit": cache_hit,
            "audd_skipped": False,
            "error": None,
        }

    return {
        "status": "FALLBACK_ERROR",
        "provider": None,
        "audd_result": audd_result,
        "audd_match": None,
        "audd_cache_hit": cache_hit,
        "audd_skipped": False,
        "error": audd_result.get("error"),
    }
