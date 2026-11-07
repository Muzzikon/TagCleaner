from concurrent.futures import ThreadPoolExecutor
import time
import sys
import traceback
from datetime import datetime
from pathlib import Path
import os
from tkinter import Tk, filedialog

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from core.scanner import (
    find_audio_files,
    scan_file,
)

from core.auditor import audit_tags, reconcile_issues_with_change_plan

from core.fingerprint import (
    generate_fingerprint,
    fingerprint_worker_count,
)

from core.run_state import (
    activate_checkpoint_run,
    load_completed_result,
    save_completed_result,
    prune_old_checkpoints,
    load_stage_result,
    save_stage_result,
)

from core.network_pipeline import (
    NetworkPipeline,
)

from core.matcher import (
    confidence_level,
    metadata_agreement,
    action_safety,
)

from core.recognition_router import (
    recognize_with_fallback,
    recognize_with_audd_only,
)

from core.isrc_resolver import (
    resolve_isrc_recording,
)

from core.credit_consensus import (
    evaluate_credit_consensus,
)

from core.audd_safe_policy import (
    apply_audd_restricted_safe_policy,
)

from core.source_url_resolver import (
    resolve_source_identity,
)

from core.catalog_search_resolver import (
    resolve_catalog_identity,
)

from core.acrcloud_resolver import (
    resolve_acrcloud_identity,
)
from core.shazam_resolver import resolve_shazam_identity
from core.shazam_client import (shazam_available, shazam_capability, shazam_transport_stats,
                               configure_progress, cancel_pending_waits)
from core.audd_client import audd_dns_stats

from core.identity_fusion import (
    fuse_identity,
)

from core.musicbrainz_client import (
    get_recording,
    format_artist_credit,
    extract_release_groups,
    get_official_release_tracks,
    get_official_release_contexts,
    musicbrainz_transport_stats,
    musicbrainz_dns_stats,
)

from core.title_resolver import (
    resolve_official_track_title,
)

from core.change_planner import (
    build_change_plan,
    build_local_cleanup_plan,
    build_release_metadata_preview,
)

from core.tag_writer import (
    collect_safe_operations,
    execute_write_plan,
    create_backup_session,
)

from core.interactive_review import (
    collect_interactive_operations,
)

from core.plan_exporter import (
    export_change_plan,
)

from core.preferences import (
    load_preferences,
    configure_preferences,
)

from core.release_selector import (
    prepare_releases,
    select_release,
    select_release_context,
)

from core.selection_history import (
    load_last_selection,
    save_last_selection,
)


ROOT_DIR = Path(__file__).resolve().parent

console = Console(record=True)
run_journal = None
active_pipeline = None
active_fingerprint_executor = None


def _rich(value):
    """Escape user/file metadata so Rich does not eat text in [brackets]."""
    if value is None:
        return ""
    return escape(str(value))


preferences = load_preferences()


# ============================================================
# ВЫБОР ФАЙЛОВ
# ============================================================

def choose_files():
    root = Tk()
    root.withdraw()

    try:
        root.attributes(
            "-topmost",
            True,
        )

        selected = filedialog.askopenfilenames(
            title="Выберите аудиофайлы",
            filetypes=[
                (
                    "Аудиофайлы",
                    (
                        "*.mp3",
                        "*.flac",
                        "*.m4a",
                        "*.mp4",
                        "*.ogg",
                        "*.opus",
                        "*.wav",
                        "*.aiff",
                        "*.aif",
                    ),
                ),
                (
                    "Все файлы",
                    "*.*",
                ),
            ],
        )

    finally:
        root.destroy()

    files = []

    for value in selected:
        path = Path(value)

        files.extend(
            find_audio_files(path)
        )

    return files


def choose_folder():
    root = Tk()
    root.withdraw()

    try:
        root.attributes(
            "-topmost",
            True,
        )

        selected = filedialog.askdirectory(
            title="Выберите папку с музыкой"
        )

    finally:
        root.destroy()

    if not selected:
        return []

    return list(
        find_audio_files(
            Path(selected)
        )
    )


requested_checkpoint_mode = "resume"


def choose_checkpoint_mode():
    console.print("\n[bold]Как проверить выбранные файлы?[/bold]")
    console.print("1 — продолжить сохранённую проверку (в том числе после перезапуска)")
    console.print("2 — начать проверку сначала с новыми ответами Shazam")
    console.print("3 — заново пересчитать решения с обычным использованием кэша сервисов")
    console.print("[dim]В режиме 2 старые ответы Shazam не используются. Полученные в этой проверке "
                  "ответы сохраняются для продолжения после перезапуска. Отпечатки и кэш остальных "
                  "сервисов сохраняются. Shazam вызывается для треков, которым нужно дополнительное "
                  "подтверждение; свежие запросы могут занять значительно больше времени.[/dim]")
    while True:
        choice = input("Режим [Enter = 1]: ").strip()
        if choice in {"", "1"}:
            return "resume"
        if choice == "2":
            return "recheck"
        if choice == "3":
            return "recheck_cached"
        console.print("[yellow]Выберите 1, 2 или 3.[/yellow]")


def choose_input():
    global preferences, requested_checkpoint_mode

    while True:
        console.print()
        console.print(
            "[bold]Что открыть?[/bold]"
        )

        console.print(
            "1 — выбрать один или несколько файлов"
        )

        console.print(
            "2 — выбрать папку"
        )

        console.print(
            "3 — открыть прошлый набор"
        )

        console.print(
            "4 — настройки TagCleaner"
        )

        choice = input(
            "Выбор: "
        ).strip()

        # ----------------------------------------------------
        # ОДИН ИЛИ НЕСКОЛЬКО ФАЙЛОВ
        # ----------------------------------------------------

        if choice == "1":
            files = choose_files()

            if not files:
                console.print(
                    "[yellow]"
                    "Аудиофайлы не выбраны."
                    "[/yellow]"
                )

                continue

            save_last_selection(
                files
            )

            requested_checkpoint_mode = choose_checkpoint_mode()
            return files

        # ----------------------------------------------------
        # ПАПКА
        # ----------------------------------------------------

        if choice == "2":
            files = choose_folder()

            if not files:
                console.print(
                    "[yellow]"
                    "Аудиофайлы не найдены."
                    "[/yellow]"
                )

                continue

            save_last_selection(
                files
            )

            requested_checkpoint_mode = choose_checkpoint_mode()
            return files

        # ----------------------------------------------------
        # ПРЕДЫДУЩИЙ НАБОР
        # ----------------------------------------------------

        if choice == "3":
            previous_files, missing_files = (
                load_last_selection()
            )

            if missing_files:
                console.print(
                    "[yellow]"
                    f"Не найдено файлов из прошлого "
                    f"набора: {len(missing_files)}"
                    "[/yellow]"
                )

            if not previous_files:
                console.print(
                    "[red]"
                    "Прошлого набора нет "
                    "или его файлы больше не существуют."
                    "[/red]"
                )

                continue

            console.print(
                "[green]"
                f"Загружен прошлый набор: "
                f"{len(previous_files)} файлов"
                "[/green]"
            )

            requested_checkpoint_mode = choose_checkpoint_mode()
            return previous_files

        # ----------------------------------------------------
        # НАСТРОЙКИ
        # ----------------------------------------------------

        if choice == "4":
            preferences = (
                configure_preferences(
                    preferences
                )
            )

            console.print(
                "[green]"
                "Настройки сохранены."
                "[/green]"
            )

            continue

        console.print(
            "[red]"
            "Нужно выбрать 1, 2, 3 или 4."
            "[/red]"
        )


def print_release_context_v2(
    contexts,
    selection,
):
    """
    Временный read-only вывод ReleaseContext v2.

    Ничего не меняет в change_plan
    и ничего не записывает в теги.
    """

    console.print()

    console.print(
        "[bold magenta]"
        "ReleaseContext v2 "
        "(read-only)"
        "[/bold magenta]"
    )

    if not contexts:
        console.print(
            "[dim]"
            "Конкретные ReleaseContext "
            "не найдены."
            "[/dim]"
        )

        return

    selected = (
        selection.get(
            "selected"
        )
        if selection
        else None
    )

    selected_release_id = (
        selected.get(
            "release_id"
        )
        if selected
        else None
    )

    for index, context in enumerate(
        contexts,
        start=1,
    ):
        release_id = (
            context.get(
                "release_id"
            )
            or ""
        )

        if (
            selected_release_id
            and release_id
            == selected_release_id
        ):
            marker = "→"
        else:
            marker = " "

        title = (
            context.get("title")
            or "—"
        )

        release_type = (
            context.get(
                "release_type"
            )
            or context.get(
                "primary_type"
            )
            or "тип не указан"
        )

        date = (
            context.get("date")
            or "—"
        )

        album_artist = (
            context.get(
                "album_artist_credit"
            )
            or "—"
        )

        track_number = (
            context.get(
                "track_number"
            )
            or "—"
        )

        track_total = (
            context.get(
                "track_total"
            )
        )

        if track_total:
            track_text = (
                f"{track_number}/"
                f"{track_total}"
            )
        else:
            track_text = str(
                track_number
            )

        disc_number = (
            context.get(
                "disc_number"
            )
            or "—"
        )

        disc_total = (
            context.get(
                "disc_total"
            )
        )

        if disc_total:
            disc_text = (
                f"{disc_number}/"
                f"{disc_total}"
            )
        else:
            disc_text = str(
                disc_number
            )

        console.print(
            f"{marker} "
            f"{index}. "
            f"[cyan]{_rich(title)}[/cyan] "
            f"({release_type})"
        )

        console.print(
            "   "
            f"Дата: {date}"
            f" | Album Artist: "
            f"{_rich(album_artist)}"
            f" | Track: "
            f"{track_text}"
            f" | Disc: "
            f"{disc_text}"
        )

        console.print(
            "[dim]"
            "   release_id: "
            f"{release_id or '—'}"
            "[/dim]"
        )

    console.print()

    if not selection:
        console.print(
            "[dim]"
            "Решение selector v2 "
            "отсутствует."
            "[/dim]"
        )

        return

    status = (
        selection.get(
            "status"
        )
        or "NONE"
    )

    reason = (
        selection.get(
            "reason"
        )
        or ""
    )

    if status == "SELECTED":
        selected_title = (
            selected.get("title")
            if selected
            else "—"
        )

        console.print(
            "[green]"
            "Selector v2: SELECTED"
            "[/green]"
        )

        console.print(
            "Выбран контекст: "
            f"{_rich(selected_title)}"
        )

    elif status == "ASK":
        console.print(
            "[yellow]"
            "Selector v2: ASK"
            "[/yellow]"
        )

        console.print(
            "Вариантов для выбора: "
            f"{len(selection.get('options') or [])}"
        )

    else:
        console.print(
            "[dim]"
            f"Selector v2: {status}"
            "[/dim]"
        )

    if reason:
        console.print(
            f"Причина: {_rich(reason)}"
        )

    equivalent = (
        selection.get(
            "equivalent_releases"
        )
        or []
    )

    if equivalent:
        console.print(
            "Эквивалентных конкретных "
            "изданий: "
            f"{len(equivalent)}"
        )

    if selection.get(
        "concrete_release_ambiguous"
    ):
        console.print(
            "[yellow]"
            "Теги определены однозначно, "
            "но конкретное издание "
            "остаётся неоднозначным."
            "[/yellow]"
        )


def print_release_metadata_preview(
    preview,
):
    """
    Показывает, что выбранный ReleaseContext
    изменил бы в release-dependent тегах.

    READ-ONLY.
    """

    console.print()

    console.print(
        "[bold magenta]"
        "Release fields v2 "
        "(read-only)"
        "[/bold magenta]"
    )

    if not preview:
        console.print(
            "[dim]"
            "Данные отсутствуют."
            "[/dim]"
        )
        return

    if (
        preview.get("status")
        != "SELECTED"
    ):
        console.print(
            "[yellow]"
            "Релиз пока не выбран — "
            "release-dependent поля "
            "не изменяются."
            "[/yellow]"
        )

        reason = (
            preview.get(
                "reason"
            )
            or ""
        )

        if reason:
            console.print(
                f"Причина: {_rich(reason)}"
            )

        return

    fields = (
        preview.get(
            "fields"
        )
        or {}
    )

    table = Table()

    table.add_column(
        "Поле",
        width=16,
    )

    table.add_column(
        "Сейчас",
        width=30,
    )

    table.add_column(
        "По ReleaseContext",
        width=30,
    )

    table.add_column(
        "Результат",
        width=16,
    )

    labels = {
        "album": "Album",
        "albumartist": "Album Artist",
        "date": "Date",
        "tracknumber": "Track",
        "discnumber": "Disc",
    }

    for key, label in (
        labels.items()
    ):
        field = (
            fields.get(key)
            or {}
        )

        current = (
            field.get(
                "current"
            )
            or "—"
        )

        proposed = (
            field.get(
                "proposed"
            )
            or "—"
        )

        if field.get(
            "changed"
        ):
            result_text = (
                "[yellow]"
                "ИЗМЕНИЛОСЬ"
                "[/yellow]"
            )

        else:
            result_text = (
                "[dim]"
                "без изменений"
                "[/dim]"
            )

        table.add_row(
            label,
            _rich(current),
            _rich(proposed),
            result_text,
        )

    console.print(
        table
    )

    release_id = (
        preview.get(
            "release_id"
        )
    )

    if release_id:
        console.print(
            "[dim]"
            "Источник: MusicBrainz Release "
            f"{release_id}"
            "[/dim]"
        )

# ============================================================
# ВЫВОД AUDD FALLBACK
# ============================================================

def print_audd_fallback(
    file,
    data,
    audd_result,
    audd_match=None,
):
    """
    Показывает результат AudD fallback и
    локальную проверку согласованности.

    Даже STRONG пока НЕ создаёт SAFE-правки,
    не выбирает ReleaseContext и ничего
    автоматически не записывает.
    """

    audd_result = audd_result or {}
    audd_match = audd_match or {}

    console.print()

    console.print(
        f"[bold magenta]"
        f"AudD fallback: {_rich(file.name)}"
        f"[/bold magenta]"
    )

    match_status = (
        audd_match.get("status")
        or "PARTIAL"
    )

    if match_status == "STRONG":
        status_text = (
            "[green]STRONG[/green]"
        )

        message = (
            "AudD хорошо согласуется с "
            "filename/текущими тегами."
        )

    elif match_status == "CONFLICT":
        status_text = (
            "[red]CONFLICT[/red]"
        )

        message = (
            "AudD распознал фрагмент, но результат "
            "конфликтует с filename/текущими тегами "
            "и не должен использоваться как identity."
        )

    else:
        status_text = (
            "[yellow]PARTIAL[/yellow]"
        )

        message = (
            "AudD частично согласуется с "
            "filename/текущими тегами и требует проверки."
        )

    console.print(
        "Проверка AudD: "
        f"{status_text}"
    )

    console.print(
        message
    )

    artist_score = (
        audd_match.get("artist_score")
    )

    title_score = (
        audd_match.get("title_score")
    )

    overall_score = (
        audd_match.get("overall_score")
    )

    if (
        artist_score is not None
        and title_score is not None
    ):
        console.print(
            "Согласованность: "
            f"Artist {artist_score:.1f} | "
            f"Title {title_score:.1f} | "
            f"Overall {float(overall_score or 0):.1f}"
        )

    reason = (
        audd_match.get("reason")
    )

    if reason:
        console.print(
            f"Причина: {_rich(reason)}"
        )

    artist = (
        audd_result.get("artist")
        or "—"
    )

    title = (
        audd_result.get("title")
        or "—"
    )

    album = (
        audd_result.get("album")
        or "—"
    )

    release_date = (
        audd_result.get("release_date")
        or "—"
    )

    label = (
        audd_result.get("label")
        or "—"
    )

    isrc = (
        audd_result.get("isrc")
        or "—"
    )

    console.print(
        f"Исполнитель: {_rich(artist)}"
    )

    console.print(
        f"Название: {_rich(title)}"
    )

    console.print(
        f"Album: {_rich(album)}"
    )

    console.print(
        f"Дата: {_rich(release_date)}"
    )

    console.print(
        f"Label: {_rich(label)}"
    )

    console.print(
        f"ISRC: {isrc}"
    )

    spotify = (
        audd_result.get("spotify")
        or {}
    )

    spotify_artists = (
        spotify.get("artists")
        or []
    )

    spotify_track_id = (
        spotify.get("track_id")
    )

    if (
        spotify_artists
        or spotify_track_id
    ):
        console.print(
            "Spotify: "
            + (
                ", ".join(
                    spotify_artists
                )
                if spotify_artists
                else "—"
            )
        )

        if spotify_track_id:
            console.print(
                "Spotify track_id: "
                f"{spotify_track_id}"
            )

    apple_music = (
        audd_result.get(
            "apple_music"
        )
        or {}
    )

    if apple_music:
        apple_artist = (
            apple_music.get("artist")
            or "—"
        )

        apple_title = (
            apple_music.get("title")
            or "—"
        )

        console.print(
            "Apple Music: "
            f"{apple_artist} — "
            f"{apple_title}"
        )

    sample = (
        audd_result.get("sample")
        or {}
    )

    if sample:
        sample_start = (
            sample.get("sample_start")
        )

        sample_duration = (
            sample.get("sample_duration")
        )

        if (
            sample_start is not None
            and sample_duration is not None
        ):
            console.print(
                "[dim]"
                "AudD sample: "
                f"{float(sample_start):.1f}s → "
                f"+{float(sample_duration):.1f}s"
                "[/dim]"
            )

    console.print(
        "[yellow]"
        "AudD пока не создаёт SAFE-правки "
        "Artist / Title / Album / release fields."
        "[/yellow]"
    )



