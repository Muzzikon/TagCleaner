from os import path
from pathlib import Path
from datetime import datetime
import shutil
import hashlib
import os

from core.cache_utils import mark_file_changed, file_signature

from mutagen import File
from core.extra_tags import read_remixer, write_remixer
from core.tag_access import read_value, write_value, SUPPORTED_EXTENSIONS


WRITABLE_FIELDS = {
    "artist": "artist",
    "title": "title",
    "album": "album",

    "albumartist": "albumartist",
    "date": "date",
    "tracknumber": "tracknumber",
    "discnumber": "discnumber",
    "remixer": "remixer",
}


def is_field_writable_for_path(
    file_path,
    field_name,
):
    """Fields supported by the shared Mutagen/container mapping."""

    if field_name not in WRITABLE_FIELDS:
        return False

    return Path(file_path).suffix.casefold() in SUPPORTED_EXTENSIONS


def values_equal(a, b):
    return (
        str(a or "")
        ==
        str(b or "")
    )


def make_operation(
    data,
    field_name,
    field,
):
    proposed = field.get(
        "proposed"
    )

    if proposed is None:
        return None

    path = str(
        data.get("path")
        or ""
    )

    if not is_field_writable_for_path(
        path,
        field_name,
    ):
        return None
    current = (
        field.get("current")
        or ""
    )

    if values_equal(
        current,
        proposed,
    ):
        return None

    return {
        "path": path,
        "file": (
            data.get("file")
            or Path(
                data.get("path")
                or ""
            ).name
        ),
        "field": field_name,
        "current": current,
        "proposed": proposed,
        "status": (
            field.get("status")
        ),
        'expected_file_signature': data.get('analysis_file_signature'),
    }


def collect_safe_operations(
    results,
):
    operations = []

    for data in results:
        plan = data.get(
            "change_plan"
        )

        if not plan:
            continue

        for field_name in (
            "artist",
            "title",
            "album",
            "albumartist",
            "date",
            "tracknumber",
            "discnumber",
        ):
            field = plan.get(
                field_name
            )

            if not field:
                continue

            if (
                field.get("status")
                != "SAFE"
            ):
                continue

            operation = make_operation(
                data,
                field_name,
                field,
            )

            if operation:
                operations.append(
                    operation
                )

    return operations


def group_operations_by_file(
    operations,
):
    grouped = {}

    for operation in operations:
        path = operation["path"]

        if not path:
            continue

        grouped.setdefault(
            path,
            [],
        ).append(
            operation
        )

    return grouped


def create_backup_session(
    root_dir,
):
    timestamp = (
        datetime.now().strftime(
            "%Y-%m-%d_%H-%M-%S"
        )
    )

    backup_root = (
        Path(root_dir)
        / "backups"
        / timestamp
    )

    backup_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    return backup_root


def backup_original(
    file_path,
    backup_root,
):
    file_path = Path(file_path)
    backup_root = Path(
        backup_root
    )

    backup_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    destination = (
        backup_root
        / file_path.name
    )

    counter = 1

    while destination.exists():
        destination = (
            backup_root
            / (
                f"{file_path.stem}"
                f"_{counter}"
                f"{file_path.suffix}"
            )
        )

        counter += 1

    shutil.copy2(
        file_path,
        destination,
    )

    return destination


def file_sha256(
    file_path,
):
    file_path = Path(
        file_path
    )

    digest = hashlib.sha256()

    with file_path.open(
        "rb"
    ) as file:
        while True:
            chunk = file.read(
                1024 * 1024
            )

            if not chunk:
                break

            digest.update(
                chunk
            )

    return digest.hexdigest()


