from pathlib import Path

from core.tag_writer import (
    make_operation,
)

from core.change_planner import (
    plan_album_from_release_context,
    plan_release_dependent_fields,
)


RELEASE_FIELD_NAMES = (
    "album",
    "albumartist",
    "date",
    "tracknumber",
    "discnumber",
)


def confirm_identity_for_review(data):
    from core.identity_review import review_context_lines
    print_separator()
    for line in review_context_lines(data):
        print(line)
    print('\nПредложение может быть ошибочным. Выбор поля сам по себе не проверяет аудио.')
    print('1 — я проверил аудио, исполнителя, название и версию; перейти к выбору тегов')
    print('2 — пропустить изменения этого трека')
    print('q — закончить ручную проверку')
    while True:
        choice = input('Выбор: ').strip().casefold()
        if choice == '1':
            return 'confirmed'
        if choice == '2':
            return 'skip'
        if choice in {'q', 'quit'}:
            return 'quit'
        print('Введите 1, 2 или q.')


def review_release_fields(data, fields):
    print_separator()
    print(data.get('file') or data.get('path') or '—')
    print('Изменения относятся к одному предложенному релизу:')
    for name, field in fields:
        show_field(name, field)
    return 'accept' if ask_yes_no('Принять этот набор полей релиза?', default=False) else 'keep'


def print_separator():
    print(
        "\n"
        + "=" * 70
    )


def ask_yes_no(
    text,
    default=False,
):
    suffix = (
        " [Y/n]: "
        if default
        else " [y/N]: "
    )

    while True:
        value = (
            input(
                text + suffix
            )
            .strip()
            .casefold()
        )

        if not value:
            return default

        if value in {
            "y",
            "yes",
            "д",
            "да",
        }:
            return True

        if value in {
            "n",
            "no",
            "н",
            "нет",
        }:
            return False

        print(
            "Введите y или n."
        )


def show_field(
    name,
    field,
):
    current = (
        field.get("current")
        or "—"
    )

    proposed = (
        field.get("proposed")
    )

    print(
        f"\n{name}:"
    )

    print(
        f"  было:  {current}"
    )

    if proposed is not None:
        print(
            f"  станет: {proposed}"
        )

    reason = (
        field.get("reason")
        or ""
    )

    if reason:
        print(
            f"  причина: {reason}"
        )


def _set_manual_value(field, value):
    field.setdefault('recognition_proposed', field.get('proposed'))
    field['proposed'] = value
    field['manual_override'] = True


def _manual_identity_corrected(data):
    plan = data.get('change_plan') or {}
    return any(field.get('manual_override')
               and field.get('proposed') != field.get('recognition_proposed')
               for name in ('artist', 'title')
               if isinstance(field := plan.get(name), dict))


def _invalidate_release_proposals(data):
    # Keep the former proposal as evidence, but never attach its edition
    # to the identity entered by the user without a new release lookup.
    plan = data.get('change_plan') or {}
    plan['release_requires_new_search'] = True
    for name in (*RELEASE_FIELD_NAMES, 'cover'):
        field = plan.get(name)
        if not isinstance(field, dict) or field.get('status') not in {'SAFE', 'REVIEW', 'ASK', 'MANUAL'}:
            continue
        field.setdefault('recognition_status', field['status'])
        field['status'] = 'BLOCKED'
        note = 'Предложение относится к прежнему кандидату; после исправления Artist/Title нужен новый поиск релиза.'
        field['reason'] = ' '.join(value for value in (field.get('reason'), note) if value)