def _downgrade_audd_identity_plan_to_review(
    plan,
):
    """
    Даже если AudD STRONG + ISRC + MusicBrainz
    дали однозначный Recording/ReleaseContext,
    этот путь пока не получает SAFE-write.
    """

    if not plan:
        return plan

    protected_fields = {
        "artist",
        "title",
        "album",
        "albumartist",
        "date",
        "tracknumber",
        "discnumber",
    }

    for field_name in protected_fields:
        field = (
            plan.get(field_name)
            or {}
        )

        if field.get("status") != "SAFE":
            continue

        field["status"] = "REVIEW"
        field["reason"] = (
            "AudD STRONG + ISRC + MusicBrainz "
            "подтверждают identity, но этот путь "
            "пока требует ручной проверки."
        )

    return plan


def process_audd_musicbrainz_bridge(
    file,
    data,
    audd_result,
    audd_match,
):
    """
    AudD STRONG -> ISRC -> MusicBrainz Recording ->
    ReleaseContext v2 -> REVIEW-план.

    ВАЖНО:
    автоматический SAFE-write здесь намеренно запрещён.
    """

    audd_result = audd_result or {}
    audd_match = audd_match or {}

    isrc = (
        audd_result.get("isrc")
        or ""
    )

    console.print()
    console.print(
        "[bold blue]"
        "AudD → ISRC → MusicBrainz"
        "[/bold blue]"
    )

    if not isrc:
        result = {
            "status": "NO_ISRC",
            "isrc": None,
            "reason": (
                "AudD STRONG, но ISRC отсутствует."
            ),
        }

        data[
            "audd_musicbrainz"
        ] = result

        console.print(
            "[yellow]"
            "ISRC отсутствует — MusicBrainz bridge "
            "не запускался."
            "[/yellow]"
        )

        return result

    try:
        result = (
            resolve_isrc_recording(
                isrc,
                audd_result,
            )
        )

    except Exception as error:
        result = {
            "status": "ERROR",
            "isrc": isrc,
            "recording_id": None,
            "recording": None,
            "reason": str(error),
        }

    data[
        "audd_musicbrainz"
    ] = result

    status = (
        result.get("status")
        or "ERROR"
    )

    reason = (
        result.get("reason")
        or ""
    )

    if status != "RESOLVED":
        if status == "AMBIGUOUS":
            status_text = (
                "[yellow]AMBIGUOUS[/yellow]"
            )
        elif status == "CONFLICT":
            status_text = (
                "[red]CONFLICT[/red]"
            )
        elif status == "NOT_FOUND":
            status_text = (
                "[yellow]NOT_FOUND[/yellow]"
            )
        elif status == "NO_ISRC":
            status_text = (
                "[yellow]NO_ISRC[/yellow]"
            )
        else:
            status_text = (
                f"[red]{status}[/red]"
            )

        console.print(
            "MusicBrainz по ISRC: "
            f"{status_text}"
        )

        if reason:
            console.print(
                f"Причина: {_rich(reason)}"
            )

        candidates = (
            result.get("candidates")
            or []
        )

        if candidates:
            console.print(
                "Кандидаты MusicBrainz:"
            )

            for candidate in candidates[:5]:
                console.print(
                    "  - "
                    f"{_rich(candidate.get('artist_credit') or '—')} — "
                    f"{_rich(candidate.get('title') or '—')} "
                    f"(Artist {float(candidate.get('artist_score') or 0):.1f}, "
                    f"Title {float(candidate.get('title_score') or 0):.1f}, "
                    f"Overall {float(candidate.get('overall_score') or 0):.1f})"
                )

        return result

    recording_id = (
        result.get("recording_id")
    )

    musicbrainz_data = (
        result.get("recording")
        or {}
    )

    selected_mb_candidate = (
        result.get("selected")
        or {}
    )

    console.print(
        "MusicBrainz по ISRC: "
        "[green]RESOLVED[/green]"
    )

    console.print(
        "Recording: "
        f"{_rich(selected_mb_candidate.get('artist_credit') or '—')} — "
        f"{_rich(selected_mb_candidate.get('title') or '—')}"
    )

    console.print(
        "Согласованность MB ↔ AudD: "
        f"Artist {float(selected_mb_candidate.get('artist_score') or 0):.1f} | "
        f"Title {float(selected_mb_candidate.get('title_score') or 0):.1f} | "
        f"Overall {float(selected_mb_candidate.get('overall_score') or 0):.1f}"
    )

    console.print(
        "[dim]"
        f"recording_id: {recording_id or '—'}"
        "[/dim]"
    )

    equivalent_ids = (
        result.get(
            "equivalent_recording_ids"
        )
        or []
    )

    if len(equivalent_ids) > 1:
        console.print(
            "[yellow]"
            "У ISRC есть несколько эквивалентных "
            "MusicBrainz Recording; выбран один для "
            "read-only/REVIEW обработки."
            "[/yellow]"
        )

    data["musicbrainz"] = (
        musicbrainz_data
    )

    data[
        "audd_musicbrainz_recording_id"
    ] = recording_id

    data[
        "audd_musicbrainz_source"
    ] = "isrc"

    artist_credit = (
        format_artist_credit(
            musicbrainz_data
        )
    )

    if artist_credit:
        console.print(
            "Artist credit: "
            f"{artist_credit}"
        )

    # --------------------------------------------------------
    # CREDIT CONSENSUS
    #
    # Identity по ISRC уже подтверждён, но user-facing Artist
    # может отличаться между filename/tags/AudD/Spotify/Apple/MB.
    # Это отдельная проверка: не даём MusicBrainz незаметно
    # удалить collaborator/feat/исторический credit.
    # --------------------------------------------------------

    try:
        credit_consensus = (
            evaluate_credit_consensus(
                file,
                data,
                audd_result,
                musicbrainz_data,
            )
        )
    except Exception as error:
        credit_consensus = {
            "status": "PARTIAL",
            "reason": (
                "Не удалось проверить Artist credit: "
                f"{error}"
            ),
            "musicbrainz_credit": (
                artist_credit or None
            ),
            "expected_artists": [],
            "missing_from_musicbrainz": [],
            "musicbrainz_only": [],
        }

    data[
        "credit_consensus"
    ] = credit_consensus

    result[
        "credit_consensus"
    ] = credit_consensus

    credit_status = (
        credit_consensus.get("status")
        or "PARTIAL"
    )

    if credit_status == "CONSENSUS":
        credit_status_text = (
            "[green]CONSENSUS[/green]"
        )
    elif credit_status == "CONFLICT":
        credit_status_text = (
            "[red]CONFLICT[/red]"
        )
    else:
        credit_status_text = (
            "[yellow]PARTIAL[/yellow]"
        )

    console.print(
        "Credit consensus: "
        f"{credit_status_text}"
    )

    credit_reason = (
        credit_consensus.get("reason")
    )

    if credit_reason:
        console.print(
            f"Причина credit: {_rich(credit_reason)}"
        )

    missing_credit = (
        credit_consensus.get(
            "missing_from_musicbrainz"
        )
        or []
    )

    if missing_credit:
        console.print(
            "[red]"
            "MusicBrainz не содержит: "
            + ", ".join(missing_credit)
            + "[/red]"
        )

    mb_only_credit = (
        credit_consensus.get(
            "musicbrainz_only"
        )
        or []
    )

    if mb_only_credit:
        console.print(
            "[yellow]"
            "Только в MusicBrainz credit: "
            + ", ".join(mb_only_credit)
            + "[/yellow]"
        )

    official_release_tracks = []

    if recording_id:
        try:
            official_release_tracks = (
                get_official_release_tracks(
                    recording_id
                )
            )

            data[
                "official_release_tracks"
            ] = (
                official_release_tracks
            )

        except Exception as error:
            result['verification_incomplete'] = True
            result.setdefault('errors', []).append(f'musicbrainz_release_tracks: {error}')
            console.print(
                "[yellow]"
                "Не удалось получить официальные "
                "track titles для AudD→ISRC: "
                f"{error}"
                "[/yellow]"
            )

    musicbrainz_releases = (
        extract_release_groups(
            musicbrainz_data
        )
        if musicbrainz_data
        else []
    )

    if preferences.get(
        "normalize_names",
        True,
    ):
        releases = (
            prepare_releases(
                musicbrainz_releases
            )
        )
    else:
        releases = list(
            musicbrainz_releases
        )

    # Legacy Release Group здесь автоматически не выбираем.
    # Конкретный ReleaseContext v2 полезнее и безопаснее.
    selected_release = None

    release_contexts = []
    release_context_selection = {
        "status": "NONE",
        "selected": None,
        "options": [],
        "equivalent_releases": [],
        "concrete_release_ambiguous": False,
        "reason": (
            "ReleaseContext v2 для AudD→ISRC "
            "не запускался."
        ),
    }

    selected_release_context = None

    if recording_id:
        try:
            release_contexts = (
                get_official_release_contexts(
                    recording_id
                )
            )

            # Для самого selector разрешаем нормальный выбор:
            # identity уже подтверждён AudD STRONG + exact ISRC.
            # Но ниже change_plan всё равно принудительно REVIEW.
            release_context_selection = (
                select_release_context(
                    contexts=(
                        release_contexts
                    ),
                    preference=(
                        preferences.get(
                            "release_preference",
                            "ask",
                        )
                    ),
                    current_album=(
                        data.get("album")
                        or ""
                    ),
                    safety="SAFE",
                )
            )

            selected_release_context = (
                release_context_selection.get(
                    "selected"
                )
            )

        except Exception as error:
            result['verification_incomplete'] = True
            result.setdefault('errors', []).append(f'musicbrainz_release_contexts: {error}')
            release_context_selection = {
                "status": "NONE",
                "selected": None,
                "options": [],
                "equivalent_releases": [],
                "concrete_release_ambiguous": False,
                "reason": (
                    "ReleaseContext v2 недоступен: "
                    f"{error}"
                ),
            }

            console.print(
                "[yellow]"
                "ReleaseContext v2 для AudD→ISRC "
                "недоступен: "
                f"{error}"
                "[/yellow]"
            )

    data[
        "release_contexts"
    ] = release_contexts

    data[
        "release_context_selection"
    ] = release_context_selection

    data[
        "selected_release_context"
    ] = selected_release_context

    release_metadata_preview = (
        build_release_metadata_preview(
            data=data,
            selected_release_context=(
                selected_release_context
            ),
            release_context_selection=(
                release_context_selection
            ),
        )
    )

    data[
        "release_metadata_preview"
    ] = release_metadata_preview

    recording_title = (
        musicbrainz_data.get("title")
        or audd_result.get("title")
        or ""
    )

    official_title = (
        resolve_official_track_title(
            official_release_tracks,
            recording_title=(
                recording_title
            ),
            selected_release=(
                selected_release
            ),
        )
    )

    data[
        "official_title"
    ] = official_title

    data[
        "possible_releases"
    ] = releases

    data[
        "selected_release"
    ] = selected_release

    if releases:
        console.print(
            "Возможные релизы по MusicBrainz:"
        )

        for release in releases[:10]:
            release_type = (
                release.get("type")
                or "тип не указан"
            )

            secondary_types = (
                release.get(
                    "secondary_types",
                    [],
                )
                or []
            )

            if secondary_types:
                type_text = (
                    release_type
                    + " / "
                    + ", ".join(
                        secondary_types
                    )
                )
            else:
                type_text = (
                    release_type
                )

            console.print(
                "  - "
                f"{_rich(release.get('title') or '—')} "
                f"({type_text})"
            )

    print_release_context_v2(
        contexts=(
            release_contexts
        ),
        selection=(
            release_context_selection
        ),
    )

    print_release_metadata_preview(
        release_metadata_preview
    )

    if (
        official_title.get("status")
        in {
            "RESOLVED",
            "RESOLVED_BY_RECORDING",
        }
    ):
        console.print(
            "[green]"
            "Официальное название через "
            "AudD→ISRC→MusicBrainz: "
            "[/green]"
            f"{_rich(official_title.get('title') or recording_title)}"
        )

    best_match = {
        "candidate": {
            "recording_id": recording_id,
            "title": recording_title,
            "artists": (
                [artist_credit]
                if artist_credit
                else []
            ),
            "release_groups": (
                musicbrainz_releases
            ),
        },
        "score": (
            float(
                selected_mb_candidate.get(
                    "overall_score",
                    0.0,
                )
            )
            / 100.0
        ),
        "acoustid_score": 0.0,
    }

    try:
        change_plan = (
            build_change_plan(
                data=data,
                best_match=best_match,
                artist_credit=artist_credit,
                official_title=official_title,
                releases=releases,
                selected_release=(
                    selected_release
                ),
                # Сначала строим полный SAFE-кандидатный план,
                # а затем отдельная AudD policy оставляет SAFE только
                # для строго подтверждённых release-dependent полей.
                safety="SAFE",
                preferences=preferences,
                release_context_selection=(
                    release_context_selection
                ),
            )
        )

        change_plan, audd_safe_policy = (
            apply_audd_restricted_safe_policy(
                plan=change_plan,
                release_context_selection=(
                    release_context_selection
                ),
                credit_consensus=(
                    credit_consensus
                ),
                audd_match=(
                    audd_match
                ),
                mb_candidate=(
                    selected_mb_candidate
                ),
            )
        )

        data["audd_safe_policy"] = (
            audd_safe_policy
        )

        result["audd_safe_policy"] = (
            audd_safe_policy
        )

        # Identity может быть RESOLVED, но Artist credit — конфликтовать.
        # В таком случае даже REVIEW-предложение по Artist не строим:
        # сохраняем текущий Artist и показываем конфликт отдельно.
        if (
            credit_consensus.get("status")
            == "CONFLICT"
        ):
            artist_field = (
                change_plan.get("artist")
                or {}
            )

            if artist_field:
                current_artist = (
                    data.get("artist")
                    or artist_field.get("current")
                    or ""
                )

                artist_field["status"] = "KEEP"
                artist_field["proposed"] = current_artist
                artist_field["reason"] = (
                    "Identity подтверждён через AudD + ISRC + "
                    "MusicBrainz, но источники расходятся по Artist "
                    "credit. Текущий Artist сохраняется до ручной "
                    "проверки."
                )

        data[
            "change_plan"
        ] = change_plan

        if audd_safe_policy.get("eligible"):
            result[
                "pipeline_status"
            ] = "RESTRICTED_SAFE_READY"

            safe_names = {
                "album": "Album",
                "albumartist": "Album Artist",
                "date": "Date",
                "tracknumber": "Track",
                "discnumber": "Disc",
            }

            active_safe_fields = []

            for field_name, display_name in safe_names.items():
                field = (
                    change_plan.get(field_name)
                    or {}
                )

                if field.get("status") == "SAFE":
                    active_safe_fields.append(display_name)

            if active_safe_fields:
                console.print(
                    "[green]"
                    "AudD→ISRC→MusicBrainz: restricted SAFE разрешён для: "
                    + ", ".join(active_safe_fields)
                    + ". Artist и Title остаются REVIEW."
                    "[/green]"
                )
            else:
                console.print(
                    "[green]"
                    "AudD→ISRC→MusicBrainz прошёл restricted SAFE policy, "
                    "но release-dependent поля уже совпадают и записывать нечего. "
                    "Artist и Title остаются REVIEW."
                    "[/green]"
                )
        else:
            result[
                "pipeline_status"
            ] = "REVIEW_READY"

            console.print(
                "[yellow]"
                "AudD→ISRC→MusicBrainz остаётся REVIEW: "
                f"{audd_safe_policy.get('reason') or 'условия restricted SAFE не выполнены.'}"
                "[/yellow]"
            )

    except Exception as error:
        result[
            "pipeline_status"
        ] = "PLAN_ERROR"

        result[
            "plan_error"
        ] = str(error)

        console.print(
            "[yellow]"
            "MusicBrainz identity найден, но REVIEW-план "
            "не удалось построить: "
            f"{error}"
            "[/yellow]"
        )

    return result