def restore_from_backup(
    file_path,
    backup_path,
):
    """
    Восстанавливает исходный файл
    из полной резервной копии.

    После копирования сравнивает SHA-256,
    то есть проверяет побайтовое
    восстановление содержимого.
    """

    file_path = Path(
        file_path
    )

    backup_path = Path(
        backup_path
    )

    if not backup_path.is_file():
        return {
            "success": False,
            "verified": False,
            "error": (
                "Резервная копия "
                "не существует."
            ),
        }

    try:
        original_stat = backup_path.stat()
        revision_error = None
        try:
            mark_file_changed(file_path)
        except Exception as error:
            # A cache failure must not prevent recovery of the audio bytes.
            revision_error = str(error)
        shutil.copy2(
            backup_path,
            file_path,
        )

        backup_hash = (
            file_sha256(
                backup_path
            )
        )

        restored_hash = (
            file_sha256(
                file_path
            )
        )

        verified = (
            backup_hash
            == restored_hash
        )

        if not verified:
            return {
                "success": False,
                "verified": False,
                "error": (
                    "Файл скопирован из backup, "
                    "но контрольные суммы "
                    "не совпадают."
                ),
            }

        restore_file_times(file_path, original_stat)

        return {
            "success": True,
            "verified": True,
            "error": None,
            "mtime_preserved": True,
            "cache_invalidation_error": revision_error,
        }

    except Exception as error:
        return {
            "success": False,
            "verified": False,
            "error": str(error),
        }