def choose_option(
    field_name,
    field,
    allow_custom=False,
):
    options = (
        field.get("options")
        or []
    )

    current = (
        field.get("current")
        or ""
    )

    print(
        f"\n{field_name}:"
    )

    if current:
        print(
            f"  сейчас: {current}"
        )

    print(
        "  0 — оставить как есть"
    )

    for index, option in enumerate(
        options,
        start=1,
    ):
        print(
            f"  {index} — {option}"
        )

    if allow_custom:
        print('  c — ввести проверенное значение вручную')

    while True:
        raw = (
            input(
                "Выбор: "
            )
            .strip()
        )

        if raw.casefold() in {
            "q",
            "quit",
        }:
            return {
                "action": "quit"
            }
        if allow_custom and raw.casefold() == 'c':
            value = input('Проверенное значение (пусто — отменить): ').strip()
            return {'action': 'select', 'value': value, 'manual_override': True} if value else {'action': 'keep'}

        try:
            choice = int(raw)

        except ValueError:
            print(
                "Введите номер варианта."
            )
            continue

        if choice == 0:
            return {
                "action": "keep"
            }

        if (
            1
            <= choice
            <= len(options)
        ):
            return {
                "action": "select",
                "value": (
                    options[
                        choice - 1
                    ]
                ),
            }

        print(
            "Такого варианта нет."
        )


def format_release_context_option(
    context,
):
    """Короткое, но различимое описание конкретного ReleaseContext."""

    title = (
        context.get("title")
        or "—"
    )

    primary_type = (
        context.get("primary_type")
        or "тип не указан"
    )

    secondary_types = (
        context.get("secondary_types")
        or []
    )

    type_text = primary_type

    if secondary_types:
        type_text += (
            " / "
            + ", ".join(
                str(value)
                for value
                in secondary_types
            )
        )

    date = (
        context.get("date")
        or "дата неизвестна"
    )

    track_number = (
        context.get("track_number")
        or "?"
    )

    track_total = (
        context.get("track_total")
    )

    if track_total:
        track_text = (
            f"{track_number}/{track_total}"
        )
    else:
        track_text = str(
            track_number
        )

    return (
        f"{title} "
        f"[{type_text}; {date}; track {track_text}]"
    )


def choose_release_context(
    data,
    contexts,
):
    contexts = list(
        contexts or []
    )

    print(
        "\nRelease / Album:"
    )

    current_album = (
        data.get("album")
        or ""
    )

    if current_album:
        print(
            f"  сейчас: {current_album}"
        )

    print(
        "  0 — оставить как есть"
    )

    for index, context in enumerate(
        contexts,
        start=1,
    ):
        print(
            f"  {index} — "
            f"{format_release_context_option(context)}"
        )

    while True:
        raw = (
            input(
                "Выбор: "
            )
            .strip()
        )

        if raw.casefold() in {
            "q",
            "quit",
        }:
            return {
                "action": "quit"
            }

        try:
            choice = int(raw)

        except ValueError:
            print(
                "Введите номер варианта."
            )
            continue

        if choice == 0:
            return {
                "action": "keep"
            }

        if (
            1
            <= choice
            <= len(contexts)
        ):
            context = (
                contexts[
                    choice - 1
                ]
            )

            return {
                "action": "select",
                "context": context,
                "value": (
                    context.get("title")
                    or ""
                ),
                "release_id": (
                    context.get("release_id")
                ),
            }

        print(
            "Такого варианта нет."
        )


def build_manual_release_operations(
    data,
    context,
):
    """
    Превращает ручной выбор ReleaseContext в единый набор операций.

    Важно: пользователь выбирает не только строку Album. Тот же самый
    ReleaseContext одновременно становится источником Album Artist,
    Date, Track и Disc, чтобы поля не оказались от разных изданий.
    """

    if not context:
        return []
    if _manual_identity_corrected(data):
        return []
    from core.identity_review import needs_identity_confirmation
    if needs_identity_confirmation(data) and not data.get('_manual_identity_confirmed'):
        return []

    selection = {
        "status": "SELECTED",
        "selected": context,
        "options": [context],
        "equivalent_releases": [context],
        "concrete_release_ambiguous": False,
        "reason": (
            "ReleaseContext выбран "
            "пользователем вручную."
        ),
    }

    fields = {
        "album": (
            plan_album_from_release_context(
                current_album=(
                    data.get("album")
                    or ""
                ),
                release_context_selection=(
                    selection
                ),
                # Ручной выбор сам является подтверждением
                # конкретного релиза для release-dependent полей.
                safety="SAFE",
            )
        ),
        **plan_release_dependent_fields(
            data=data,
            release_context_selection=(
                selection
            ),
            safety="SAFE",
        ),
    }

    operations = []

    for field_name in (
        RELEASE_FIELD_NAMES
    ):
        field = (
            fields.get(field_name)
            or {}
        )

        proposed = field.get(
            "proposed"
        )

        if proposed is None:
            continue

        manual_field = {
            **field,
            "status": "MANUAL",
            "reason": (
                "Значение следует из "
                "ReleaseContext, выбранного "
                "пользователем вручную."
            ),
        }

        operation = make_operation(
            data,
            field_name,
            manual_field,
        )

        if operation:
            operations.append(
                operation
            )

    return operations