def print_source_url_identity(
    file,
    result,
):
    result = result or {}
    status = result.get("status") or "NO_SOURCE"

    if status == "NO_SOURCE":
        return

    console.print()
    console.print(
        f"[bold cyan]Source URL fallback: {_rich(file.name)}[/bold cyan]"
    )

    if status == "STRONG":
        status_text = "[green]STRONG[/green]"
    elif status == "PARTIAL":
        status_text = "[yellow]PARTIAL[/yellow]"
    elif status == "AMBIGUOUS":
        status_text = "[yellow]AMBIGUOUS[/yellow]"
    elif status == "CONFLICT":
        status_text = "[red]CONFLICT[/red]"
    else:
        status_text = "[red]ERROR[/red]"

    console.print(f"Source identity: {status_text}")

    best = result.get("best") or {}
    if best:
        provider = best.get("provider") or "—"
        source_artist = best.get("source_artist") or "—"
        source_title = best.get("source_title") or best.get("raw_title") or "—"
        author = best.get("source_author") or "—"
        url = best.get("url") or "—"

        console.print(f"Provider: {_rich(provider)}")
        console.print(f"Artist: {_rich(source_artist)}")
        console.print(f"Title: {_rich(source_title)}")

        if author != "—":
            console.print(f"Author/channel: {_rich(author)}")

        artist_score = best.get("artist_score")
        title_score = best.get("title_score")
        overall_score = best.get("overall_score")

        artist_text = (
            "—"
            if artist_score is None
            else f"{float(artist_score):.1f}"
        )

        console.print(
            "Согласованность source: "
            f"Artist {artist_text} | "
            f"Title {float(title_score or 0):.1f} | "
            f"Overall {float(overall_score or 0):.1f}"
        )
        console.print(f"URL: {_rich(url)}")

    reason = result.get("reason")
    if reason:
        console.print(f"Причина: {_rich(reason)}")

    if status == "STRONG":
        console.print(
            "[cyan]"
            "Source URL подтвердил identity, но пока не создаёт SAFE-правки. "
            "Это дополнительный recognition-layer для редких/web-only треков."
            "[/cyan]"
        )


def _resolve_source_result(file, data):
    try:
        return resolve_source_identity(
            file_path=file,
            data=data,
        )
    except Exception as error:
        return {
            "status": "ERROR",
            "best": None,
            "results": [],
            "reason": str(error),
        }


def try_source_url_fallback(
    file,
    data,
    *,
    prefetched_result=None,
):
    result = (
        prefetched_result
        if prefetched_result is not None
        else _resolve_source_result(file, data)
    )

    data["source_url_identity"] = result
    print_source_url_identity(file, result)

    if result.get("status") == "STRONG":
        # Identity найдена, но metadata пока только REVIEW.
        data["action_safety"] = "REVIEW"

    return result


def print_catalog_identity(
    file,
    result,
):
    result = result or {}
    status = result.get("status") or "NOT_FOUND"

    if status == "NOT_FOUND":
        return

    console.print()
    console.print(
        f"[bold magenta]Catalog search fallback: {_rich(file.name)}[/bold magenta]"
    )

    if status == "STRONG":
        status_text = "[green]STRONG[/green]"
    elif status == "PARTIAL":
        status_text = "[yellow]PARTIAL[/yellow]"
    elif status == "AMBIGUOUS":
        status_text = "[yellow]AMBIGUOUS[/yellow]"
    elif status == "CONFLICT":
        status_text = "[red]CONFLICT[/red]"
    else:
        status_text = "[red]ERROR[/red]"

    console.print(f"Catalog identity: {status_text}")

    best = result.get("best") or {}
    if best:
        console.print(f"Provider: {_rich(best.get('provider') or '—')}")
        console.print(f"Artist: {_rich(best.get('artist') or '—')}")
        console.print(f"Title: {_rich(best.get('title') or '—')}")

        if best.get("album"):
            console.print(f"Album: {_rich(best.get('album'))}")

        if best.get("recording_id"):
            console.print(
                "[dim]"
                f"recording_id: {best.get('recording_id')}"
                "[/dim]"
            )

        duration_diff = best.get("duration_diff")
        duration_text = (
            "—"
            if duration_diff is None
            else f"{float(duration_diff):.1f}s"
        )

        console.print(
            "Согласованность catalog: "
            f"Artist {float(best.get('artist_score') or 0):.1f} | "
            f"Title {float(best.get('title_score') or 0):.1f} | "
            f"Duration Δ {duration_text} | "
            f"Overall {float(best.get('overall_score') or 0):.1f}"
        )

    reason = result.get("reason")
    if reason:
        console.print(f"Причина: {_rich(reason)}")

    errors = result.get("errors") or []
    for error in errors:
        console.print(
            "[yellow]"
            f"{_rich(error.get('provider') or 'catalog')}: "
            f"{_rich(error.get('error') or 'ошибка')}"
            "[/yellow]"
        )

    if status == "STRONG":
        console.print(
            "[magenta]"
            "Каталожный поиск подтвердил identity, но пока используется "
            "только как REVIEW/trusted recognition. SAFE-write не создаётся."
            "[/magenta]"
        )


def print_acrcloud_identity(
    file,
    result,
):
    result = result or {}
    status = result.get("status") or "NOT_FOUND"

    if status == "NOT_FOUND":
        return

    console.print()
    console.print(
        f"[bold bright_blue]ACRCloud fallback: {_rich(file.name)}[/bold bright_blue]"
    )

    if status == "STRONG":
        status_text = "[green]STRONG[/green]"
    elif status == "PARTIAL":
        status_text = "[yellow]PARTIAL[/yellow]"
    elif status == "CONFLICT":
        status_text = "[red]CONFLICT[/red]"
    else:
        status_text = "[red]ERROR[/red]"

    console.print(f"ACRCloud identity: {status_text}")

    best = result.get("best") or {}
    match = result.get("match") or {}

    if best:
        console.print(f"Artist: {_rich(best.get('artist') or '—')}")
        console.print(f"Title: {_rich(best.get('title') or '—')}")

        if best.get("album"):
            console.print(f"Album: {_rich(best.get('album'))}")

        if best.get("score") is not None:
            console.print(f"ACRCloud score: {best.get('score')}")

        if best.get("isrc"):
            console.print(f"ISRC: {best.get('isrc')}")

        if best.get("upc"):
            console.print(f"UPC: {best.get('upc')}")

        sample = best.get("sample") or {}
        if sample:
            console.print(
                "ACRCloud sample: "
                f"{sample.get('sample_start', '—')}s → "
                f"+{sample.get('sample_duration', '—')}s"
            )

    if match:
        console.print(
            "Согласованность ACRCloud: "
            f"Artist {float(match.get('artist_score') or 0):.1f} | "
            f"Title {float(match.get('title_score') or 0):.1f} | "
            f"Overall {float(match.get('overall_score') or 0):.1f}"
        )

    attempts = result.get("attempts") or []
    if attempts:
        console.print(
            "Проверено частей трека: "
            f"{len(attempts)}"
        )
        for attempt in attempts:
            fragment = attempt.get("recognition") or {}
            if fragment.get("status") == "FOUND":
                console.print(
                    f"Фрагмент {attempt.get('position')}: "
                    f"{_rich(fragment.get('artist') or '—')} — {_rich(fragment.get('title') or '—')} "
                    f"(audio score: {fragment.get('score', '—')})."
                )
            else:
                console.print(f"Фрагмент {attempt.get('position')}: {_rich(fragment.get('status') or 'ERROR')}.")
            if fragment.get('error'):
                console.print(f"Причина ошибки фрагмента {attempt.get('position')}: {_rich(fragment['error'])}")
            if fragment.get('status_code') is not None and fragment.get('status') == 'ERROR':
                console.print(f"Код ответа ACRCloud: {_rich(fragment['status_code'])}")
            if fragment.get('rate_limit_retries'):
                origin = " (сохранённый ответ из кэша)" if fragment.get('cache_hit') else ""
                console.print(f"Повторов после лимита частоты ACRCloud: {fragment['rate_limit_retries']}{origin}.")
            if fragment.get('pcm_fallback'):
                console.print(f"Фрагмент {attempt.get('position')}: резервная обработка PCM после ошибки 2004; итог {_rich(fragment.get('status') or 'ERROR')}.")
    if result.get("verification_incomplete"):
        console.print("[yellow]Проверка фрагментов не завершена: результат сохранён, требуется повторная проверка.[/yellow]")

    reason = result.get("reason")
    if reason:
        console.print(f"Причина: {_rich(reason)}")

    if status == "STRONG":
        console.print(
            "[bright_blue]"
            "ACRCloud подтвердил identity. Пока это только REVIEW/trusted recognition; "
            "SAFE-write из ACRCloud не создаётся."
            "[/bright_blue]"
        )


def _resolve_acrcloud_result(file, data):
    try:
        return resolve_acrcloud_identity(
            file_path=file,
            data=data,
        )
    except Exception as error:
        return {
            "status": "ERROR",
            "best": None,
            "match": None,
            "attempts": [],
            "reason": str(error),
        }


def try_acrcloud_fallback(
    file,
    data,
    *,
    prefetched_result=None,
):
    result = (
        prefetched_result
        if prefetched_result is not None
        else _resolve_acrcloud_result(file, data)
    )

    data["acrcloud_identity"] = result
    print_acrcloud_identity(file, result)

    if result.get("status") == "STRONG":
        data["action_safety"] = "REVIEW"

    return result


def _resolve_shazam_result(file, data):
    try:
        from core.run_state import shazam_refresh_generation
        generation = shazam_refresh_generation(file, preferences)
        return (resolve_shazam_identity(file, data, refresh_generation=generation)
                if generation else resolve_shazam_identity(file, data))
    except Exception as error:
        return {'status': 'ERROR', 'best': None, 'attempts': [],
                'errors': [str(error)], 'verification_incomplete': True}


def _journal_shazam(file, result):
    _journal_stage(file, 'shazam_done', status=result.get('status'),
                   agreement_count=result.get('agreement_count'), errors=result.get('errors') or [],
                   fragments=[{'sample': attempt.get('sample'),
                               **{key: (attempt.get('recognition') or {}).get(key)
                                  for key in ('status', 'artist', 'title', 'shazam_id', 'isrc', 'error', 'cache_hit', 'cache_scope',
                                              'error_kind', 'http_status', 'deferred', 'retry_after_seconds',
                                              'rate_limit_retried')}}
                              for attempt in result.get('attempts') or []])


def try_shazam_fallback(file, data, *, prefetched_result=None):
    result = prefetched_result if prefetched_result is not None else _resolve_shazam_result(file, data)
    data['shazam_identity'] = result
    if result.get('status') != 'SKIPPED':
        best = result.get('best') or {}
        console.print(f"Shazam: {_rich(result.get('status'))}; "
                      f"{_rich(best.get('artist') or '—')} — {_rich(best.get('title') or '—')}; "
                      f"согласованных фрагментов: {result.get('agreement_count', 0)}.")
        if result.get('reason'):
            console.print(_rich(result['reason']))
    if prefetched_result is None:
        _journal_shazam(file, result)
    return result


def _resolve_catalog_result(file, data):
    try:
        return resolve_catalog_identity(
            file_path=file,
            data=data,
        )
    except Exception as error:
        return {
            "status": "ERROR",
            "best": None,
            "results": [],
            "reason": str(error),
            "errors": [],
        }


def try_catalog_search_fallback(
    file,
    data,
    *,
    run_acr=True,
    prefetched_result=None,
):
    result = (
        prefetched_result
        if prefetched_result is not None
        else _resolve_catalog_result(file, data)
    )

    data["catalog_identity"] = result
    print_catalog_identity(file, result)

    if result.get("status") == "STRONG":
        data["action_safety"] = "REVIEW"

    if run_acr:
        if result.get("status") == "STRONG":
            data["acrcloud_identity"] = {
                "status": "SKIPPED",
                "reason": "Catalog search уже дал STRONG identity.",
            }
        else:
            try_acrcloud_fallback(file, data)

    return result



def _resolve_acrcloud_musicbrainz_result(acr_result):
    """Pure ACRCloud STRONG + ISRC -> MusicBrainz result for prefetch."""
    acr_result = acr_result or {}
    best = acr_result.get("best") or {}

    if acr_result.get("status") != "STRONG":
        return {
            "status": "SKIPPED",
            "reason": "ACRCloud identity не STRONG.",
        }

    isrc = best.get("isrc") or ""
    if not isrc:
        return {
            "status": "NO_ISRC",
            "reason": "ACRCloud STRONG, но ISRC отсутствует.",
        }

    identity_payload = {
        "artist": best.get("artist") or "",
        "title": best.get("title") or "",
        "isrc": isrc,
    }

    try:
        return resolve_isrc_recording(isrc, identity_payload)
    except Exception as error:
        return {
            "status": "ERROR",
            "isrc": isrc,
            "recording_id": None,
            "recording": None,
            "reason": str(error),
        }


def resolve_acrcloud_musicbrainz_bridge(
    file,
    data,
    acr_result,
    *,
    prefetched_result=None,
):
    """ACRCloud STRONG + ISRC -> canonical MusicBrainz Recording."""
    result = (
        prefetched_result
        if prefetched_result is not None
        else _resolve_acrcloud_musicbrainz_result(acr_result)
    )

    data["acrcloud_musicbrainz"] = result

    console.print()
    console.print(
        "[bold bright_blue]"
        "ACRCloud → ISRC → MusicBrainz"
        "[/bold bright_blue]"
    )

    status = result.get("status") or "ERROR"
    if status == "RESOLVED":
        selected = result.get("selected") or {}
        console.print(
            "MusicBrainz по ISRC: [green]RESOLVED[/green]"
        )
        console.print(
            "Recording: "
            f"{_rich(selected.get('artist_credit') or '—')} — "
            f"{_rich(selected.get('title') or '—')}"
        )
        console.print(
            "[dim]"
            f"recording_id: {result.get('recording_id') or '—'}"
            "[/dim]"
        )
    else:
        console.print(
            f"MusicBrainz по ISRC: [yellow]{status}[/yellow]"
        )
        if result.get("reason"):
            console.print(
                f"Причина: {result.get('reason')}"
            )

    return result


def _fallback_none_release_selection(reason):
    return {
        "status": "NONE",
        "selected": None,
        "options": [],
        "equivalent_releases": [],
        "concrete_release_ambiguous": False,
        "reason": reason,
    }


def apply_fused_identity_plan(
    file,
    data,
    fusion,
    *,
    prefetched_mb=None,
):
    """Превращает consensus fallback в обычный change_plan."""

    fusion = fusion or {}
    status = fusion.get("status") or "NOT_FOUND"

    if status not in {"SAFE", "REVIEW"}:
        return False

    artist = fusion.get("artist") or ""
    title = fusion.get("title") or ""
    recording_id = fusion.get("recording_id")

    if not title:
        return False

    data["fallback_fusion"] = fusion

    if fusion.get("review_reasons"):
        console.print("[yellow]Автоматическая запись требует проверки: "
                      + _rich(" ".join(fusion["review_reasons"])) + "[/yellow]")

    console.print()
    status_style = "green" if status == "SAFE" else "yellow"
    console.print(
        f"[bold {status_style}]"
        f"Consensus fallback: {_rich(artist or '—')} — {_rich(title)}"
        f"[/bold {status_style}]"
    )
    console.print(
        f"Статус: {status}"
    )
    console.print(
        f"Причина: {_rich(fusion.get('reason') or '—')}"
    )

    if recording_id:
        score = fusion.get("confidence")
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.95 if status == "SAFE" else 0.85

        score = max(
            score,
            0.95 if status == "SAFE" else 0.80,
        )

        best_match = {
            "candidate": {
                "recording_id": recording_id,
                "title": title,
                "artists": [artist] if artist else [],
                'artist_credit_is_flat': True,
                "release_groups": [],
            },
            "score": min(score, 0.999),
            "acoustid_score": 0.0,
            "artist_similarity": 1.0 if artist else 0.0,
            "title_similarity": 1.0,
            "version_score": 1.0,
            "version_mismatch": False,
        }

        print_identification(
            file,
            data,
            [best_match],
            provider_label="Consensus fallback → MusicBrainz",
            forced_safety=status,
            score_label=(
                "Consensus confidence: "
                f"{score:.3f}"
            ),
            evidence_reason=fusion.get("reason"),
            prefetched_mb=prefetched_mb,
        )

        data["fallback_best_match"] = best_match
        return True

    # Без recording_id можно безопасно/через REVIEW исправлять только
    # Artist/Title. Album/Date/Track/Disc намеренно не выдумываем.
    release_selection = _fallback_none_release_selection(
        "Fallback identity подтверждена, но MusicBrainz recording_id отсутствует."
    )

    official_title = {
        "status": "RESOLVED_BY_RECORDING",
        "title": title,
        "source": "fallback_consensus",
        "variants": [],
    }

    best_match = {
        "candidate": {
            "recording_id": None,
            "title": title,
            "artists": [artist] if artist else [],
            "release_groups": [],
        },
        "score": fusion.get("confidence") or (0.95 if status == "SAFE" else 0.80),
        "acoustid_score": 0.0,
    }

    change_plan = build_change_plan(
        data=data,
        best_match=best_match,
        artist_credit=artist,
        official_title=official_title,
        releases=[],
        selected_release=None,
        safety=status,
        preferences=preferences,
        release_context_selection=release_selection,
    )

    data["action_safety"] = status
    data["metadata_agreement"] = "STRONG" if status == "SAFE" else "PARTIAL"
    data["best_match"] = best_match
    data["official_title"] = official_title
    data["release_context_selection"] = release_selection
    data["change_plan"] = change_plan

    return True


