RELEASE_FIELDS = (
    "album",
    "albumartist",
    "date",
    "tracknumber",
    "discnumber",
)

IDENTITY_FIELDS = (
    "artist",
    "title",
)


def _score(candidate, key):
    try:
        return float((candidate or {}).get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def evaluate_audd_restricted_safe(
    release_context_selection,
    credit_consensus,
    audd_match,
    mb_candidate,
):
    """
    Решает, можно ли разрешить ограниченный SAFE для release-dependent
    полей на пути AudD STRONG -> ISRC -> MusicBrainz.

    Artist и Title эта политика НИКОГДА не переводит в SAFE.
    """

    release_context_selection = release_context_selection or {}
    credit_consensus = credit_consensus or {}
    audd_match = audd_match or {}
    mb_candidate = mb_candidate or {}

    reasons = []

    release_selected = (
        release_context_selection.get("status") == "SELECTED"
        and bool(release_context_selection.get("selected"))
    )

    concrete_ambiguous = bool(
        release_context_selection.get("concrete_release_ambiguous")
    )

    credit_ok = (
        credit_consensus.get("status") == "CONSENSUS"
    )

    audd_strong = (
        audd_match.get("status") == "STRONG"
    )

    version_conflict = bool(
        audd_match.get("version_conflict")
    )

    artist_score = _score(mb_candidate, "artist_score")
    title_score = _score(mb_candidate, "title_score")
    overall_score = _score(mb_candidate, "overall_score")

    # SAFE-write должен быть заметно строже самого факта распознавания.
    mb_agreement_ok = (
        overall_score >= 94.0
        and artist_score >= 90.0
        and title_score >= 88.0
    )

    if not release_selected:
        reasons.append("ReleaseContext не выбран однозначно.")

    if concrete_ambiguous:
        reasons.append("Конкретное издание неоднозначно.")

    if not credit_ok:
        reasons.append("Artist credit не имеет межисточникового CONSENSUS.")

    if not audd_strong:
        reasons.append("AudD match не STRONG.")

    if version_conflict:
        reasons.append("Есть конфликт версии Remix/VIP/Edit/Bootleg.")

    if not mb_agreement_ok:
        reasons.append(
            "Согласованность MusicBrainz ↔ AudD ниже порога restricted SAFE "
            f"(Artist {artist_score:.1f}, Title {title_score:.1f}, "
            f"Overall {overall_score:.1f})."
        )

    eligible = not reasons

    if eligible:
        reason = (
            "AudD STRONG + ISRC + MusicBrainz RESOLVED + credit CONSENSUS + "
            "однозначный ReleaseContext. Разрешён restricted SAFE только "
            "для release-dependent полей."
        )
    else:
        reason = " ".join(reasons)

    return {
        "eligible": eligible,
        "reason": reason,
        "artist_score": round(artist_score, 2),
        "title_score": round(title_score, 2),
        "overall_score": round(overall_score, 2),
        "release_selected": release_selected,
        "concrete_release_ambiguous": concrete_ambiguous,
        "credit_status": credit_consensus.get("status"),
        "audd_status": audd_match.get("status"),
        "version_conflict": version_conflict,
        "safe_fields": list(RELEASE_FIELDS) if eligible else [],
        "review_fields": list(IDENTITY_FIELDS),
    }


def apply_audd_restricted_safe_policy(
    plan,
    release_context_selection,
    credit_consensus,
    audd_match,
    mb_candidate,
):
    """
    build_change_plan вызывается с safety="SAFE", после чего эта функция
    оставляет Artist/Title только REVIEW, а release-dependent SAFE разрешает
    лишь при строгом наборе подтверждений.
    """

    if not plan:
        return plan, evaluate_audd_restricted_safe(
            release_context_selection,
            credit_consensus,
            audd_match,
            mb_candidate,
        )

    decision = evaluate_audd_restricted_safe(
        release_context_selection,
        credit_consensus,
        audd_match,
        mb_candidate,
    )

    # Identity-поля пока никогда не пишем автоматически через AudD bridge.
    for field_name in IDENTITY_FIELDS:
        field = plan.get(field_name) or {}

        if field.get("status") == "SAFE":
            field["status"] = "REVIEW"
            field["reason"] = (
                "Identity подтверждён AudD + ISRC + MusicBrainz, но Artist/Title "
                "пока требуют ручной проверки даже при хорошем consensus."
            )

    # Release-dependent поля можно оставить SAFE только при строгом решении.
    for field_name in RELEASE_FIELDS:
        field = plan.get(field_name) or {}

        if field.get("status") != "SAFE":
            continue

        if decision["eligible"]:
            field["reason"] = (
                "Restricted SAFE: AudD STRONG + ISRC + MusicBrainz RESOLVED + "
                "credit CONSENSUS + однозначный ReleaseContext."
            )
        else:
            field["status"] = "REVIEW"
            field["reason"] = (
                "AudD bridge не прошёл условия restricted SAFE. "
                + decision["reason"]
            )

    return plan, decision