def review_linked_artist_title(
    data,
    artist_field,
    title_field,
):
    print_separator()

    print(
        data.get(
            "file",
            "неизвестный файл",
        )
    )

    print(
        "\nЭти изменения связаны."
    )

    show_field(
        "Artist",
        artist_field,
    )

    show_field(
        "Title",
        title_field,
    )

    pending_artist_selection = None
    if artist_field.get("options"):
        selection = choose_option("Artist: историческое / текущее имя", artist_field, allow_custom=True)
        if selection["action"] == "select":
            pending_artist_selection = selection
        else:
            return "quit" if selection["action"] == "quit" else "keep"

    print(
        "\n1 — принять ОБА изменения"
    )

    print(
        "2 — оставить ОБА поля"
    )
    print('3 — ввести проверенные Artist и Title вручную')

    print(
        "q — закончить ручную проверку"
    )

    while True:
        choice = (
            input(
                "Выбор: "
            )
            .strip()
            .casefold()
        )

        if choice == "1":
            if pending_artist_selection:
                if pending_artist_selection.get('manual_override'):
                    _set_manual_value(artist_field, pending_artist_selection['value'])
                else:
                    artist_field['proposed'] = pending_artist_selection['value']
            return "accept"

        if choice == "2":
            return "keep"
        if choice == '3':
            artist = input('Проверенный Artist (пусто — отменить): ').strip()
            if not artist:
                return 'keep'
            title = input('Проверенный Title с нужной версией (пусто — отменить): ').strip()
            if not title:
                return 'keep'
            _set_manual_value(artist_field, artist)
            _set_manual_value(title_field, title)
            return 'accept'

        if choice in {
            "q",
            "quit",
        }:
            return "quit"

        print(
            "Введите 1, 2, 3 или q."
        )


def review_single_field(
    data,
    field_name,
    field,
):
    print_separator()

    print(
        data.get(
            "file",
            "неизвестный файл",
        )
    )

    show_field(
        field_name,
        field,
    )

    if field_name == "artist" and field.get("options"):
        selection = choose_option("Artist: историческое / текущее имя", field, allow_custom=True)
        if selection["action"] == "select":
            if selection.get('manual_override'):
                _set_manual_value(field, selection['value'])
            else:
                field["proposed"] = selection["value"]
            field["status"] = "MANUAL"
            return "accept"
        return "quit" if selection["action"] == "quit" else "keep"

    print(
        "\n1 — принять изменение"
    )

    print(
        "2 — оставить как есть"
    )
    print('3 — ввести проверенное значение вручную')

    print(
        "q — закончить ручную проверку"
    )

    while True:
        choice = (
            input(
                "Выбор: "
            )
            .strip()
            .casefold()
        )

        if choice == "1":
            return "accept"

        if choice == "2":
            return "keep"
        if choice == '3':
            value = input('Проверенное значение (пусто — отменить): ').strip()
            if not value:
                return 'keep'
            _set_manual_value(field, value)
            return 'accept'

        if choice in {
            "q",
            "quit",
        }:
            return "quit"

        print(
            "Введите 1, 2, 3 или q."
        )