def run_secondary_identity_pipeline(
    file,
    data,
    *,
    fingerprint_data,
    acoustid_match=None,
    acoustid_safety=None,
    prefetched=None,
):
    """Каскад после AcoustID miss/REVIEW/REJECT.

    V12 can receive provider results that were resolved earlier in background
    workers. The same print/mutation/fusion path is still executed here, so
    scheduling changes do not change SAFE/REVIEW semantics.
    """
    prefetched = prefetched or {}

    source_result = try_source_url_fallback(
        file,
        data,
        prefetched_result=(
            prefetched.get("source")
            if "source" in prefetched
            else None
        ),
    )

    catalog_result = try_catalog_search_fallback(
        file,
        data,
        run_acr=False,
        prefetched_result=(
            prefetched.get("catalog")
            if "catalog" in prefetched
            else None
        ),
    )

    preliminary = fuse_identity(
        acoustid_match=acoustid_match,
        acoustid_safety=acoustid_safety,
        source_result=source_result,
        catalog_result=catalog_result,
    )

    acr_result = None
    acr_mb_result = None

    if (
        preliminary.get("status") != "SAFE"
        or not preliminary.get("recording_id")
    ):
        acr_result = try_acrcloud_fallback(
            file,
            data,
            prefetched_result=(
                prefetched.get("acrcloud")
                if "acrcloud" in prefetched
                else None
            ),
        )

        if acr_result.get("status") == "STRONG":
            acr_mb_result = resolve_acrcloud_musicbrainz_bridge(
                file,
                data,
                acr_result,
                prefetched_result=(
                    prefetched.get("acrcloud_musicbrainz")
                    if "acrcloud_musicbrainz" in prefetched
                    else None
                ),
            )

    fusion = fuse_identity(
        acoustid_match=acoustid_match,
        acoustid_safety=acoustid_safety,
        source_result=source_result,
        catalog_result=catalog_result,
        acr_result=acr_result,
        acr_mb_result=acr_mb_result,
    )

    shazam_result = prefetched.get('shazam')
    if shazam_result is not None or fusion.get('status') != 'SAFE':
        shazam_result = try_shazam_fallback(file, data, prefetched_result=shazam_result)
        fusion = fuse_identity(acoustid_match=acoustid_match, acoustid_safety=acoustid_safety,
                               source_result=source_result, catalog_result=catalog_result,
                               acr_result=acr_result, acr_mb_result=acr_mb_result, shazam_result=shazam_result)

    data["fallback_fusion"] = fusion

    # AudD remains LAST RESORT and is intentionally not speculatively prefetched.
    # This preserves its quota behavior exactly.
    audd_secondary = None
    audd_bridge = None

    if fusion.get("status") != "SAFE":
        audd_secondary = recognize_with_audd_only(
            file,
            data,
            fingerprint_data=fingerprint_data,
        )

        if (
            audd_secondary.get("status") == "FOUND"
            and audd_secondary.get("provider") == "audd"
        ):
            audd_result = audd_secondary.get("audd_result") or {}
            audd_match = audd_secondary.get("audd_match") or {}

            print_audd_fallback(
                file,
                data,
                audd_result,
                audd_match,
            )

            if audd_match.get("status") == "STRONG":
                audd_bridge = process_audd_musicbrainz_bridge(
                    file,
                    data,
                    audd_result,
                    audd_match,
                )

        elif (
            audd_secondary.get("status") == "FALLBACK_ERROR"
            and not audd_secondary.get("audd_skipped")
        ):
            print_fallback_error(
                file,
                audd_secondary.get("error"),
            )

    data["secondary_recognition"] = {
        "source": source_result,
        "catalog": catalog_result,
        "acrcloud": acr_result,
        "acrcloud_musicbrainz": acr_mb_result,
        "shazam": shazam_result,
        "fusion": fusion,
        "audd": audd_secondary,
        "audd_bridge": audd_bridge,
    }

    if not (
        audd_bridge
        and audd_bridge.get("status") == "RESOLVED"
    ):
        prior_safety = acoustid_safety or "REJECT"
        should_apply = (
            fusion.get("status") == "SAFE"
            or (
                fusion.get("status") == "REVIEW"
                and prior_safety == "REJECT"
            )
            or (
                fusion.get("status") == "REVIEW"
                and acoustid_match is None
            )
        )

        if should_apply:
            apply_fused_identity_plan(
                file,
                data,
                fusion,
                prefetched_mb=prefetched.get("mb_prefetch"),
            )

    return data["secondary_recognition"]


def print_recognition_not_found(

    file,
):
    console.print(
        f"[yellow]"
        f"Не распознано после AcoustID / AudD / source URL / catalog search / ACRCloud / Shazam: "
        f"{_rich(file.name)}"
        f"[/yellow]"
    )


def print_fallback_error(
    file,
    error,
):
    console.print(
        f"[yellow]"
        f"AudD fallback не выполнен для "
        f"{_rich(file.name)}: "
        f"{error or 'неизвестная ошибка'}"
        f"[/yellow]"
    )


# ============================================================
# ВЫВОД ACOUSTID / MUSICBRAINZ
# ============================================================

def _get_musicbrainz_release_bundle(recording_id):
    """Fetch the two release-derived views from one shared raw-release cache."""
    tracks = get_official_release_tracks(recording_id)
    contexts = get_official_release_contexts(recording_id)
    return {
        "tracks": tracks,
        "contexts": contexts,
    }


def print_identification(
    file,
    data,
    ranked_candidates,
    *,
    provider_label="AcoustID",
    forced_safety=None,
    score_label=None,
    evidence_reason=None,
    prefetched_mb=None,
):
    prefetched_mb = prefetched_mb or {}

    if not ranked_candidates:
        console.print(
            f"[yellow]"
            f"AcoustID не распознал: "
            f"{_rich(file.name)}"
            f"[/yellow]"
        )

        data["change_plan"] = (
            build_local_cleanup_plan(
                data
            )
        )

        return

    best_match = (
        ranked_candidates[0]
    )

    best = (
        best_match["candidate"]
    )

    confidence = confidence_level(
        best_match["score"]
    )

    agreement = metadata_agreement(
        best_match
    )

    safety = (
        forced_safety
        or action_safety(
            best_match
        )
    )

    data["metadata_agreement"] = (
        agreement
    )

    data["action_safety"] = (
        safety
    )

    data["best_match"] = (
        best_match
    )

    data["confidence"] = (
        confidence
    )

    # --------------------------------------------------------
    # ЦВЕТ УВЕРЕННОСТИ
    # --------------------------------------------------------

    if confidence == "HIGH":
        confidence_text = (
            "[green]ВЫСОКАЯ[/green]"
        )

    elif confidence == "MEDIUM":
        confidence_text = (
            "[yellow]СРЕДНЯЯ[/yellow]"
        )

    else:
        confidence_text = (
            "[red]НИЗКАЯ[/red]"
        )

    # --------------------------------------------------------
    # MUSICBRAINZ
    # --------------------------------------------------------

    _journal_stage(file, "musicbrainz_enrichment_started", recording_id=best.get("recording_id"))
    musicbrainz_data = None
    # V12.1: this value is shared by the official-title and ReleaseContext
    # branches. It must always exist even when no MB prefetch future was
    # scheduled, otherwise the later ReleaseContext branch can hit an
    # UnboundLocalError.
    release_bundle_data = None

    if (
        confidence != "LOW"
        and best.get(
            "recording_id"
        )
    ):
        try:
            recording_future = prefetched_mb.get("recording_future")
            if recording_future is not None:
                musicbrainz_data = recording_future.result()
            else:
                musicbrainz_data = get_recording(
                    best["recording_id"]
                )

            data["musicbrainz"] = musicbrainz_data

        except Exception as error:
            data["_checkpoint_retry_required"] = True
            data.setdefault("_checkpoint_retry_reasons", []).append(
                f"musicbrainz_recording: {error}"
            )
            console.print(
                "[yellow]"
                "MusicBrainz недоступен: "
                f"{error}"
                "[/yellow]"
            )

    official_release_tracks = []

    if (
        safety != "REJECT"
        and best.get(
            "recording_id"
        )
    ):
        try:
            release_future = prefetched_mb.get("release_future")
            if release_future is not None:
                release_bundle_data = release_future.result()
                official_release_tracks = (
                    release_bundle_data.get("tracks") or []
                )
            else:
                official_release_tracks = get_official_release_tracks(
                    best["recording_id"]
                )

            data["official_release_tracks"] = official_release_tracks

        except Exception as error:
            data["_checkpoint_retry_required"] = True
            data.setdefault("_checkpoint_retry_reasons", []).append(
                f"musicbrainz_release_tracks: {error}"
            )
            console.print(
                "[yellow]"
                "Не удалось получить "
                "официальные track titles: "
                f"{error}"
                "[/yellow]"
            )

    # --------------------------------------------------------
    # ОСНОВНАЯ ИНФОРМАЦИЯ
    # --------------------------------------------------------

    console.print()

    console.print(
        f"[bold cyan]"
        f"{provider_label}: {_rich(file.name)}"
        f"[/bold cyan]"
    )

    if provider_label == "AcoustID":
        console.print(
            "AcoustID score: "
            f"{float(best_match.get('acoustid_score') or 0.0):.3f}"
        )
    elif score_label:
        console.print(score_label)

    if evidence_reason:
        console.print(
            "Подтверждение: "
            f"{_rich(evidence_reason)}"
        )

    console.print(
        "Итоговая уверенность: "
        f"{best_match['score']:.3f} "
        f"({confidence_text})"
    )

    if agreement == "STRONG":
        agreement_text = (
            "[green]СИЛЬНОЕ[/green]"
        )

    elif agreement == "PARTIAL":
        agreement_text = (
            "[yellow]ЧАСТИЧНОЕ[/yellow]"
        )

    else:
        agreement_text = (
            "[red]КОНФЛИКТ[/red]"
        )


    if safety == "SAFE":
        safety_text = (
            "[green]БЕЗОПАСНО[/green]"
        )

    elif safety == "REVIEW":
        safety_text = (
            "[yellow]ПРОВЕРИТЬ[/yellow]"
        )

    else:
        safety_text = (
            "[red]НЕ ИСПРАВЛЯТЬ[/red]"
        )


    console.print(
        f"Совпадение метаданных: "
        f"{agreement_text}"
    )

    console.print(
        f"Автоисправление: "
        f"{safety_text}"
    )

    if best_match.get("identity_ambiguous"):
        console.print("[yellow]AcoustID: близкие аудио-кандидаты разных треков/версий. "
                      "Имя файла не разрешает эту неоднозначность автоматически.[/yellow]")
        for alternative in best_match.get("identity_alternatives") or []:
            console.print("  Альтернатива: " + _rich(", ".join(alternative.get("artists") or []))
                          + " — " + _rich(alternative.get("title") or "—")
                          + f" (AcoustID {float(alternative.get('score') or 0):.3f})")

    artists = (
        ", ".join(
            best.get(
                "artists",
                [],
            )
        )
        or "—"
    )

    console.print(
        f"Исполнитель: {_rich(artists)}"
    )

    console.print(
        "Название: "
        f"{_rich(best.get('title') or '—')}"
    )

    # --------------------------------------------------------
    # ARTIST CREDIT / ISRC
    # --------------------------------------------------------

    artist_credit = ""

    if musicbrainz_data:
        artist_credit = (
            format_artist_credit(
                musicbrainz_data
            )
        )

        if artist_credit:
            console.print(
                f"Artist credit: "
                f"{_rich(artist_credit)}"
            )

        isrcs = (
            musicbrainz_data.get(
                "isrcs",
                [],
            )
        )

        if isrcs:
            console.print(
                "ISRC: "
                + ", ".join(isrcs)
            )

    # --------------------------------------------------------
    # НИЗКАЯ УВЕРЕННОСТЬ
    # --------------------------------------------------------

    if confidence == "LOW":
        console.print(
            "[red]"
            "Кандидат недостаточно надёжен. "
            "Исправлять теги нельзя."
            "[/red]"
        )

    # --------------------------------------------------------
    # РЕЛИЗЫ
    # --------------------------------------------------------

    musicbrainz_releases = []

    if musicbrainz_data:
        musicbrainz_releases = (
            extract_release_groups(
                musicbrainz_data
            )
        )


    # MusicBrainz теперь основной источник
    # списка релизов.
    #
    # Если он ничего не вернул —
    # используем старые данные AcoustID.
    if musicbrainz_releases:
        raw_releases = (
            musicbrainz_releases
        )

    else:
        raw_releases = (
            best.get(
                "release_groups",
                [],
            )
        )

    if preferences.get(
        "normalize_names",
        True,
    ):
        releases = (
            prepare_releases(
                raw_releases
            )
        )

    else:
        releases = list(
            raw_releases
        )

    # Автовыбор допустим только
    # при HIGH confidence.
    if safety == "SAFE":
        selected_release = (
            select_release(
                releases,
                preferences.get(
                    "release_preference",
                    "ask",
                ),
            )
        )

    else:
        selected_release = None

        # ========================================================
    # RELEASE CONTEXT V2
    #
    # Пока работает ТОЛЬКО READ-ONLY.
    #
    # selected_release ниже остаётся старым
    # Release Group и продолжает обслуживать
    # старый change_plan.
    # ========================================================

    release_contexts = []

    release_context_selection = {
        "status": "NONE",
        "selected": None,
        "options": [],
        "equivalent_releases": [],
        "concrete_release_ambiguous": False,
        "reason": (
            "ReleaseContext v2 "
            "не запускался."
        ),
    }

    selected_release_context = None

    recording_id = (
        best.get(
            "recording_id"
        )
    )

    if (
        safety != "REJECT"
        and recording_id
    ):
        try:
            if release_bundle_data is not None:
                release_contexts = (
                    release_bundle_data.get("contexts") or []
                )
            else:
                release_future = prefetched_mb.get("release_future")
                if release_future is not None:
                    release_bundle_data = release_future.result()
                    release_contexts = (
                        release_bundle_data.get("contexts") or []
                    )
                else:
                    release_contexts = get_official_release_contexts(
                        recording_id
                    )

            release_context_selection = (
                select_release_context(
                    contexts=(
                        release_contexts
                    ),

                    preference=(
                        preferences.get(
                            "release_preference",
                            "ask",
                        )
                    ),

                    current_album=(
                        data.get(
                            "album"
                        )
                        or ""
                    ),

                    safety=safety,
                )
            )

            selected_release_context = (
                release_context_selection.get(
                    "selected"
                )
            )

        except Exception as error:
            data["_checkpoint_retry_required"] = True
            data.setdefault("_checkpoint_retry_reasons", []).append(
                f"release_context: {error}"
            )
            console.print(
                "[yellow]"
                "ReleaseContext v2 "
                "недоступен: "
                f"{error}"
                "[/yellow]"
            )

    data[
        "release_contexts"
    ] = release_contexts

    data[
        "release_context_selection"
    ] = release_context_selection

    data[
        "selected_release_context"
    ] = selected_release_context

    release_metadata_preview = (
        build_release_metadata_preview(
            data=data,

            selected_release_context=(
                selected_release_context
            ),

            release_context_selection=(
                release_context_selection
            ),
        )
    )

    data[
        "release_metadata_preview"
    ] = release_metadata_preview

    official_title = (
        resolve_official_track_title(
            official_release_tracks,

            recording_title=(
                best.get("title")
            ),

            selected_release=(
                selected_release
            ),
        )
    )

    data[
        "official_title"
    ] = official_title

    data["possible_releases"] = (
        releases
    )

    data["selected_release"] = (
        selected_release
    )

    if releases:
        console.print(
            "Возможные релизы:"
        )

        for release in releases:
            if (
                selected_release
                and release
                == selected_release
            ):
                marker = "→"

            else:
                marker = " "

            release_type = (
                release.get("type")
                or "тип не указан"
            )

            secondary_types = (
                release.get(
                    "secondary_types",
                    [],
                )
            )

            if secondary_types:
                type_text = (
                    release_type
                    + " / "
                    + ", ".join(
                        secondary_types
                    )
                )

            else:
                type_text = release_type


            console.print(
                f"{marker} - "
                f"{_rich(release.get('title') or '—')} "
                f"({type_text})"
            )

        if selected_release is None:
            if safety != "SAFE":
                console.print(
                    "[yellow]"
                    "Релиз не выбран: "
                    "метаданные требуют проверки."
                    "[/yellow]"
                )

            elif len(releases) > 1:
                console.print(
                    "[yellow]"
                    "Релиз автоматически "
                    "не выбран."
                    "[/yellow]"
                )

    print_release_context_v2(contexts=(release_contexts), selection=(release_context_selection))

    print_release_metadata_preview(release_metadata_preview)

    if (
        official_title[
            "status"
        ]
        in {
            "RESOLVED",
            "RESOLVED_BY_RECORDING",
        }
    ):
        if (
            official_title.get("source")
            == "selected_release_group"
        ):
            console.print(
                "[green]"
                "Название на выбранном релизе: "
                "[/green]"
                f"{_rich(official_title['title'])}"
            )

        else:
            console.print(
                "[green]"
                "Каноническое название "
                "по официальным релизам: "
                "[/green]"
                f"{_rich(official_title['title'])}"
            )

        variants = (
            official_title.get(
                "variants",
                [],
            )
        )

        if len(variants) > 1:
            console.print(
                "Официальные варианты названия:"
            )

            for variant in (
                variants[:5]
            ):
                console.print(
                    "  - "
                    f"{_rich(variant['title'])} "
                    f"["
                    f"{variant['release_group_count']} "
                    f"release group"
                    f"]"
                )

    elif (
        official_title[
            "status"
        ]
        == "AMBIGUOUS"
    ):
        console.print(
            "[yellow]"
            "На официальных релизах "
            "используются разные названия:"
            "[/yellow]"
        )

        for variant in (
            official_title.get(
                "variants",
                [],
            )[:5]
        ):
            console.print(
                "  - "
                f"{_rich(variant['title'])} "
                f"["
                f"{variant['release_group_count']} "
                f"release group"
                f"]"
            )

    elif (
        official_title[
            "status"
        ]
        == "NO_OFFICIAL_TRACKS"
    ):
        console.print(
            "[dim]"
            "Официальное название по "
            "релизам MusicBrainz "
            "не найдено."
            "[/dim]"
        )

    change_plan = build_change_plan(
        data=data,
        best_match=best_match,
        artist_credit=artist_credit,
        official_title=official_title,
        releases=releases,
        selected_release=selected_release,
        safety=safety,
        preferences=preferences,
        release_context_selection=(
            release_context_selection
        ),
    )

    data["change_plan"] = (
        change_plan
    )