def restore_file_times(file_path, original_stat):
    """Preserve integer timestamps, including NTFS's 100 ns precision."""
    os.utime(file_path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    if Path(file_path).stat().st_mtime_ns != original_stat.st_mtime_ns:
        raise RuntimeError('Дата изменения файла не восстановлена точно; запись откатывается.')


def apply_operations_to_file(
    file_path,
    operations,
    backup_root=None,
    dry_run=True,
):
    file_path = Path(
        file_path
    )

    if dry_run:
        return {
            "path": str(file_path),
            "success": True,
            "changed": False,
            "dry_run": True,
            "operations": operations,
            "backup": None,
            "error": None,
        }

    if not file_path.is_file():
        return {
            "path": str(file_path),
            "success": False,
            "changed": False,
            "dry_run": False,
            "operations": operations,
            "backup": None,
            "error": (
                "Файл не существует."
            ),
        }

    if not backup_root:
        return {
            "path": str(file_path),
            "success": False,
            "changed": False,
            "dry_run": False,
            "operations": operations,
            "backup": None,
            "error": (
                "Не указан каталог "
                "резервных копий."
            ),
        }

    backup_path = None
    save_started = False

    try:
        original_stat = file_path.stat()
        current_signature = file_signature(file_path)
        if any(op.get('expected_file_signature') is not None and
               op['expected_file_signature'] != current_signature for op in operations):
            raise RuntimeError('Файл изменился после анализа; запись старого плана запрещена. Повторите проверку файла.')
        # Сначала полная копия файла.
        backup_path = backup_original(
            file_path,
            backup_root,
        )
        audio = File(
            file_path,
            easy=True,
        )

        if audio is None:
            raise RuntimeError(
                "Mutagen не смог "
                "открыть файл."
            )

        if audio.tags is None:
            try:
                audio.add_tags()

            except Exception:
                pass

        # Check the entire group before changing any tag. A signature protects
        # against replacements; comparing tags also catches same-size edits
        # which deliberately preserve the modification time.
        for operation in operations:
            if operation.get('expected_file_signature') is None:
                continue
            current = read_value(audio, operation['field'])
            if current != str(operation.get('current') or ''):
                raise RuntimeError('Текущие теги изменились после анализа; старый план нельзя записать.')

        for operation in operations:
            field = operation[
                "field"
            ]

            if not is_field_writable_for_path(
                file_path,
                field,
            ):
                raise RuntimeError(
                    "Writer не разрешает поле "
                    f"{field!r} для файла "
                    f"{file_path.suffix!r}."
                )

            tag_name = (
                WRITABLE_FIELDS.get(
                    field
                )
            )

            if not tag_name:
                raise RuntimeError(
                    "Неизвестное поле writer: "
                    f"{field!r}."
                )

            proposed = str(
                operation[
                    "proposed"
                ]
            )

            write_value(audio, field, proposed)

        if file_signature(file_path) != current_signature:
            raise RuntimeError('Файл изменился во время подготовки записи. Повторите проверку.')
        # Commit cache invalidation only after read-only validation. A failed
        # preflight must not rewrite the file by restoring an unnecessary backup.
        mark_file_changed(file_path)
        save_started = True
        audio.save()

        # -----------------------------------------
        # ПРОВЕРКА ПОСЛЕ ЗАПИСИ
        # -----------------------------------------

        verify_audio = File(
            file_path,
            easy=True,
        )

        if verify_audio is None:
            raise RuntimeError(
                "Файл записан, но Mutagen "
                "не смог открыть его повторно "
                "для проверки."
            )

        verification_errors = []

        for operation in operations:
            field = operation[
                "field"
            ]

            if not is_field_writable_for_path(
                file_path,
                field,
            ):
                verification_errors.append({
                    "field": field,
                    "expected": str(
                        operation.get(
                            "proposed"
                        )
                    ),
                    "actual": (
                        "поле запрещено "
                        "для этого формата"
                    ),
                })

                continue

            tag_name = (
                WRITABLE_FIELDS.get(
                    field
                )
            )

            if not tag_name:
                verification_errors.append({
                    "field": field,
                    "expected": str(
                        operation.get(
                            "proposed"
                        )
                    ),
                    "actual": (
                        "неизвестное поле"
                    ),
                })

                continue

            expected = str(
                operation["proposed"]
            )

            actual_values = []

            if verify_audio.tags:
                actual_values = (
                    verify_audio.tags.get(
                        tag_name,
                        [],
                    )
                    or []
                )

            actual = (
                str(actual_values[0])
                if actual_values
                else ""
            )
            actual = read_value(verify_audio, field)

            if actual != expected:
                verification_errors.append({
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                })


        if verification_errors:
            details = "; ".join(
                (
                    f"{item['field']}: "
                    f"ожидалось "
                    f"{item['expected']!r}, "
                    f"получено "
                    f"{item['actual']!r}"
                )
                for item
                in verification_errors
            )

            rollback = (
                restore_from_backup(
                    file_path,
                    backup_path,
                )
            )

            return {
                "path": str(file_path),

                "success": False,

                # Если rollback прошёл,
                # файл снова идентичен backup.
                "changed": (
                    not rollback[
                        "success"
                    ]
                ),

                "verified": False,
                "dry_run": False,
                "operations": operations,

                "backup": str(
                    backup_path
                ),

                "verification_errors": (
                    verification_errors
                ),

                "rollback_attempted": True,

                "rolled_back": (
                    rollback[
                        "success"
                    ]
                ),

                "rollback_verified": (
                    rollback[
                        "verified"
                    ]
                ),

                "rollback_error": (
                    rollback[
                        "error"
                    ]
                ),

                "error": (
                    "Проверка после записи "
                    "не пройдена: "
                    + details
                ),
            }


        restore_file_times(file_path, original_stat)

        return {
            "path": str(file_path),
            "success": True,
            "changed": True,
            "verified": True,
            "dry_run": False,
            "operations": operations,
            "backup": str(
                backup_path
            ),
            "verification_errors": [],
            "rollback_attempted": False,
            "rolled_back": False,
            "rollback_verified": None,
            "rollback_error": None,
            "error": None,
            "mtime_preserved": True,
            "mtime_before_ns": original_stat.st_mtime_ns,
            "mtime_after_ns": file_path.stat().st_mtime_ns,
        }

    except Exception as error:
        rollback = {
            "success": False,
            "verified": False,
            "error": None,
        }

        rollback_attempted = False

        if (
            save_started and backup_path
            and Path(
                backup_path
            ).is_file()
        ):
            rollback_attempted = True

            rollback = (
                restore_from_backup(
                    file_path,
                    backup_path,
                )
            )

        return {
            "path": str(file_path),
            "success": False,

            "changed": (
                not rollback[
                    "success"
                ]
                if rollback_attempted
                else False
            ),

            "verified": False,
            "dry_run": False,
            "operations": operations,

            "backup": (
                str(backup_path)
                if backup_path
                else None
            ),

            "rollback_attempted": (
                rollback_attempted
            ),

            "rolled_back": (
                rollback[
                    "success"
                ]
                if rollback_attempted
                else False
            ),

            "rollback_verified": (
                rollback[
                    "verified"
                ]
                if rollback_attempted
                else None
            ),

            "rollback_error": (
                rollback[
                    "error"
                ]
                if rollback_attempted
                else None
            ),

            "error": str(error),
        }


def execute_write_plan(
    operations,
    backup_root=None,
    dry_run=True,
    progress_callback=None,
):
    grouped = (
        group_operations_by_file(
            operations
        )
    )

    results = []

    for path, file_operations in (
        grouped.items()
    ):
        result = (
            apply_operations_to_file(
                path,
                file_operations,
                backup_root=(
                    backup_root
                ),
                dry_run=dry_run,
            )
        )

        if progress_callback is not None:
            progress_callback(result)

        results.append(
            result
        )

    return results