def collect_interactive_operations(
    results,
    include_safe=True,
):
    operations = []

    decisions = []

    stopped = False

    # -----------------------------------------
    # SAFE
    # -----------------------------------------

    safe_enabled = False

    if include_safe:
        safe_enabled = ask_yes_no(
            "Добавить все SAFE-изменения "
            "в очередь записи?",
            default=False,
        )

    if safe_enabled:
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

    # -----------------------------------------
    # REVIEW / ASK
    # -----------------------------------------

    for data in results:
        if stopped:
            break

        plan = data.get(
            "change_plan"
        )

        if not plan:
            continue

        from core.identity_review import needs_identity_confirmation
        data.pop('_manual_identity_confirmed', None)
        identity_changes_declined = False
        if needs_identity_confirmation(data) and any(
                (plan.get(name) or {}).get('status') in {'REVIEW', 'ASK'}
                for name in ('artist', 'title', *RELEASE_FIELD_NAMES)):
            confirmation = confirm_identity_for_review(data)
            decisions.append({'file': data.get('file'), 'decision': 'identity_' + confirmation})
            if confirmation != 'confirmed':
                path = str(data.get('path') or '')
                operations[:] = [op for op in operations if op['path'] != path]
            if confirmation == 'quit':
                stopped = True
                break
            if confirmation != 'confirmed':
                continue
            data['_manual_identity_confirmed'] = True

        artist_field = (
            plan.get("artist")
            or {}
        )

        title_field = (
            plan.get("title")
            or {}
        )

        # -------------------------------------
        # СВЯЗАННЫЕ ARTIST + TITLE
        # -------------------------------------

        linked = (
            title_field.get(
                "dependency_group"
            )
            == "artist_title_credit"
        )
        linked = linked or (artist_field.get('status') == title_field.get('status') == 'REVIEW'
                            and artist_field.get('proposed') is not None and title_field.get('proposed') is not None
                            and artist_field.get('current') != artist_field.get('proposed')
                            and title_field.get('current') != title_field.get('proposed'))
        linked_handled = False

        if (
            linked
            and artist_field.get(
                "status"
            )
            == "REVIEW"
            and title_field.get(
                "status"
            )
            == "REVIEW"
        ):
            linked_handled = True
            decision = (
                review_linked_artist_title(
                    data,
                    artist_field,
                    title_field,
                )
            )

            if decision == "quit":
                stopped = True
                break

            if decision == "accept":
                for field_name, field in (
                    (
                        "artist",
                        artist_field,
                    ),
                    (
                        "title",
                        title_field,
                    ),
                ):
                    operation = (
                        make_operation(
                            data,
                            field_name,
                            {**field, 'status': 'MANUAL'},
                        )
                    )

                    if operation:
                        operations.append(
                            operation
                        )

                decisions.append({
                    "file": data.get(
                        "file"
                    ),
                    "fields": [
                        "artist",
                        "title",
                    ],
                    "decision": (
                        "accepted_linked"
                    ),
                })

            else:
                identity_changes_declined = True
                decisions.append({
                    "file": data.get(
                        "file"
                    ),
                    "fields": [
                        "artist",
                        "title",
                    ],
                    "decision": (
                        "kept_linked"
                    ),
                })

        # -------------------------------------
        # ОБЫЧНЫЕ REVIEW
        # -------------------------------------

        for field_name in ("artist", "title"):
            if stopped:
                break

            field = (
                plan.get(
                    field_name
                )
                or {}
            )

            if (
                field.get("status")
                != "REVIEW"
            ):
                continue

            if (
                linked_handled
                and field_name
                in {
                    "artist",
                    "title",
                }
            ):
                continue

            decision = (
                review_single_field(
                    data,
                    field_name,
                    field,
                )
            )

            if decision == "quit":
                stopped = True
                break

            if decision == "accept":
                operation = make_operation(
                    data,
                    field_name,
                    {**field, 'status': 'MANUAL'},
                )

                if operation:
                    operations.append(
                        operation
                    )
            else:
                identity_changes_declined = True

            decisions.append({
                "file": data.get(
                    "file"
                ),
                "field": field_name,
                "decision": decision,
            })

        if _manual_identity_corrected(data):
            from core.identity_review import proposed_identity
            _invalidate_release_proposals(data)
            path = str(data.get('path') or '')
            operations[:] = [op for op in operations if not (op['path'] == path and op['field'] in RELEASE_FIELD_NAMES)]
            decisions.append({'file': data.get('file'), 'decision': 'release_skipped_after_identity_corrected',
                              'corrected_identity': proposed_identity(data)})
            print('Artist/Title исправлены вручную. Поля релиза и обложка прежнего кандидата пропущены; нужен новый поиск релиза.')
            if stopped:
                break
            continue

        if stopped:
            break

        if identity_changes_declined:
            path = str(data.get('path') or '')
            operations[:] = [op for op in operations if not (op['path'] == path and op['field'] in RELEASE_FIELD_NAMES)]
            decisions.append({'file': data.get('file'), 'decision': 'release_skipped_after_identity_declined'})
            continue

        release_review = [(name, plan[name]) for name in RELEASE_FIELD_NAMES
                          if isinstance(plan.get(name), dict) and plan[name].get('status') == 'REVIEW']
        album_ask = (plan.get('album') or {}).get('status') == 'ASK'
        if release_review and not album_ask:
            release_decision = review_release_fields(data, release_review)
            decisions.append({'file': data.get('file'), 'fields': [n for n, _ in release_review],
                              'decision': 'release_' + release_decision})
            if release_decision == 'accept':
                for name, field in release_review:
                    operation = make_operation(data, name, {**field, 'status': 'MANUAL'})
                    if operation:
                        operations.append(operation)

        # -------------------------------------
        # ASK — ALBUM
        # -------------------------------------

        album_field = (
            plan.get("album")
            or {}
        )

        if (
            album_field.get("status")
            == "ASK"
        ):
            print_separator()

            print(
                data.get(
                    "file",
                    "неизвестный файл",
                )
            )

            release_selection = (
                data.get(
                    "release_context_selection"
                )
                or {}
            )

            release_options = (
                release_selection.get(
                    "options"
                )
                or []
            )

            # Если ASK пришёл именно от ReleaseContext v2,
            # выбираем конкретный релиз, а не голую строку Album.
            if (
                release_selection.get("status")
                == "ASK"
                and release_options
            ):
                selection = (
                    choose_release_context(
                        data,
                        release_options,
                    )
                )
            else:
                selection = choose_option(
                    "Album",
                    album_field,
                )

            if (
                selection["action"]
                == "quit"
            ):
                stopped = True
                break

            if (
                selection["action"]
                == "select"
            ):
                context = selection.get(
                    "context"
                )

                if context:
                    operations.extend(
                        build_manual_release_operations(
                            data,
                            context,
                        )
                    )
                else:
                    value = selection[
                        "value"
                    ]

                    custom_field = {
                        "current": (
                            album_field.get(
                                "current"
                            )
                            or ""
                        ),
                        "proposed": value,
                        "status": (
                            "MANUAL"
                        ),
                    }

                    operation = make_operation(
                        data,
                        "album",
                        custom_field,
                    )

                    if operation:
                        operations.append(
                            operation
                        )

            decisions.append({
                "file": data.get(
                    "file"
                ),
                "field": "album",
                **selection,
            })

        # -------------------------------------
        # ASK — COVER
        #
        # Пока только запоминаем выбор.
        # Физическая запись картинки будет позже.
        # -------------------------------------

        cover_field = (
            plan.get("cover")
            or {}
        )

        if (
            cover_field.get("status")
            == "ASK"
            and cover_field.get(
                "options"
            )
        ):
            print_separator()

            print(
                data.get(
                    "file",
                    "неизвестный файл",
                )
            )

            selection = choose_option(
                "Cover",
                cover_field,
            )

            if (
                selection["action"]
                == "quit"
            ):
                stopped = True
                break

            decisions.append({
                "file": data.get(
                    "file"
                ),
                "field": "cover",
                **selection,
                "note": (
                    "Обложка пока "
                    "физически не записывается."
                ),
            })

    return {
        "operations": operations,
        "decisions": decisions,
        "stopped": stopped,
    }