def format_change_field(field):
    if not field:
        return "—"

    status = (
        field.get("status")
        or "KEEP"
    )

    current = (
        field.get("current")
        or ""
    )

    proposed = (
        field.get("proposed")
    )

    options = (
        field.get("options")
        or []
    )

    current_text = (
        _rich(current)
        if current
        else "—"
    )

    proposed_text = (
        _rich(proposed)
        if proposed is not None
        else ""
    )

    if status == "KEEP":
        return (
            "[dim]"
            "без изменений"
            "[/dim]"
        )

    if status == "BLOCKED":
        return (
            "[red]"
            "заблокировано"
            "[/red]"
        )

    if status == "SAFE":
        if proposed:
            return (
                "[green]SAFE[/green]\n"
                f"{current_text}\n"
                f"→ {proposed_text}"
            )

        return "[green]SAFE[/green]"

    if status == "REVIEW":
        if proposed:
            return (
                "[yellow]"
                "ПРОВЕРИТЬ"
                "[/yellow]\n"
                f"{current_text}\n"
                f"→ {proposed_text}"
            )

        return (
            "[yellow]"
            "ПРОВЕРИТЬ"
            "[/yellow]"
        )

    if status == "ASK":
        lines = [
            "[cyan]ВЫБОР[/cyan]"
        ]

        if current:
            lines.append(
                f"сейчас: {_rich(current)}"
            )

        if options:
            for option in options[:3]:
                lines.append(
                    f"• {_rich(option)}"
                )

            if len(options) > 3:
                lines.append(
                    f"… ещё "
                    f"{len(options) - 3}"
                )

        return "\n".join(lines)

    return str(status)


def print_change_preview(
    results,
):
    table = Table(
        title=(
            "TagCleaner — "
            "предпросмотр изменений"
        )
    )

    table.add_column(
        "Файл",
        width=30,
    )

    table.add_column(
        "Artist",
        width=24,
    )

    table.add_column(
        "Title",
        width=30,
    )

    table.add_column(
        "Album",
        width=26,
    )

    table.add_column(
        "Album Artist",
        width=22,
    )

    table.add_column(
        "Date",
        width=14,
    )

    table.add_column(
        "Track",
        width=12,
    )

    table.add_column(
        "Disc",
        width=12,
    )

    table.add_column(
        "Cover",
        width=26,
    )

    safe_count = 0
    review_count = 0
    ask_count = 0
    blocked_count = 0

    rows_added = 0

    for data in results:
        plan = data.get(
            "change_plan"
        )

        if not plan:
            continue

        fields = [
            plan.get("artist"),
            plan.get("title"),
            plan.get("album"),
            plan.get(
                "albumartist"
            ),
            plan.get("date"),
            plan.get(
                "tracknumber"
            ),
            plan.get(
                "discnumber"
            ),
            plan.get("cover"),
        ]

        statuses = {
            field.get("status")
            for field in fields
            if field
        }

        # Полностью чистые строки
        # в preview не показываем.
        if statuses <= {"KEEP"}:
            continue

        # -----------------------------------------
        # СЧЁТЧИКИ СТАТУСОВ
        # -----------------------------------------

        for field in fields:
            if not field:
                continue

            status = field.get(
                "status"
            )

            if status == "SAFE":
                safe_count += 1

            elif status == "REVIEW":
                review_count += 1

            elif status == "ASK":
                ask_count += 1

            elif status == "BLOCKED":
                blocked_count += 1

        # -----------------------------------------
        # ОДНА СТРОКА НА ОДИН ФАЙЛ
        #
        # ВАЖНО:
        # этот add_row находится УЖЕ ПОСЛЕ
        # цикла "for field in fields".
        # -----------------------------------------

        table.add_row(
            _rich(data.get(
                "file",
                "—",
            )),

            format_change_field(
                plan.get("artist")
            ),

            format_change_field(
                plan.get("title")
            ),

            format_change_field(
                plan.get("album")
            ),

            format_change_field(
                plan.get(
                    "albumartist"
                )
            ),

            format_change_field(
                plan.get("date")
            ),

            format_change_field(
                plan.get(
                    "tracknumber"
                )
            ),

            format_change_field(
                plan.get(
                    "discnumber"
                )
            ),

            format_change_field(
                plan.get("cover")
            ),
        )

        rows_added += 1

    console.print()

    if rows_added:
        console.print(
            table
        )

    else:
        console.print(
            "[green]"
            "Предлагаемых изменений нет."
            "[/green]"
        )

    console.print()

    console.print(
        "[bold]"
        "Поля предпросмотра:"
        "[/bold]"
    )

    console.print(
        f"  [green]SAFE:[/green] "
        f"{safe_count}"
    )

    console.print(
        f"  [yellow]"
        f"ПРОВЕРИТЬ:"
        f"[/yellow] "
        f"{review_count}"
    )

    console.print(
        f"  [cyan]ВЫБОР:[/cyan] "
        f"{ask_count}"
    )

    console.print(
        f"  [red]"
        f"ЗАБЛОКИРОВАНО:"
        f"[/red] "
        f"{blocked_count}"
    )

# ============================================================
# ОСНОВНАЯ ТАБЛИЦА
# ============================================================

def print_library_table(
    results,
):
    table = Table(
        title="TagCleaner — библиотека"
    )

    table.add_column(
        "#",
        justify="right",
    )

    table.add_column(
        "Файл",
    )

    table.add_column(
        "Исполнитель",
    )

    table.add_column(
        "Название",
    )

    table.add_column(
        "Проблемы",
        justify="right",
    )

    for index, data in enumerate(
        results,
        start=1,
    ):
        issues = data.get(
            "issues",
            [],
        )

        if issues:
            problems = (
                f"[yellow]"
                f"{len(issues)}"
                f"[/yellow]"
            )

        else:
            problems = (
                "[green]OK[/green]"
            )

        table.add_row(
            str(index),
            _rich(data.get(
                "file",
                "—",
            )),
            _rich(data.get(
                "artist",
                "",
            )
            or "—"),
            _rich(data.get(
                "title",
                "",
            )
            or "—"),
            problems,
        )

    console.print()
    console.print(
        table
    )


# ============================================================
# ПОДРОБНЫЙ АУДИТ
# ============================================================

def print_audit_details(
    results,
):
    for data in results:
        issues = data.get(
            "issues",
            [],
        )

        if not issues:
            continue

        console.print()

        console.print(
            f"[bold cyan]"
            f"{_rich(data.get('file', '—'))}"
            f"[/bold cyan]"
        )

        table = Table()

        table.add_column(
            "Уровень",
            width=10,
        )

        table.add_column(
            "Проблема",
        )

        table.add_column(
            "Предложение",
        )

        for issue in issues:
            level = (
                issue.get("level")
                or issue.get("severity")
                or "WARNING"
            )

            level_upper = (
                str(level).upper()
            )

            if level_upper == "ERROR":
                level_text = (
                    "[red]ERROR[/red]"
                )

            elif level_upper == "INFO":
                level_text = (
                    "[cyan]INFO[/cyan]"
                )

            else:
                level_text = (
                    "[yellow]"
                    f"{level_upper}"
                    "[/yellow]"
                )

            table.add_row(
                level_text,
                _rich(
                    issue.get("problem")
                    or issue.get("message")
                    or issue.get("code")
                    or "—"
                ),
                _rich(
                    issue.get("suggestion")
                    or "—"
                ),
            )

        console.print(
            table
        )


# ============================================================
# ОТЧЁТ
# ============================================================

def save_report():
    if run_journal is not None:
        run_journal.sync()
        console.print(f"\nОтчёт сохранён: {_rich(run_journal.report_path)}")
        return run_journal.report_path

    reports_dir = (
        ROOT_DIR
        / "reports"
    )

    reports_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = (
        datetime.now().strftime(
            "%Y-%m-%d_%H-%M-%S"
        )
    )

    report_path = (
        reports_dir
        / (
            "TagCleaner_report_"
            f"{timestamp}.txt"
        )
    )

    console.save_text(
        str(report_path),
        clear=False,
        styles=False,
    )

    console.print(
        "\n[green]"
        "Отчёт сохранён:"
        "[/green] "
        f"{_rich(report_path)}"
    )


def print_writer_dry_run(
    operations,
):
    console.print()

    console.print(
        "[bold cyan]"
        "TagCleaner Writer — DRY RUN"
        "[/bold cyan]"
    )

    console.print(
        "[dim]"
        "Файлы НЕ изменяются."
        "[/dim]"
    )

    if not operations:
        console.print(
            "[green]"
            "Безопасных операций "
            "для записи нет."
            "[/green]"
        )
        return

    table = Table(
        title=(
            "Что было бы записано"
        )
    )

    table.add_column(
        "Файл",
        width=38,
    )

    table.add_column(
        "Поле",
        width=10,
    )

    table.add_column(
        "Было",
        width=32,
    )

    table.add_column(
        "Стало",
        width=32,
    )

    changed_files = set()

    for operation in operations:
        changed_files.add(
            operation["path"]
        )

        table.add_row(
            _rich(operation["file"]),
            _rich(operation["field"]),
            _rich(
                operation["current"]
                or "—"
            ),
            _rich(operation["proposed"]),
        )

    console.print(table)

    console.print(
        f"\nSAFE-операций: "
        f"[green]"
        f"{len(operations)}"
        f"[/green]"
    )

    console.print(
        f"Затронутых файлов: "
        f"[green]"
        f"{len(changed_files)}"
        f"[/green]"
    )

    console.print(
        "[yellow]"
        "Запись на диск отключена."
        "[/yellow]"
    )


def choose_write_mode():
    console.print()

    console.print(
        "[bold]"
        "Что делать с найденными "
        "исправлениями?"
        "[/bold]"
    )

    console.print(
        "1 — только DRY RUN"
    )

    console.print(
        "2 — записать только SAFE"
    )

    console.print(
        "3 — интерактивная проверка"
    )

    while True:
        choice = (
            input(
                "Выбор: "
            )
            .strip()
        )

        if choice in {
            "1",
            "2",
            "3",
        }:
            return choice

        console.print(
            "[yellow]"
            "Введите 1, 2 или 3."
            "[/yellow]"
        )


def confirm_real_write(
    operations,
):
    if not operations:
        return False

    files = {
        operation["path"]
        for operation
        in operations
    }

    console.print()

    console.print(
        "[bold yellow]"
        "ПОДТВЕРЖДЕНИЕ ЗАПИСИ"
        "[/bold yellow]"
    )

    console.print(
        f"Будет изменено полей: "
        f"[bold]"
        f"{len(operations)}"
        f"[/bold]"
    )

    console.print(
        f"Будет изменено файлов: "
        f"[bold]"
        f"{len(files)}"
        f"[/bold]"
    )

    console.print(
        "[green]"
        "Перед изменением каждого файла "
        "будет создана полная резервная копия."
        "[/green]"
    )

    console.print()

    console.print(
        "Для подтверждения введите:"
    )

    console.print(
        "[bold]"
        "WRITE"
        "[/bold]"
    )

    value = (
        input(
            "> "
        )
        .strip()
    )

    return value == "WRITE"


def print_write_results(
    write_results,
):
    console.print()

    table = Table(
        title=(
            "Результат записи тегов"
        )
    )

    table.add_column(
        "Файл",
        width=42,
    )

    table.add_column(
        "Статус",
        width=14,
    )

    table.add_column(
        "Backup",
        width=42,
    )

    table.add_column(
        "Ошибка",
        width=40,
    )

    success_count = 0
    error_count = 0

    for result in write_results:
        path = Path(
            result["path"]
        )

        if result.get(
            "success"
        ):
            status = (
                "[green]OK[/green]"
            )

            success_count += 1

        else:
            status = (
                "[red]ОШИБКА[/red]"
            )

            error_count += 1

        table.add_row(
            _rich(path.name),
            status,
            _rich(
                result.get("backup")
                or "—"
            ),
            _rich(
                result.get("error")
                or "—"
            ),
        )

    console.print(table)

    console.print(
        f"\nУспешно: "
        f"[green]"
        f"{success_count}"
        f"[/green]"
    )

    console.print(
        f"Ошибок: "
        f"[red]"
        f"{error_count}"
        f"[/red]"
    )


def print_extra_tag_audit(
    results,
):
    rows = []

    for data in results:
        for finding in (
            data.get(
                "tag_findings"
            )
            or []
        ):
            status = finding.get(
                "status"
            )

            if status in {
                "KEEP",
                "CORE",
            }:
                continue

            rows.append(
                (
                    data,
                    finding,
                )
            )

    if not rows:
        return

    console.print()

    table = Table(
        title=(
            "TagCleaner — "
            "дополнительные теги"
        )
    )

    table.add_column(
        "Файл",
        width=36,
    )

    table.add_column(
        "Поле",
        width=20,
    )

    table.add_column(
        "Тип",
        width=18,
    )

    table.add_column(
        "Значение",
        width=44,
    )

    table.add_column(
        "Решение",
        width=20,
    )

    for data, finding in rows:
        values = " | ".join(
            finding.get(
                "values"
            )
            or []
        )

        # Чтобы огромный comment
        # не разносил таблицу.
        if len(values) > 180:
            values = (
                values[:177]
                + "..."
            )

        status = finding.get(
            "status"
        )

        if (
            status
            == "SAFE_DELETE"
        ):
            shown_status = (
                "[green]"
                "SAFE DELETE"
                "[/green]"
            )

        elif (
            status
            == "RELEASE_DEPENDENT"
        ):
            shown_status = (
                "[cyan]"
                "ПО РЕЛИЗУ"
                "[/cyan]"
            )

        elif (
            status
            == "SOURCE_REFERENCE"
        ):
            shown_status = (
                "[magenta]"
                "ИСТОЧНИК"
                "[/magenta]"
            )

        else:
            shown_status = (
                "[yellow]"
                "ПРОВЕРИТЬ"
                "[/yellow]"
            )

        table.add_row(
            _rich(data.get(
                "file",
                "—",
            )),
            _rich(finding.get(
                "raw_key",
                "—",
            )),
            _rich(finding.get(
                "logical",
                "unknown",
            )),
            _rich(values or "—"),
            shown_status,
        )

    console.print(
        table
    )

# ============================================================
# V12 NETWORK PREFETCH
# ============================================================

def _schedule_mb_prefetch_for_match(pipeline, best_match, forced_safety=None):
    best_match = best_match or {}
    best = best_match.get("candidate") or {}
    recording_id = best.get("recording_id")
    if not recording_id:
        return {}

    try:
        confidence = confidence_level(float(best_match.get("score") or 0.0))
    except Exception:
        confidence = "LOW"

    try:
        safety = forced_safety or action_safety(best_match)
    except Exception:
        safety = forced_safety or "REJECT"

    result = {}
    if confidence != "LOW":
        result["recording_future"] = pipeline.submit_mb_recording(
            recording_id,
            get_recording,
        )
    if safety != "REJECT":
        result["release_future"] = pipeline.submit_mb_release_bundle(
            recording_id,
            _get_musicbrainz_release_bundle,
        )
    return result


def _schedule_mb_prefetch_for_fusion(pipeline, fusion):
    fusion = fusion or {}
    recording_id = fusion.get("recording_id")
    status = fusion.get("status") or "NOT_FOUND"
    if not recording_id or status not in {"SAFE", "REVIEW"}:
        return {}

    return {
        "recording_future": pipeline.submit_mb_recording(
            recording_id,
            get_recording,
        ),
        "release_future": pipeline.submit_mb_release_bundle(
            recording_id,
            _get_musicbrainz_release_bundle,
        ),
    }


def _prefetch_secondary_evidence(
    file,
    data,
    *,
    pipeline,
    acoustid_match=None,
    acoustid_safety=None,
):
    """Resolve the same secondary providers early, without printing/mutating."""
    started = time.monotonic()

    source_result = load_stage_result(file, preferences, "source")
    catalog_result = load_stage_result(file, preferences, "catalog")
    source_future = (pipeline.submit_source(_resolve_source_result, file, data)
                     if source_result is None else None)
    catalog_future = (pipeline.submit_catalog(_resolve_catalog_result, file, data)
                      if catalog_result is None else None)
    if source_future is not None:
        source_result = source_future.result()
        save_stage_result(file, preferences, "source", source_result)
    if catalog_future is not None:
        catalog_result = catalog_future.result()
        save_stage_result(file, preferences, "catalog", catalog_result)
    _journal_stage(file, "source_catalog_done", source=source_result.get("status"), catalog=catalog_result.get("status"),
                   catalog_errors=catalog_result.get("errors") or [])

    preliminary = fuse_identity(
        acoustid_match=acoustid_match,
        acoustid_safety=acoustid_safety,
        source_result=source_result,
        catalog_result=catalog_result,
    )

    acr_result = None
    acr_mb_result = None

    if (
        preliminary.get("status") != "SAFE"
        or not preliminary.get("recording_id")
    ):
        acr_result = load_stage_result(file, preferences, "acrcloud")
        if acr_result is None:
            acr_result = pipeline.submit_acrcloud(_resolve_acrcloud_result, file, data).result()
            save_stage_result(file, preferences, "acrcloud", acr_result)
        _journal_stage(file, "acrcloud_done", status=acr_result.get("status"),
                       errors=acr_result.get('errors') or [],
                       fragments=[{'position': attempt.get('position'),
                                   **{key: (attempt.get('recognition') or {}).get(key)
                                      for key in ('status', 'artist', 'title', 'score', 'error', 'status_code',
                                                  'request_attempts', 'rate_limit_retries', 'request_history', 'cache_hit',
                                                  'pcm_fallback', 'initial_status_code', 'sample')}}
                                  for attempt in acr_result.get('attempts') or []])

        if acr_result.get("status") == "STRONG":
            # resolve_isrc_recording uses the shared, thread-safe MusicBrainz
            # limiter, so this can wait here while other providers keep moving.
            acr_mb_result = _resolve_acrcloud_musicbrainz_result(acr_result)

    fusion = fuse_identity(
        acoustid_match=acoustid_match,
        acoustid_safety=acoustid_safety,
        source_result=source_result,
        catalog_result=catalog_result,
        acr_result=acr_result,
        acr_mb_result=acr_mb_result,
    )

    shazam_result = load_stage_result(file, preferences, 'shazam')
    if shazam_result is None and fusion.get('status') != 'SAFE':
        shazam_result = (pipeline.submit_shazam(_resolve_shazam_result, file, data).result()
                         if shazam_available() else _resolve_shazam_result(file, data))
        save_stage_result(file, preferences, 'shazam', shazam_result)
    if shazam_result is not None:
        _journal_shazam(file, shazam_result)
        fusion = fuse_identity(acoustid_match=acoustid_match, acoustid_safety=acoustid_safety,
                               source_result=source_result, catalog_result=catalog_result,
                               acr_result=acr_result, acr_mb_result=acr_mb_result, shazam_result=shazam_result)

    return {
        "source": source_result,
        "catalog": catalog_result,
        "acrcloud": acr_result,
        "acrcloud_musicbrainz": acr_mb_result,
        "shazam": shazam_result,
        "fusion": fusion,
        "mb_prefetch": _schedule_mb_prefetch_for_fusion(
            pipeline,
            fusion,
        ),
        "prefetch_seconds": time.monotonic() - started,
    }


def _prefetch_file_network(file, data, fingerprint_future, pipeline):
    """Warm AcoustID, secondary providers and MusicBrainz ahead of main()."""
    started = time.monotonic()

    fingerprint_error = None
    try:
        fingerprint_data = fingerprint_future.result()
    except Exception as error:
        fingerprint_data = None
        fingerprint_error = str(error)
        _journal_stage(file, "fingerprint_failed", error=fingerprint_error)

    recognition = load_stage_result(file, preferences, "recognition")
    if fingerprint_data is not None:
        _journal_stage(file, "fingerprint_done", cache_hit=bool(fingerprint_data.get("cache_hit")),
                       **{key: fingerprint_data.get(key) for key in ("decode_fallback", "decode_partial", "decode_verification_incomplete", "decode_validation", "decoder", "decoded_duration", "reference_duration", "initial_decoder_error", "decode_warnings")})
    if recognition is None:
        try:
            recognition = recognize_with_fallback(
                file, data, fingerprint_data=fingerprint_data, allow_audd=False,
                primary_fingerprint_error=fingerprint_error,
            )
        except Exception as error:
            recognition = {"status": "ERROR", "provider": None, "error": str(error)}
        save_stage_result(file, preferences, "recognition", recognition)
    _journal_stage(file, "recognition_done", status=recognition.get("status"), provider=recognition.get("provider"))
    if recognition.get('acoustid_multipass'):
        _journal_stage(file, 'acoustid_multipass_done', **recognition['acoustid_multipass'])

    recognition_status = recognition.get("status") or "ERROR"
    recognition_provider = recognition.get("provider")
    ranked_candidates = recognition.get("ranked_candidates") or []

    acoustid_match = None
    acoustid_safety = None
    mb_prefetch = {}
    secondary = None

    if (
        recognition_status == "FOUND"
        and recognition_provider == "acoustid"
        and ranked_candidates
    ):
        acoustid_match = ranked_candidates[0]
        try:
            acoustid_safety = action_safety(acoustid_match)
        except Exception:
            acoustid_safety = "REJECT"

        mb_prefetch = _schedule_mb_prefetch_for_match(
            pipeline,
            acoustid_match,
            forced_safety=acoustid_safety,
        )

        if acoustid_safety != "SAFE":
            secondary = _prefetch_secondary_evidence(
                file,
                data,
                pipeline=pipeline,
                acoustid_match=acoustid_match,
                acoustid_safety=acoustid_safety,
            )
    else:
        secondary = _prefetch_secondary_evidence(
            file,
            data,
            pipeline=pipeline,
        )

    return {
        "fingerprint_error": fingerprint_error,
        "fingerprint_data": fingerprint_data,
        "recognition": recognition,
        "secondary": secondary,
        "mb_prefetch": mb_prefetch,
        "prefetch_seconds": time.monotonic() - started,
    }


def _recover_prefetch_failure(file, fingerprint_future, error):
    """A later worker failure must not erase the completed audio stage."""
    fingerprint_error = None
    try:
        fingerprint_data = fingerprint_future.result()
    except Exception as audio_error:
        fingerprint_data = None
        fingerprint_error = str(audio_error)
    recognition = load_stage_result(file, preferences, "recognition")
    if recognition is None:
        recognition = {"status": "ERROR", "provider": None,
                       "error": f"Не завершён этап распознавания: {error}"}
    _journal_stage(file, "prefetch_failed", error=str(error),
                   error_type=type(error).__name__, traceback=traceback.format_exc(),
                   fingerprint_preserved=bool(fingerprint_data),
                   recognition_preserved=recognition.get("status") != "ERROR")
    return {"fingerprint_error": fingerprint_error, "fingerprint_data": fingerprint_data,
            "recognition": recognition, "secondary": None, "mb_prefetch": {},
            "prefetch_error": str(error)}


# ============================================================
# ОСНОВНАЯ РАБОТА
# ============================================================

def main():
    global active_pipeline, active_fingerprint_executor
    files = choose_input()

    if not files:
        console.print(
            "[yellow]"
            "Аудиофайлы не найдены."
            "[/yellow]"
        )

        return

    console.print()

    console.print(
        "[bold]"
        f"Найдено аудиофайлов: "
        f"{len(files)}"
        "[/bold]"
    )

    results = []
    checkpoint_context = activate_checkpoint_run(files, preferences,
                                                 recheck=requested_checkpoint_mode in {'recheck', 'recheck_cached'},
                                                 refresh_shazam=requested_checkpoint_mode == 'recheck')
    _journal_stage(None, "checkpoint_mode_selected", **checkpoint_context)
    console.print("Начата повторная проверка всех выбранных файлов; новый прогресс сохраняется."
                  if requested_checkpoint_mode in {'recheck', 'recheck_cached'} else "Продолжаем сохранённую проверку выбранных файлов.")
    if checkpoint_context.get('shazam_refresh_files'):
        console.print('Shazam: ответы до начала этой проверки не используются. '
                      'Уже полученные ответы этой проверки сохраняются для продолжения.')

    read_errors = 0
    fingerprint_count = 0
    fingerprint_cache_hit_count = 0
    resumed_count = 0

    acoustid_found_count = 0
    audd_strong_count = 0
    audd_partial_count = 0
    audd_conflict_count = 0
    audd_mb_resolved_count = 0
    audd_mb_no_isrc_count = 0
    audd_mb_not_found_count = 0
    audd_mb_ambiguous_count = 0
    audd_mb_conflict_count = 0
    audd_mb_error_count = 0
    credit_consensus_count = 0
    credit_partial_count = 0
    credit_conflict_count = 0
    recognition_not_found_count = 0
    fallback_error_count = 0
    recognition_error_count = 0

    source_strong_count = 0
    source_partial_count = 0
    source_conflict_count = 0
    source_ambiguous_count = 0
    source_no_source_count = 0
    source_error_count = 0

    catalog_strong_count = 0
    catalog_partial_count = 0
    catalog_conflict_count = 0
    catalog_ambiguous_count = 0
    catalog_not_found_count = 0
    catalog_error_count = 0

    # --------------------------------------------------------
    # PERFORMANCE / RESUME / NETWORK PIPELINE V12
    #
    # 1) V11 checkpoints are loaded BEFORE any fpcalc/network work.
    # 2) Only unfinished files are scanned and submitted.
    # 3) AcoustID + source/catalog + conditional ACR + MB enrichment for later
    #    files run in the background while the main thread finalizes earlier
    #    files in the original order.
    # 4) AudD remains non-speculative LAST RESORT to preserve quota behavior.
    # --------------------------------------------------------

    prune_old_checkpoints()

    fingerprint_workers = fingerprint_worker_count()
    fingerprint_executor = ThreadPoolExecutor(
        max_workers=fingerprint_workers,
        thread_name_prefix="tagcleaner-fp",
    )
    network_pipeline = NetworkPipeline()
    active_pipeline = network_pipeline
    active_fingerprint_executor = fingerprint_executor

    resumed_results = {}
    prepared_data = {}
    fingerprint_futures = {}
    prefetch_futures = {}
    read_error_files = set()
    prefetch_started = time.monotonic()
    _journal_stage(None, "prepare_started", total=len(files))

    # --------------------------------------------------------
    # PREPARE + SUBMIT
    # --------------------------------------------------------

    for file in files:
        file = Path(file)

        cached_result = load_completed_result(
            file,
            preferences,
        )

        if cached_result is not None:
            resumed_results[file] = cached_result
            resumed_count += 1
            _journal_stage(file, "checkpoint_restored",
                           identity=(cached_result.get("change_plan") or {}).get("identity"),
                           warnings=cached_result.get("track_diagnostics") or [])

            if resumed_count == 1 or resumed_count % 250 == 0:
                console.print(
                    "[dim]"
                    f"Resume: восстановлено из checkpoint {resumed_count} файлов..."
                    "[/dim]"
                )
            continue

        try:
            data = scan_file(file)
        except Exception as error:
            read_errors += 1
            read_error_files.add(file)
            console.print(
                "[red]"
                f"Ошибка чтения {_rich(file.name)}: {error}"
                "[/red]"
            )
            continue

        try:
            issues = audit_tags(data)
        except Exception as error:
            console.print(
                "[red]"
                f"Ошибка аудита {_rich(file.name)}: {error}"
                "[/red]"
            )
            issues = []

        data["issues"] = issues
        prepared_data[file] = data
        _journal_stage(file, "scanned", issues=len(issues))

        fp_future = fingerprint_executor.submit(
            generate_fingerprint,
            file,
        )
        fingerprint_futures[file] = fp_future

        prefetch_futures[file] = network_pipeline.submit_file(
            _prefetch_file_network,
            file,
            data,
            fp_future,
            network_pipeline,
        )

    pending_count = len(prefetch_futures)
    console.print(
        "[dim]"
        f"V13.10 pipeline: resume={resumed_count}, pending={pending_count}; "
        f"fpcalc={fingerprint_workers}, {network_pipeline.settings_text()}."
        "[/dim]"
    )
    if resumed_count:
        console.print(
            "[dim]"
            "Совместимые завершённые checkpoint не запускают fpcalc/сеть повторно."
            "[/dim]"
        )

    # --------------------------------------------------------
    # FINALIZE IN ORIGINAL INPUT ORDER
    # --------------------------------------------------------

    finalized_new = 0

    for file in files:
        file = Path(file)

        if file in resumed_results:
            results.append(resumed_results[file])
            continue

        if file in read_error_files or file not in prepared_data:
            continue

        data = prepared_data[file]

        try:
            prefetched = prefetch_futures[file].result()
        except Exception as error:
            prefetched = _recover_prefetch_failure(file, fingerprint_futures[file], error)
            console.print(f"[yellow]Ошибка фоновой подготовки {_rich(file.name)}: {_rich(error)}. "
                          "Завершённые этапы сохранены; поиск продолжится в основном потоке.[/yellow]")

        fingerprint_error = prefetched.get("fingerprint_error")
        fingerprint_data = prefetched.get("fingerprint_data")

        if fingerprint_error or not fingerprint_data:
            console.print(f"[yellow]Fingerprint недоступен для {_rich(file.name)}: {_rich(fingerprint_error or 'нет данных')}. Продолжаем независимые слои поиска.[/yellow]")
            data.setdefault("issues", []).append({"severity": "WARNING", "code": "FINGERPRINT_FAILED", "message": fingerprint_error or "Нет fingerprint"})
        else:
            fingerprint_count += 1
            if fingerprint_data.get("cache_hit"):
                fingerprint_cache_hit_count += 1
            data["fingerprint_duration"] = fingerprint_data["duration"]
            data["fingerprint"] = fingerprint_data["fingerprint"]
            if fingerprint_data.get("decode_fallback"):
                data["fingerprint_decode"] = {key: fingerprint_data.get(key) for key in
                                              ("decode_partial", "decode_verification_incomplete", "decode_validation", "decoder", "decoded_duration", "reference_duration", "initial_decoder_error", "decode_warnings")}

        recognition = prefetched.get("recognition") or {
            "status": "ERROR",
            "provider": None,
            "acoustid_candidates": [],
            "ranked_candidates": [],
            "audd_result": None,
            "fallback_used": False,
            "error": "V12 prefetch не вернул recognition.",
        }

        recognition_status = recognition.get("status") or "ERROR"
        recognition_provider = recognition.get("provider")
        candidates = recognition.get("acoustid_candidates") or []
        ranked_candidates = recognition.get("ranked_candidates") or []
        audd_result = recognition.get("audd_result")
        audd_match = recognition.get("audd_match") or {}

        data["recognition"] = recognition
        data['acoustid_multipass'] = recognition.get('acoustid_multipass')
        data["recognition_status"] = recognition_status
        data["recognition_provider"] = recognition_provider
        data["acoustid_candidates"] = candidates
        data["ranked_candidates"] = ranked_candidates
        data["audd_result"] = audd_result
        data["audd_match"] = audd_match

        secondary_prefetched = prefetched.get("secondary")

        if (
            recognition_status == "FOUND"
            and recognition_provider == "acoustid"
        ):
            acoustid_found_count += 1

            print_identification(
                file,
                data,
                ranked_candidates,
                prefetched_mb=prefetched.get("mb_prefetch"),
            )

            acoustid_best = (
                ranked_candidates[0]
                if ranked_candidates
                else None
            )
            acoustid_safety = data.get("action_safety") or "REJECT"
            data["acoustid_action_safety"] = acoustid_safety

            if acoustid_safety != "SAFE":
                run_secondary_identity_pipeline(
                    file,
                    data,
                    fingerprint_data=fingerprint_data,
                    acoustid_match=acoustid_best,
                    acoustid_safety=acoustid_safety,
                    prefetched=secondary_prefetched,
                )

        elif recognition_status == "NOT_FOUND":
            data["change_plan"] = build_local_cleanup_plan(data)

            secondary = run_secondary_identity_pipeline(
                file,
                data,
                fingerprint_data=fingerprint_data,
                prefetched=secondary_prefetched,
            )

            fusion_status = (
                (secondary.get("fusion") or {}).get("status")
                or "NOT_FOUND"
            )
            audd_bridge_status = (
                (secondary.get("audd_bridge") or {}).get("status")
                or ""
            )
            audd_match_status = (
                ((secondary.get("audd") or {}).get("audd_match") or {}).get("status")
                or ""
            )

            if (
                fusion_status not in {"SAFE", "REVIEW"}
                and audd_bridge_status != "RESOLVED"
                and audd_match_status != "STRONG"
            ):
                print_recognition_not_found(file)

        else:
            recognition_error_count += 1
            data["change_plan"] = build_local_cleanup_plan(data)

            console.print(
                "[yellow]"
                "Первичный AcoustID-слой не завершён для "
                f"{_rich(file.name)}: "
                f"{recognition.get('error') or recognition.get('acoustid_error') or '—'}"
                "[/yellow]"
            )

            secondary = run_secondary_identity_pipeline(
                file,
                data,
                fingerprint_data=fingerprint_data,
                prefetched=secondary_prefetched,
            )

            fusion_status = (
                (secondary.get("fusion") or {}).get("status")
                or "NOT_FOUND"
            )
            audd_bridge_status = (
                (secondary.get("audd_bridge") or {}).get("status")
                or ""
            )
            audd_match_status = (
                ((secondary.get("audd") or {}).get("audd_match") or {}).get("status")
                or ""
            )

            if (
                fusion_status not in {"SAFE", "REVIEW"}
                and audd_bridge_status != "RESOLVED"
                and audd_match_status != "STRONG"
            ):
                print_recognition_not_found(file)

        data["issues"] = reconcile_issues_with_change_plan(
            data.get("issues"),
            data.get("change_plan"),
        )

        if not _result_search_complete(data):
            data["_checkpoint_retry_required"] = True
            data.setdefault("_checkpoint_retry_reasons", []).append("Не все запущенные проверки завершены; причину см. в предупреждениях по источникам и аудио.")
        from core.track_diagnostics import add_track_diagnostics, apply_identity_write_guard
        add_track_diagnostics(data)
        review_guard = apply_identity_write_guard(data)
        if review_guard:
            console.print("[yellow]Итоговый план требует проверки: " + _rich(' '.join(review_guard)) + "[/yellow]")

        _journal_stage(file, "plan_done", identity=(data.get("change_plan") or {}).get("identity"),
                       retry=bool(data.get("_checkpoint_retry_required")), warnings=data.get("track_diagnostics"),
                       review_guard=review_guard, proposals=_journal_proposals(data),
                       retry_reasons=data.get('_checkpoint_retry_reasons') or [])
        if data.get("_checkpoint_retry_required"):
            reasons = data.get("_checkpoint_retry_reasons") or []
            reason_text = "; ".join(str(value) for value in reasons[:2])
            console.print(
                "[yellow]"
                f"Checkpoint не помечен завершённым для {_rich(file.name)}: "
                "незавершённая ветка поиска будет повторена при следующем запуске"
                + (f" ({reason_text})" if reason_text else "")
                + ".[/yellow]"
            )
        else:
            try:
                save_completed_result(
                    file,
                    preferences,
                    data,
                )
            except Exception as error:
                console.print(
                    "[yellow]"
                    f"Не удалось сохранить checkpoint для {_rich(file.name)}: {_rich(error)}"
                    "[/yellow]"
                )

        results.append(data)
        finalized_new += 1

        if finalized_new % 100 == 0:
            done = resumed_count + finalized_new
            elapsed = max(0.001, time.monotonic() - prefetch_started)
            rate = finalized_new / elapsed
            console.print(
                "[dim]"
                f"V13 progress: {done}/{len(files)}; "
                f"новых {finalized_new}; ~{rate:.2f} файла/с finalization."
                "[/dim]"
            )

    # All per-file futures have been consumed. Shutdown preserves cache/checkpoint
    # files and only joins worker threads.
    network_pipeline.shutdown()
    fingerprint_executor.shutdown(wait=True)
    _journal_stage(None, "analysis_done", processed=len(results), resumed=resumed_count,
                   fingerprint_available=fingerprint_count, musicbrainz_transport=musicbrainz_transport_stats(),
                   retry_required=sum(bool(d.get('_checkpoint_retry_required')) for d in results),
                   audio_decode_partial=sum(bool((d.get('fingerprint_decode') or {}).get('decode_partial')) for d in results),
                   audio_decode_unverified=sum(bool((d.get('fingerprint_decode') or {}).get('decode_verification_incomplete')) for d in results))

    # --------------------------------------------------------
    # ИТОГОВЫЕ ТАБЛИЦЫ
    # --------------------------------------------------------

    print_library_table(results)
    print_audit_details(results)
    print_extra_tag_audit(results)
    print_change_preview(results)
    _journal_stage(None, "summary_tables_done")

    # ---------------------------------------------
    # СОХРАНЯЕМ ПОЛНЫЙ ПЛАН
    # ---------------------------------------------

    reports_dir = (
        ROOT_DIR
        / "reports"
    )

    plan_path = (
        export_change_plan(
            results,
            reports_dir,
            preferences=preferences,
        )
    )

    console.print(
        "\n[green]"
        "План изменений сохранён: "
        f"{_rich(plan_path)}"
        "[/green]"
    )


    # ---------------------------------------------
    # ВЫБОР РЕЖИМА
    # ---------------------------------------------

    write_results = []
    review_decisions = []
    _journal_stage(None, "write_mode_waiting")
    write_mode = (
        choose_write_mode()
    )
    _journal_stage(None, "write_mode_selected", mode=write_mode)


    # =============================================
    # 1 — DRY RUN
    # =============================================

    if write_mode == "1":
        operations = (
            collect_safe_operations(
                results
            )
        )

        execute_write_plan(
            operations,
            dry_run=True,
        )

        print_writer_dry_run(
            operations
        )


    # =============================================
    # 2 — ТОЛЬКО SAFE
    # =============================================

    elif write_mode == "2":
        operations = (
            collect_safe_operations(
                results
            )
        )

        print_writer_dry_run(
            operations
        )

        if not operations:
            console.print(
                "[yellow]"
                "SAFE-изменений нет."
                "[/yellow]"
            )

        elif confirm_real_write(
            operations
        ):
            backup_root = (
                create_backup_session(
                    ROOT_DIR
                )
            )

            write_results = (
                execute_write_plan(
                    operations,
                    backup_root=(
                        backup_root
                    ),
                    dry_run=False,
                    progress_callback=_journal_write_result,
                )
            )

            print_write_results(
                write_results
            )

            console.print(
                "\nРезервные копии:"
            )

            console.print(
                f"[green]"
                f"{_rich(backup_root)}"
                f"[/green]"
            )

        else:
            console.print(
                "[yellow]"
                "Запись отменена."
                "[/yellow]"
            )


    # =============================================
    # 3 — ИНТЕРАКТИВНАЯ ПРОВЕРКА
    # =============================================

    elif write_mode == "3":
        review = (
            collect_interactive_operations(
                results,
                include_safe=True,
            )
        )

        review_decisions = review.get("decisions") or []
        _journal_stage(None, "manual_review_done", decisions=review_decisions, stopped=review.get("stopped"))
        operations = (
            review[
                "operations"
            ]
        )

        console.print()

        console.print(
            "[bold]"
            "Ручная проверка завершена."
            "[/bold]"
        )

        if review["stopped"]:
            console.print(
                "[yellow]"
                "Проверка была остановлена "
                "пользователем."
                "[/yellow]"
            )

        print_writer_dry_run(
            operations
        )

        if not operations:
            console.print(
                "[yellow]"
                "Ничего не выбрано "
                "для записи."
                "[/yellow]"
            )

        elif confirm_real_write(
            operations
        ):
            backup_root = (
                create_backup_session(
                    ROOT_DIR
                )
            )

            write_results = (
                execute_write_plan(
                    operations,
                    backup_root=(
                        backup_root
                    ),
                    dry_run=False,
                    progress_callback=_journal_write_result,
                )
            )

            print_write_results(
                write_results
            )

            console.print(
                "\nРезервные копии:"
            )

            console.print(
                f"[green]"
                f"{_rich(backup_root)}"
                f"[/green]"
            )

        else:
            console.print(
                "[yellow]"
                "Запись отменена."
                "[/yellow]"
            )


    reports_dir = (
        ROOT_DIR
        / "reports"
    )

    plan_path = (
        export_change_plan(
            results,
            reports_dir,
            preferences=preferences,
            write_results=write_results,
            review_decisions=review_decisions,
        )
    )

    console.print(
        "\n[green]"
        "План изменений сохранён: "
        f"{_rich(plan_path)}"
        "[/green]"
    )

    total_issues = sum(
        len(
            data.get(
                "issues",
                [],
            )
        )
        for data in results
    )

    # Статистику fallback-слоёв считаем по фактически сохранённым результатам,
    # а не по веткам main(). Так цифры остаются корректными после изменения
    # порядка resolver'ов и повторного использования consensus pipeline.
    source_counts = {
        "STRONG": 0,
        "PARTIAL": 0,
        "CONFLICT": 0,
        "AMBIGUOUS": 0,
        "NO_SOURCE": 0,
        "ERROR": 0,
    }
    catalog_counts = {
        "STRONG": 0,
        "PARTIAL": 0,
        "CONFLICT": 0,
        "AMBIGUOUS": 0,
        "NOT_FOUND": 0,
        "ERROR": 0,
    }
    fusion_counts = {
        "SAFE": 0,
        "REVIEW": 0,
        "CONFLICT": 0,
        "NOT_FOUND": 0,
    }

    audd_strong_count = 0
    audd_partial_count = 0
    audd_conflict_count = 0
    fallback_error_count = 0
    audd_skipped_count = 0

    audd_mb_resolved_count = 0
    audd_mb_no_isrc_count = 0
    audd_mb_not_found_count = 0
    audd_mb_ambiguous_count = 0
    audd_mb_conflict_count = 0
    audd_mb_error_count = 0

    credit_consensus_count = 0
    credit_partial_count = 0
    credit_conflict_count = 0

    for item in results:
        source_status = (
            (item.get("source_url_identity") or {}).get("status")
        )
        if source_status in source_counts:
            source_counts[source_status] += 1

        catalog_status = (
            (item.get("catalog_identity") or {}).get("status")
        )
        if catalog_status in catalog_counts:
            catalog_counts[catalog_status] += 1

        fusion_status = (
            (item.get("fallback_fusion") or {}).get("status")
        )
        if fusion_status in fusion_counts:
            fusion_counts[fusion_status] += 1

        secondary = item.get("secondary_recognition") or {}
        audd_secondary = secondary.get("audd") or {}
        audd_match_secondary = audd_secondary.get("audd_match") or {}

        if (
            audd_secondary.get("status") == "FOUND"
            and audd_secondary.get("provider") == "audd"
        ):
            match_status = audd_match_secondary.get("status") or "PARTIAL"
            if match_status == "STRONG":
                audd_strong_count += 1
            elif match_status == "CONFLICT":
                audd_conflict_count += 1
            else:
                audd_partial_count += 1
        elif audd_secondary.get("status") == "FALLBACK_ERROR":
            if audd_secondary.get("audd_skipped"):
                audd_skipped_count += 1
            else:
                fallback_error_count += 1

        bridge = item.get("audd_musicbrainz") or {}
        bridge_status = bridge.get("status")
        if bridge_status == "RESOLVED":
            audd_mb_resolved_count += 1
        elif bridge_status == "NO_ISRC":
            audd_mb_no_isrc_count += 1
        elif bridge_status == "NOT_FOUND":
            audd_mb_not_found_count += 1
        elif bridge_status == "AMBIGUOUS":
            audd_mb_ambiguous_count += 1
        elif bridge_status == "CONFLICT":
            audd_mb_conflict_count += 1
        elif bridge_status == "ERROR":
            audd_mb_error_count += 1

        credit_status = (
            (item.get("credit_consensus") or {}).get("status")
        )
        if credit_status == "CONSENSUS":
            credit_consensus_count += 1
        elif credit_status == "CONFLICT":
            credit_conflict_count += 1
        elif credit_status == "PARTIAL":
            credit_partial_count += 1

    # Checkpoint-resume files must participate in statistics exactly like files
    # processed during this invocation.
    fingerprint_count = sum(
        1 for item in results if item.get("fingerprint")
    )
    acoustid_found_count = sum(
        1
        for item in results
        if item.get("recognition_provider") == "acoustid"
        and item.get("recognition_status") == "FOUND"
    )
    recognition_error_count = sum(
        1
        for item in results
        if (item.get("recognition_status") or "") == "ERROR"
    )

    source_strong_count = source_counts["STRONG"]
    source_partial_count = source_counts["PARTIAL"]
    source_conflict_count = source_counts["CONFLICT"]
    source_ambiguous_count = source_counts["AMBIGUOUS"]
    source_no_source_count = source_counts["NO_SOURCE"]
    source_error_count = source_counts["ERROR"]

    catalog_strong_count = catalog_counts["STRONG"]
    catalog_partial_count = catalog_counts["PARTIAL"]
    catalog_conflict_count = catalog_counts["CONFLICT"]
    catalog_ambiguous_count = catalog_counts["AMBIGUOUS"]
    catalog_not_found_count = catalog_counts["NOT_FOUND"]
    catalog_error_count = catalog_counts["ERROR"]

    console.print()

    console.print(
        f"[dim]"
        f"Fingerprint доступен для "
        f"{fingerprint_count} файлов"
        f"[/dim]"
    )

    if resumed_count:
        console.print(
            "[dim]"
            f"Resume checkpoint: {resumed_count} файлов не анализировались повторно."
            "[/dim]"
        )

    if fingerprint_cache_hit_count:
        console.print(
            "[dim]"
            f"Fingerprint cache hit в текущем запуске: {fingerprint_cache_hit_count}."
            "[/dim]"
        )

    print_search_health(results)

    console.print()

    console.print(
        f"Обработано: "
        f"[cyan]{len(results)}[/cyan]"
        f" | Ошибок чтения: "
        f"[cyan]{read_errors}[/cyan]"
        f" | Найдено замечаний: "
        f"[cyan]{total_issues}[/cyan]"
    )

    console.print()

    console.print(
        "[bold]"
        "Распознавание:"
        "[/bold]"
    )

    console.print(
        "  AcoustID найдено: "
        f"[green]{acoustid_found_count}[/green]"
    )

    console.print(
        "  AudD STRONG: "
        f"[green]{audd_strong_count}[/green]"
    )

    console.print(
        "  AudD PARTIAL: "
        f"[yellow]{audd_partial_count}[/yellow]"
    )

    console.print(
        "  AudD CONFLICT: "
        f"[red]{audd_conflict_count}[/red]"
    )

    console.print()
    console.print(
        "[bold]"
        "AudD STRONG → ISRC → MusicBrainz:"
        "[/bold]"
    )

    console.print(
        "  RESOLVED: "
        f"[green]{audd_mb_resolved_count}[/green]"
    )

    console.print(
        "  NO_ISRC: "
        f"[yellow]{audd_mb_no_isrc_count}[/yellow]"
    )

    console.print(
        "  NOT_FOUND: "
        f"[yellow]{audd_mb_not_found_count}[/yellow]"
    )

    console.print(
        "  AMBIGUOUS: "
        f"[yellow]{audd_mb_ambiguous_count}[/yellow]"
    )

    console.print(
        "  CONFLICT: "
        f"[red]{audd_mb_conflict_count}[/red]"
    )

    console.print(
        "  ERROR: "
        f"[red]{audd_mb_error_count}[/red]"
    )

    console.print()
    console.print(
        "[bold]"
        "Artist credit consensus среди MB RESOLVED:"
        "[/bold]"
    )

    console.print(
        "  CONSENSUS: "
        f"[green]{credit_consensus_count}[/green]"
    )

    console.print(
        "  PARTIAL: "
        f"[yellow]{credit_partial_count}[/yellow]"
    )

    console.print(
        "  CONFLICT: "
        f"[red]{credit_conflict_count}[/red]"
    )

    console.print()
    console.print(
        "[bold]Source URL resolver (только после неуверенного audio recognition):[/bold]"
    )
    console.print(
        "  STRONG: "
        f"[green]{source_strong_count}[/green]"
    )
    console.print(
        "  PARTIAL: "
        f"[yellow]{source_partial_count}[/yellow]"
    )
    console.print(
        "  CONFLICT: "
        f"[red]{source_conflict_count}[/red]"
    )
    console.print(
        "  AMBIGUOUS: "
        f"[yellow]{source_ambiguous_count}[/yellow]"
    )
    console.print(
        "  NO_SOURCE: "
        f"[dim]{source_no_source_count}[/dim]"
    )
    console.print(
        "  ERROR: "
        f"[red]{source_error_count}[/red]"
    )

    console.print()
    console.print(
        "[bold]Catalog search resolver (после неуверенного audio recognition):[/bold]"
    )
    console.print(
        "  STRONG: "
        f"[green]{catalog_strong_count}[/green]"
    )
    console.print(
        "  PARTIAL: "
        f"[yellow]{catalog_partial_count}[/yellow]"
    )
    console.print(
        "  CONFLICT: "
        f"[red]{catalog_conflict_count}[/red]"
    )
    console.print(
        "  AMBIGUOUS: "
        f"[yellow]{catalog_ambiguous_count}[/yellow]"
    )
    console.print(
        "  NOT_FOUND: "
        f"[dim]{catalog_not_found_count}[/dim]"
    )
    console.print(
        "  ERROR: "
        f"[red]{catalog_error_count}[/red]"
    )

    acr_counts = {
        "STRONG": 0,
        "PARTIAL": 0,
        "CONFLICT": 0,
        "NOT_FOUND": 0,
        "ERROR": 0,
    }

    for item in results:
        acr_status = (item.get("acrcloud_identity") or {}).get("status")
        if acr_status in acr_counts:
            acr_counts[acr_status] += 1

    console.print()
    console.print(
        "[bold]ACRCloud resolver (когда source/catalog consensus ещё недостаточен):[/bold]"
    )
    console.print(
        "  STRONG: "
        f"[green]{acr_counts['STRONG']}[/green]"
    )
    console.print(
        "  PARTIAL: "
        f"[yellow]{acr_counts['PARTIAL']}[/yellow]"
    )
    console.print(
        "  CONFLICT: "
        f"[red]{acr_counts['CONFLICT']}[/red]"
    )
    console.print(
        "  NOT_FOUND: "
        f"[dim]{acr_counts['NOT_FOUND']}[/dim]"
    )
    console.print(
        "  ERROR: "
        f"[red]{acr_counts['ERROR']}[/red]"
    )

    console.print()
    console.print(
        "[bold]Consensus fallback:[/bold]"
    )
    console.print(
        "  SAFE: "
        f"[green]{fusion_counts['SAFE']}[/green]"
    )
    console.print(
        "  REVIEW: "
        f"[yellow]{fusion_counts['REVIEW']}[/yellow]"
    )
    console.print(
        "  CONFLICT: "
        f"[red]{fusion_counts['CONFLICT']}[/red]"
    )
    console.print(
        "  NOT_FOUND: "
        f"[dim]{fusion_counts['NOT_FOUND']}[/dim]"
    )

    # Coverage = полная Artist+Title identity, которую программа считает
    # достаточно надёжной хотя бы для REVIEW. Title-only source без Artist
    # теперь сюда не попадает.
    trusted_identity_count = 0
    without_accepted_identity = []
    for item in results:
        provider = item.get("recognition_provider")
        acoustid_safety = item.get("acoustid_action_safety")
        audd_status = (item.get("audd_match") or {}).get("status")
        fusion = item.get("fallback_fusion") or {}
        fusion_status = fusion.get("status")
        fusion_has_identity = bool(
            fusion.get("artist") and fusion.get("title")
        )
        secondary_audd = (
            (item.get("secondary_recognition") or {}).get("audd")
            or {}
        )
        secondary_audd_status = (
            (secondary_audd.get("audd_match") or {}).get("status")
        )
        audd_bridge_status = (
            (item.get("audd_musicbrainz") or {}).get("status")
        )

        if provider == "acoustid" and acoustid_safety in {"SAFE", "REVIEW"}:
            trusted_identity_count += 1
        elif provider == "audd" and audd_status == "STRONG":
            trusted_identity_count += 1
        elif fusion_status in {"SAFE", "REVIEW"} and fusion_has_identity:
            trusted_identity_count += 1
        elif secondary_audd_status == "STRONG":
            trusted_identity_count += 1
        elif audd_bridge_status == "RESOLVED":
            trusted_identity_count += 1
        else:
            without_accepted_identity.append(item)

    # Итоговое unresolved считаем по фактическому coverage, а не по веткам:
    # так AudD CONFLICT / network ERROR тоже корректно попадают в остаток.
    pending_without_identity = sum(bool(item.get('_checkpoint_retry_required')) or not _result_search_complete(item)
                                   for item in without_accepted_identity)
    completed_without_identity = len(without_accepted_identity) - pending_without_identity

    coverage = (
        100.0 * trusted_identity_count / len(results)
        if results
        else 0.0
    )

    console.print()
    console.print(
        "[bold]Кандидаты с Artist/Title (SAFE или REVIEW):[/bold] "
        f"{trusted_identity_count}/{len(results)} "
        f"= [bold]{coverage:.1f}%[/bold] "
        "(REVIEW требует проверки; это покрытие кандидатами, не измеренная точность)"
    )

    console.print()

    console.print(
        "  Без кандидата уровня SAFE/REVIEW после завершённой проверки: "
        f"[yellow]{completed_without_identity}[/yellow]"
    )
    console.print(f"  Без кандидата уровня SAFE/REVIEW, проверка не завершена: {pending_without_identity}")
    _journal_stage(None, 'recognition_coverage', candidates=trusted_identity_count, total=len(results),
                   completed_without_identity=completed_without_identity,
                   pending_without_identity=pending_without_identity)

    console.print(
        "  Ошибок AudD fallback: "
        f"[yellow]{fallback_error_count}[/yellow]"
    )

    if audd_skipped_count:
        console.print(
            "  AudD пропущен после quota/disable: "
            f"[dim]{audd_skipped_count}[/dim]"
        )

    if recognition_error_count:
        console.print(
            "  Прочих ошибок распознавания: "
            f"[red]{recognition_error_count}[/red]"
        )

    save_report()


# ============================================================
# ЗАПУСК
# ============================================================

def _journal_write_result(outcome):
    _journal_stage(outcome.get("path"), "write_done", success=outcome.get("success"),
                   verified=outcome.get("verified"), rolled_back=outcome.get("rolled_back"),
                   backup=outcome.get("backup"), error=outcome.get("error"),
                   mtime_preserved=outcome.get('mtime_preserved'),
                   mtime_before_ns=outcome.get('mtime_before_ns'), mtime_after_ns=outcome.get('mtime_after_ns'))


def _journal_stage(file, stage, **details):
    if run_journal is not None:
        run_journal.stage(file, stage, **details)


def _result_search_complete(data):
    from core.track_diagnostics import incomplete_search_stages
    return not incomplete_search_stages(data)


def _journal_proposals(data):
    """Exact text proposals, without raw provider payloads or truncated tables."""
    plan = data.get('change_plan') or {}
    return {field: {key: plan[field].get(key) for key in ('status', 'current', 'proposed')}
            for field in ('artist', 'title', 'album', 'albumartist', 'date', 'tracknumber', 'discnumber')
            if isinstance(plan.get(field), dict)}


def _has_unavailable_audd(value):
    """Count affected files once, even when nested branches repeat the error."""
    from core.audd_cache import is_audd_quota_error
    if isinstance(value, dict):
        if value.get('audd_skipped') or is_audd_quota_error(value.get('audd_result') or {}):
            return True
        return any(_has_unavailable_audd(v) for v in value.values() if isinstance(v, (dict, list)))
    if isinstance(value, list):
        return any(_has_unavailable_audd(v) for v in value)
    return False


def print_search_health(results):
    from core.audd_cache import audd_service_state, audd_network_enabled
    audd_state = audd_service_state()
    audd_policy_skipped = sum(bool(((d.get('secondary_recognition') or {}).get('audd') or {}).get('audd_disabled_by_policy'))
                              or bool((d.get('recognition') or {}).get('audd_disabled_by_policy')) for d in results)
    if not audd_network_enabled():
        console.print(f'AudD: новые запросы отключены; пропущен как необязательный источник у {audd_policy_skipped} файлов. '
                      'Ранее найденные положительные ответы сохранены. Завершённость оценивается по включённым источникам.')
    if audd_state.get('category') in {'quota_exhausted', 'anonymous_limit'}:
        console.print('[yellow]AudD: сервер сообщил об исчерпании лимита. '
                      'Это ограничение API, а не ошибка распознавания или DNS; смена VPN не увеличивает квоту токена.[/yellow]')
    retry = sum(bool(d.get('_checkpoint_retry_required')) for d in results)
    catalog_error_files = _catalog_error_file_counts(results)
    if catalog_error_files:
        console.print('Незавершённые проверки каталога по источникам (файлы): ' + _rich(catalog_error_files))
    from core.extra_catalogs import service_state
    if (service_state().get('lastfm') or {}).get('last_api_error') == 11:
        console.print('[yellow]Last.fm: API 11 — сервис сообщил о временной недоступности. '
                      'Поиск по этому источнику требует повторения после восстановления сервиса.[/yellow]')
    incomplete = sum(bool((d.get('acrcloud_identity') or {}).get('verification_incomplete')) for d in results)
    quota_skipped = sum(bool((a.get('recognition') or {}).get('quota_skipped')) for d in results
                        for a in (d.get('acrcloud_identity') or {}).get('attempts') or [])
    partial = sum(bool((d.get('fingerprint_decode') or {}).get('decode_partial')) for d in results)
    unverified = sum(bool((d.get('fingerprint_decode') or {}).get('decode_verification_incomplete')) for d in results)
    transports = musicbrainz_transport_stats()
    attempts = sum(s.get('attempts', 0) for s in transports.values())
    successes = sum(s.get('successes', 0) for s in transports.values())
    console.print(f'Незавершённых веток поиска, требующих повторения: {retry} файлов.')
    audd_unavailable = sum(bool(d.get('_checkpoint_retry_required')) and
                           (_has_unavailable_audd(d.get('recognition')) or
                            _has_unavailable_audd(d.get('secondary_recognition'))) for d in results)
    console.print(f'Из них AudD недоступен из-за лимита/авторизации у {audd_unavailable} файлов; '
                  'у этих файлов могут быть и другие незавершённые проверки.')
    console.print(f'ACRCloud: неполная проверка фрагментов у {incomplete} файлов; '
                  f'запросов отложено после исчерпания лимита: {quota_skipped}.')
    multipass = sum(bool((d.get('acoustid_multipass') or {}).get('attempts')) for d in results)
    recovered = sum(bool((m.get('best_match') or {}).get('supplemental_audio_only')) or
                    any(i.get('supplemental_audio_only') for i in m.get('ranked_candidates') or []) for m in results)
    shazam_counts = {}
    for data in results:
        result = data.get('shazam_identity')
        if result:
            status = result.get('status') or 'ERROR'
            shazam_counts[status] = shazam_counts.get(status, 0) + 1
    console.print(f'Дополнительные отпечатки AcoustID: {multipass} файлов; '
                  f'кандидаты, полученные/усиленные дополнительными проходами: {recovered} файлов.')
    console.print(f"ShazamIO: {_rich(shazam_capability())}; результаты, включая кэш и восстановленные чекпоинты: {_rich(shazam_counts)}.")
    shazam_transport = shazam_transport_stats()
    shazam_cache_only = bool(shazam_transport.get('cache_hits')) and not shazam_transport.get('http_requests', 0)
    if shazam_cache_only:
        console.print('[cyan]Shazam: найденные ответы прочитаны из кэша; '
                      'новых HTTP-проверок не было. Быстрый пересчёт не является новым сетевым тестом.[/cyan]')
    console.print(f"Shazam: ожидание между HTTP-запросами {shazam_transport.get('pacing_wait_seconds', 0):.1f} с; "
                  f"HTTP и внутренние повторы {shazam_transport.get('http_and_retry_seconds', 0):.1f} с; "
                  f"восстановлений частоты {shazam_transport.get('pacing_recoveries', 0)}.")
    console.print(f"Shazam: HTTP-запросов {shazam_transport.get('http_requests', 0)}, "
                  f"ответов HTTP 429 {shazam_transport.get('http_429', 0)}, "
                  f"фрагментов из кэша {shazam_transport.get('cache_hits', 0)}, "
                  f"отложенных фрагментов {shazam_transport.get('deferred_samples', 0)}.")
    console.print(f"Shazam: сохранённых ответов этой же свежей проверки "
                  f"{shazam_transport.get('current_check_cache_hits', 0)}, обычного кэша "
                  f"{shazam_transport.get('shared_cache_hits', 0)}, фрагментов, требовавших "
                  f"свежей проверки {shazam_transport.get('fresh_cache_misses', 0)}.")
    if shazam_transport.get('fresh_cache_misses') and not shazam_transport.get('http_requests', 0):
        console.print('[yellow]Shazam: свежая проверка запрошена, но новых HTTP-ответов нет. '
                      'Проверьте сообщения об ошибках подготовки аудио, зависимости, сети или паузе сервиса; '
                      'старые ответы не заменяют незавершённую свежую проверку.[/yellow]')
    if shazam_transport.get('rate_limit_exhausted'):
        console.print('[yellow]Shazam продолжил ограничивать запросы после повторов с паузами. '
                      'Сетевые проверки этого источника отложены до следующего запуска; это не NOT_FOUND.[/yellow]')
    dns_health = {'musicbrainz': musicbrainz_dns_stats(), 'shazam': shazam_transport.get('dns') or {},
                  'audd': audd_dns_stats()}
    from core.acrcloud_client import acrcloud_dns_stats
    from core.catalog_search_resolver import itunes_dns_stats
    dns_health['itunes'] = itunes_dns_stats()
    dns_health.update({'acrcloud:' + host: stats for host, stats in acrcloud_dns_stats().items()})
    for provider, dns in dns_health.items():
        if dns.get('failures'):
            console.print(f"[yellow]{provider}: ошибки DNS — {dns['failures']}, "
                          f"проверки восстановления — {dns.get('probes', 0)}, "
                          f"восстановлений разрешения имени — {dns.get('recoveries', 0)}. "
                          'Проверка этого источника не завершена; после восстановления сети повторите запуск.[/yellow]')
    if partial:
        console.print(f'[yellow]Отпечаток восстановлен с повреждёнными кадрами: {partial} файлов; предложения требуют проверки.[/yellow]')
    if unverified:
        console.print(f'[yellow]Полноту резервного декодирования проверить не удалось: {unverified} файлов; предложения требуют проверки.[/yellow]')
    console.print(f'MusicBrainz: сетевых попыток {attempts}, успешных {successes}.')
    if attempts and not successes:
        console.print('[yellow]MusicBrainz не дал ни одного нового сетевого ответа. '
                      'Результаты из кэша использованы; незавершённые запросы будут повторены. '
                      'Покрытие кандидатами не означает завершение всех проверок.[/yellow]')
        for name, stats in transports.items():
            if stats.get('last_error'):
                console.print(f'[dim]{_rich(name)}: {_rich(stats["last_error"])}[/dim]')
    _journal_stage(None, 'search_health', retry_required=retry, acr_verification_incomplete=incomplete,
                   catalog_error_files_by_provider=catalog_error_files,
                   acr_quota_skipped=quota_skipped, audio_decode_partial=partial,
                   audio_decode_unverified=unverified, audd_unavailable_files=audd_unavailable,
                   audd_service_state=audd_state,
                   audd_network_enabled=audd_network_enabled(), audd_policy_skipped_files=audd_policy_skipped,
                   acoustid_multipass_files=multipass, supplemental_audio_files=recovered, shazam_statuses=shazam_counts,
                   shazam_transport=shazam_transport,
                   shazam_cache_only=shazam_cache_only,
                   dns_health=dns_health,
                   musicbrainz_transport=transports)


def _catalog_error_file_counts(results):
    counts = {}
    for data in results:
        providers = set()
        for error in (data.get('catalog_identity') or {}).get('errors') or []:
            if not isinstance(error, dict):
                continue
            provider = str(error.get('provider') or 'unknown').split('-', 1)[0]
            providers.add(provider)
        for provider in providers:
            counts[provider] = counts.get(provider, 0) + 1
    return counts


def run_app():
    global console, run_journal
    from core.run_journal import RunJournal
    run_journal = RunJournal(ROOT_DIR / "reports", sys.stdout)
    original_stdout = sys.stdout
    sys.stdout = run_journal
    console = Console(file=run_journal, record=False)
    console.print(f"Текущий отчёт: {_rich(run_journal.report_path)}")
    from core.network_settings import route_summary
    routes = {host: route_summary('https://' + host) for host in
              ('musicbrainz.org', 'amp.shazam.com', 'api.audd.io', 'api.acoustid.org',
               'api.discogs.com', 'ws.audioscrobbler.com')}
    from core.audd_cache import audd_network_enabled
    if not audd_network_enabled():
        console.print('AudD: новые запросы отключены. Используются остальные включённые источники и положительный кэш AudD.')
    from core.extra_catalogs import capability as extra_catalog_capability, service_state as extra_catalog_state
    for provider, info in extra_catalog_capability().items():
        console.print(f"{provider}: " + ('включён дополнительный поиск' if info['enabled'] else
                                        'отключён или не задан ключ; настройка: configure_extra_catalogs.cmd'))
    _journal_stage(None, 'run_configuration', version='13.27.1', shazam=shazam_capability(),
                   extra_catalogs=extra_catalog_capability(),
                   network_routes=routes,
                   audd_network_enabled=audd_network_enabled(),
                   audd_token_mode='configured' if (os.getenv('AUDD_API_TOKEN') or '').strip() not in {'', 'test'} else 'public_test')
    def shazam_progress(**details):
        _journal_stage(None, 'shazam_transport', **details)
        if details.get('event') == 'rate_limit_wait':
            console.print(f"Shazam: ожидание разрешённого повтора — {details['remaining_seconds']} с; прогресс сохраняется.")
    configure_progress(shazam_progress)
    status = "run_completed"
    try:
        main()
        _journal_stage(None, 'extra_catalog_summary', providers=extra_catalog_state())
        return 0
    except KeyboardInterrupt:
        _journal_stage(None, 'extra_catalog_summary', providers=extra_catalog_state())
        status = "run_interrupted"
        console.print("\nОбработка остановлена. Завершённые этапы и текущий отчёт сохранены.")
        return 130
    except Exception:
        _journal_stage(None, 'extra_catalog_summary', providers=extra_catalog_state())
        status = "run_failed"
        console.print_exception(show_locals=False)
        return 1
    finally:
        cancel_pending_waits()
        if active_pipeline is not None:
            active_pipeline.shutdown(cancel_pending=True, wait=False)
        if active_fingerprint_executor is not None:
            active_fingerprint_executor.shutdown(wait=False, cancel_futures=True)
        try:
            run_journal.close(status)
        finally:
            sys.stdout = original_stdout
            run_journal = None
            configure_progress(None)


if __name__ == "__main__":
    raise SystemExit(run_app())
