"""Isolated adapter: the CLI engine remains authoritative for every decision."""
import base64
import copy
import builtins
import json
import time
import os
import sys
import threading
import _thread
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Both ends use UTF-8 explicitly; JSON on the pipe is ASCII escaped too.
for channel in (sys.stdin, sys.stdout, sys.stderr):
    if hasattr(channel, 'reconfigure'):
        channel.reconfigure(encoding='utf-8', errors='strict')
wire = sys.stdout
prompt_serial = 0
lock = threading.Lock()
library_cache = None
from core.covers import CoverSession
covers = CoverSession(ROOT)
from ui.protocol import RESULT_BATCH_SIZE, encode_event

def emit(kind, **data):
    if kind=='results' and len(data.get('items',[]))>RESULT_BATCH_SIZE:
        records=data['items']
        for offset in range(0,len(records),RESULT_BATCH_SIZE):
            emit('results_chunk',items=records[offset:offset+RESULT_BATCH_SIZE],offset=offset,total=len(records))
        emit('results_complete',total=len(records))
        return
    if library_cache is not None and kind in ('results', 'results_chunk', 'edited', 'written', 'cover_updated', 'cover_updates'):
        records = data.get('items', []) if kind in ('results','results_chunk') else ([data['item']] if kind in ('edited','cover_updated') else [u['item'] for u in data.get('updates', [])])
        for record in records: covers.attach(record)
        try: library_cache.save(records)
        except Exception as error:
            with lock:
                wire.write(json.dumps({'kind':'warning','message':'Не удалось обновить локальный кеш библиотеки: '+str(error)}, ensure_ascii=True)+'\n')
                wire.flush()
    with lock:
        wire.write(encode_event({'kind': kind, **data}) + '\n')
        wire.flush()

class Transcript:
    encoding = 'utf-8'
    def __init__(self):
        self.tail = ''
        self.file = None
    def write(self, text):
        self.tail = (self.tail + text)[-50000:]
        if self.file:
            self.file.write(text); self.file.flush()
        return len(text)
    def flush(self):
        if self.file: self.file.flush()
    def isatty(self): return False

stream = Transcript()
sys.stdout = stream

class ReviewCancelled(Exception):
    pass

def ask(prompt=''):
    global prompt_serial
    prompt_serial += 1
    emit('prompt', text=stream.tail, prompt=prompt, prompt_id=prompt_serial)
    stream.tail = ''
    line = sys.stdin.readline()
    if not line: raise EOFError('Окно приложения закрыто')
    reply = json.loads(line)
    if reply.get('cancel'): raise ReviewCancelled()
    answer = reply.get('answer', '')
    stream.write(prompt + str(answer) + '\n')
    return str(answer)

builtins.input = ask

def main():
    global library_cache
    emit('started', pid=os.getpid(), ui_api_version=60)
    request = json.loads(sys.stdin.readline())
    from core.library_cache import LibraryCache
    library_cache = LibraryCache(ROOT/'ui_runtime/library.sqlite3')
    for path in request['files']:
        try: path.encode('utf-8', errors='strict')
        except UnicodeEncodeError:
            raise ValueError('Некорректный Unicode в пути: ' + ascii(path) + '. Выберите папку заново; переименовывать файлы автоматически нельзя.')
    import app
    from core.cache_utils import atomic_write_json
    from core.preferences import save_preferences
    files = [Path(p) for p in request['files']]
    retained = []
    if request.get('retained_path'):
        seed = Path(request['retained_path'])
        retained = json.loads(seed.read_text(encoding='utf-8'))
        seed.unlink(missing_ok=True)
    from ui.uniformity import UniformitySession
    from core.uniformity import AuditCancelled
    uniformity=UniformitySession(ROOT)
    audit_cancelled=False
    def audit_progress(done,total,path):
        emit('progress',stage='audit_scanned',file=path,details={'done':done,'total':total})
    def audit_cancel():return (ROOT/'ui_runtime/audit_stop').exists()
    retained_paths = {os.path.normcase(str(Path(d['path']).resolve())) for d in retained}
    # Explicitly approved files need only a local signature/tag check when
    # continuing a session, even if automatic recognition had been incomplete.
    manually_completed = []
    if request['mode'] == 'resume' and not request.get('audit_only'):
        try:
            from ui.editor import WrittenStore, path_key
            from core.scanner import scan_file
            resume_store = WrittenStore(ROOT/'ui_runtime/written_tracks.json')
            for path in files:
                if os.path.normcase(str(path.resolve())) in retained_paths: continue
                record = resume_store.items.get(path_key({'path':str(path)})) or {}
                if record.get('completion_kind') != 'manual': continue
                try:
                    fresh = scan_file(path)
                    resume_store.attach(fresh)
                    if fresh.get('_ui_written'):
                        manually_completed.append(fresh)
                        emit('progress', stage='checkpoint_restored', file=str(path))
                except Exception:
                    pass  # A changed/unreadable file follows ordinary analysis.
        except Exception as error:
            emit('warning', message='Не удалось прочитать отметки исправления: '+str(error))
    completed_paths = {d['path'] for d in manually_completed}
    active_files = [p for p in files if str(p) not in completed_paths and os.path.normcase(str(p.resolve())) not in retained_paths]
    app.preferences = request['preferences']
    save_preferences(app.preferences)
    app.requested_checkpoint_mode = request['mode']
    app.choose_input = lambda: active_files
    app.choose_write_mode = lambda: '1'
    original_stage = app._journal_stage
    stop = ROOT / 'ui_runtime' / 'stop'
    finished = threading.Event()
    interrupted = threading.Event()
    def stage(file, name, **details):
        original_stage(file, name, **details)
        if name in ('run_configuration', 'prepare_started', 'scanned', 'fingerprint_done', 'recognition_done', 'plan_done', 'checkpoint_restored', 'shazam_transport', 'analysis_done', 'summary_tables_done', 'write_mode_waiting', 'fingerprint_failed'):
            emit('progress', stage=name, file=str(file or ''), details=details)
        if stop.exists() and not interrupted.is_set() and threading.current_thread() is threading.main_thread():
            interrupted.set()
            raise KeyboardInterrupt
    app._journal_stage = stage
    results = []
    export = app.export_change_plan
    def capture(data, *args, **kwargs):
        by_path = {d['path']:d for d in [*retained, *manually_completed, *data]}
        results[:] = [by_path[str(p)] for p in files if str(p) in by_path]
        emit('progress', stage='export_started', file='')
        path = export(results, *args, **kwargs)
        emit('export', path=str(path))
        return path
    app.export_change_plan = capture
    def watch_stop():
        while not finished.wait(0.15):
            if stop.exists():
                app.cancel_pending_waits()
                if not interrupted.is_set():
                    interrupted.set()
                    _thread.interrupt_main()
                return
    threading.Thread(target=watch_stop, daemon=True).start()
    try:
        if request['mode'] == 'browse':
            def local_progress(done,total,path):
                emit('progress',stage='library_loaded',file=path,details={'done':done,'total':total})
            try:results[:]=library_cache.load(files,local_progress)
            except Exception as error:
                emit('warning',message='Локальный кеш недоступен; читаем только теги файлов: '+str(error))
                from core.uniformity import read_collection
                results[:]=read_collection(files,local_progress)
                for data in results:data['change_plan']={'identity':'UNSCANNED'}
            code=0
        elif request.get('audit_only'):
            try:results[:]=uniformity.run([str(p) for p in files],retained,request.get('audit_rules'),audit_progress,audit_cancel)
            except AuditCancelled:results[:]=retained;audit_cancelled=True
            code=0
        elif active_files:
            code = app.run_app()
        else:
            capture([], ROOT/'reports', preferences=app.preferences)
            code = 0
    except KeyboardInterrupt:
        code = 130
    finally:
        finished.set()
    if code or (not results and files and not request.get('audit_only')):
        emit('stopped' if code == 130 else 'error', message='Проверка остановлена; этапы сохранены.' if code == 130 else stream.tail[-8000:])
        return
    from core.extra_tags import hydrate_remixer
    for data in ([] if request['mode']=='browse' else results):
        try: hydrate_remixer(data)
        except Exception as error:
            data['_ui_draft_warning'] = 'Не удалось прочитать поле ремиксера: '+str(error)
    from ui.editor import DraftStore, build_draft, preview_operations, draft_operations, refresh_after_write, WrittenStore, draft_matches_current, mark_completed
    drafts = None
    try:
        drafts = DraftStore(ROOT/'ui_runtime/manual_drafts.json')
        for data in results: drafts.attach(data)
    except Exception as error:
        emit('warning', message='Ручные черновики не загружены: '+str(error))
    written_store = None
    try:
        written_store = WrittenStore(ROOT/'ui_runtime/written_tracks.json')
        restored = written_store.recover_from_plans(results, ROOT/'reports')
        if restored: emit('progress',stage='written_marks_restored',file=str(restored))
        for data in results: written_store.attach(data)
        written_store.flush_approval_migrations()
        if uniformity.active:
            uniformity.recompute(results,changed_paths=[d['path'] for d in results if d.get('_ui_audit_approval')])
    except Exception as error:
        emit('warning', message='Не удалось восстановить отметки записи: '+str(error))
    emit('progress', stage='loading_results', file='')
    # Generated caches are maintained between operations, with no file writes.
    from core.storage import maintain
    try:
        if request.get('maintain_cache'):
            emit('progress',stage='cache_maintenance',file='Проверяем срок хранения временных данных')
            maintain(ROOT)
    except Exception as error:emit('warning',message='Обслуживание кэша: '+str(error))
    emit('results', items=results)
    if request.get('audit_only'):
        if audit_cancelled:emit('audit_cancelled')
        else:emit('audit_report',**uniformity.event(results,open_view=True))

    from core.tag_writer import collect_safe_operations, execute_write_plan, create_backup_session
    from core.interactive_review import collect_interactive_operations
    from core.run_journal import RunJournal
    from core.file_renamer import preview_renames, execute_rename, refresh_after_rename
    pending = None
    pending_kind = 'tags'
    searches = {}
    search_lock = threading.Lock()

    def find_releases(command, snapshot):
        request_id = command['request_id']
        try:
            from core.release_discovery import discover
            contexts,warnings=discover(snapshot,command['artist'],command['title'],command.get('album_query',''),command.get('source_url',''))
            error=None
        except Exception as failure:
            contexts,warnings,error=[],[],str(failure)
        with search_lock:
            searches[request_id]=(snapshot['path'],snapshot.get('analysis_file_signature'),contexts,warnings,error)
        emit('release_search_ready',path=snapshot['path'],request_id=request_id)

    for line in sys.stdin:
        command = json.loads(line)
        try:
            action = command['action']
            if action not in ('write', 'discard'):
                pending = None
                pending_kind = 'tags'
            if action in ('cover_search', 'cover_diagnose', 'cover_write', 'cover_import', 'cover_source', 'cover_upscale', 'cover_mark', 'cover_queue', 'cover_unqueue', 'cover_commit'):
                indices = command.get('indices', [])
                if not indices or any(type(i) is not int or not 0 <= i < len(results) for i in indices):
                    raise ValueError('Выберите треки для обложек.')
                failures = []
                partial = 0
                incomplete = 0
                unavailable_sources = Counter()
                warning_counts = Counter()
                prepared_tracks=0
                not_found_tracks=0
                candidate_count=0
                processed = 0
                written_paths=[]
                marked_paths=[]
                upscaled_choices={}
                imported_choices={}
                from core.cover_batch import CoverSearchBatch, BatchArtworkDownloads
                from core.tag_writer import create_backup_session
                writing = action in ('cover_write', 'cover_commit')
                searching = action in ('cover_search', 'cover_diagnose')
                backup_session = create_backup_session(ROOT) if action == 'cover_commit' else None
                write_downloads = BatchArtworkDownloads() if action == 'cover_commit' else None
                updates = []
                queue_only = action in ('cover_queue', 'cover_unqueue')
                last_update = 0.0
                cancelled=lambda:(ROOT/'ui_runtime/cover_stop').exists()
                query = command.get('query') if action == 'cover_search' else None
                refreshing = action == 'cover_search' and command.get('restore_originals') is True
                retrying = action == 'cover_search' and command.get('retry_failed_sources') is True
                refresh_downloads = BatchArtworkDownloads() if refreshing else None
                retained_choices = {}
                saved_queries = {}
                provider_filters = {}
                deezer_only = set()
                deferred = 0
                deezer_pause = None
                restored_originals = 0
                work = list(dict.fromkeys(indices))
                if retrying:
                    from core.cover_catalogs import deezer_cooldown
                    from core.cover_search import retry_providers
                    from core.extra_catalogs import retry_access_denied
                    retryable = []
                    sources = set()
                    for index in work:
                        previous = covers.attach(results[index])
                        providers = retry_providers(previous)
                        if providers == []:
                            processed += 1
                            count = len(previous.get('candidates') or [])
                            prepared_tracks += bool(count)
                            candidate_count += count
                            continue
                        provider_filters[index] = providers
                        if providers == ['deezer']:
                            deezer_only.add(index)
                        saved_queries[index] = previous.get('query')
                        retryable.append(index)
                        sources.update(providers if providers is not None else ('discogs', 'lastfm'))
                    work = retryable
                    retry_access_denied(sources)
                if refreshing:
                    from core.cover_search import needs_publication_search

                    verified = set()
                    work = []
                    for position, index in enumerate(dict.fromkeys(indices)):
                        if cancelled():
                            break
                        data = results[index]
                        if position % 8 == 0:
                            emit('progress', stage='cover_search', file='Проверяем сохранённые оригиналы: '+data.get('file',''),
                                 details={'done':processed,'total':len(indices)})
                        try:
                            keep, restored, needs_search = covers.restore_originals(data, refresh_downloads, verified, cancelled)
                        except (OSError, ValueError):
                            keep, restored, needs_search = set(), 0, True
                        restored_originals += restored
                        if cancelled():
                            break
                        state = data.get('_ui_cover') or {}
                        add_publication = not needs_search and needs_publication_search(state)
                        if needs_search or add_publication:
                            work.append(index)
                            retained_choices[index] = keep
                            saved_queries[index] = state.get('query')
                            if add_publication:
                                provider_filters[index] = ['soundcloud']
                        else:
                            prepared_tracks += 1
                            candidate_count += len(data['_ui_cover']['candidates'])
                            processed += 1
                            if restored:
                                updates.append({'index': index, 'item': data})
                                if len(updates) >= 8:
                                    emit('cover_updates', updates=updates)
                                    updates = []
                    emit('progress', stage='cover_search', file='Проверка сохранённых оригиналов завершена.',
                         details={'done':processed,'total':len(indices)})
                # Deezer already serializes its API calls. Check its pause before
                # scheduling each Deezer-only retry so a refusal doesn't fan out.
                context=CoverSearchBatch(results,work,query,
                    progress=lambda i,message:emit('progress',stage='cover_search',file=results[i].get('file','')+': '+message),
                    cancelled=cancelled,queries=saved_queries,downloads=refresh_downloads,
                    provider_filters=provider_filters,on_demand=deezer_only) if searching and (len(work)>1 or refreshing and work) and not cancelled() else nullcontext(None)
                researched_tracks = 0
                with context as batch:
                    for index in work:
                        if (searching or action == 'cover_commit') and cancelled():
                            break
                        if index in deezer_only and (cooldown := deezer_cooldown()):
                            reason, remaining = cooldown
                            deezer_pause = {'reason': reason, 'until': time.time() + remaining}
                            deferred += 1
                            continue
                        data = results[index]
                        try:
                            if searching:
                                researched_tracks += 1
                                cover_state=covers.search(data,query or saved_queries.get(index),
                                    progress=lambda message:emit('progress',stage='cover_search',file=message),
                                    cancelled=cancelled,prepared=batch.result(index) if batch is not None else None,
                                    downloads=batch.downloads if batch is not None else None,
                                    retained_choices=retained_choices.get(index, ()),providers=provider_filters.get(index))
                                if cover_state.get('diagnosis_outcome')=='error':failures.append(data.get('file','')+': '+'; '.join(cover_state.get('errors') or ['Поиск не завершён.']))
                                elif cover_state.get('diagnosis_outcome')=='partial':partial+=1
                                elif cover_state.get('diagnosis_outcome')=='incomplete':incomplete+=1
                                unavailable_sources.update(cover_state.get('unavailable_sources', []))
                                warning_counts.update(cover_state.get('errors', []))
                                count=len(cover_state.get('candidates') or [])
                                prepared_tracks+=bool(count);candidate_count+=count
                                not_found_tracks += not count and cover_state.get('diagnosis_outcome') == 'complete'
                            elif action == 'cover_upscale':
                                choice=covers.upscale(data,command.get('candidate_id'),command.get('choice_key'),command.get('crop_square',False),command.get('crop_position',50))
                                upscaled_choices[data['path']]=choice['choice_key']
                            elif action == 'cover_source':
                                covers.import_source(data,command['source_url'])
                            elif action == 'cover_mark':
                                covers.mark(data,command.get('decision'))
                                if command.get('decision') == 'review':
                                    covers.set_queued(data)
                                marked_paths.append(data['path'])
                            elif action in ('cover_queue', 'cover_unqueue'):
                                choice = command.get('choice_key') if action == 'cover_queue' else None
                                options = {key: command[key] for key in ('review_confirmed', 'upscale_small', 'crop_square', 'crop_position') if key in command}
                                if choice not in (None, 'current'):
                                    options['original_blob'] = base64.b64decode(command['original_image'], validate=True)
                                    options['original_id'] = command.get('original_id')
                                covers.set_queued(data, choice, **options)
                                marked_paths.append(data['path'])
                            elif action == 'cover_import':
                                candidate=covers.import_image(data,base64.b64decode(command['image'],validate=True),command.get('origin','file'))
                                imported_choices[data['path']]=candidate['choice_key']
                            else:
                                if action == 'cover_commit':
                                    covers.write_queued(data, backup_session, write_downloads)
                                else:
                                    covers.write(data, command.get('candidate_id'),command.get('replace_existing',False),command.get('review_confirmed',False),command.get('choice_key'),command.get('upscale_small',False),command.get('confirm_embedded',False),command.get('crop_square',False),command.get('crop_position',50))
                                written_paths.append(data['path'])
                                try:
                                    if data.get('_ui_draft') and drafts is not None: drafts.save(data, data['_ui_draft'])
                                    if written_store is not None and data.get('_ui_written'): written_store.save_batch([data])
                                except Exception as error:
                                    emit('warning', message='Обложка вшита, но не удалось обновить данные редактора: '+str(error))
                            if queue_only:
                                updates.append({'index': index, 'cover': data.get('_ui_cover') or {}})
                            else:
                                updates.append({'index': index, 'item': data})
                        except Exception as error:
                            failures.append(data.get('file','') + ': ' + str(error))
                            if writing:
                                if data.get('_ui_cover',{}).get('write_restored') and not data.get('_ui_write_recheck_required'):
                                    try:
                                        if data.get('_ui_draft') and drafts is not None:drafts.save(data,data['_ui_draft'])
                                        if data.get('_ui_written') and written_store is not None:written_store.save_batch([data])
                                    except Exception as persistence_error:
                                        emit('warning',message='Исходный файл восстановлен, но отметки не удалось обновить: '+str(persistence_error))
                                if action == 'cover_commit' and data.get('_ui_cover'):
                                    data['_ui_cover']['write_error'] = str(error)
                            if not queue_only:
                                updates.append({'index': index, 'item': data})
                        processed += 1
                        if updates and (len(indices) <= 8 or len(updates) >= 8 or time.monotonic() - last_update >= 1):
                            if queue_only:
                                emit('cover_queue_updates', updates=updates)
                            elif len(updates) == 1:
                                emit('cover_updated', **updates[0])
                            else:
                                emit('cover_updates', updates=updates)
                            updates = []
                            last_update = time.monotonic()
                        emit('progress', stage='cover_write' if writing else 'cover_search', file=data.get('file',''),details={'done':processed,'total':len(indices),'errors':len(failures)})
                if updates:
                    emit('cover_queue_updates' if queue_only else 'cover_updates', updates=updates)
                if uniformity.active and written_paths:
                    try:
                        uniformity.recompute(results, changed_paths=written_paths)
                        emit('audit_report', **uniformity.event(results))
                    except Exception as error:
                        emit('warning', message='Не удалось обновить отчёт оформления: '+str(error))
                summary = {
                    'processed': processed, 'total': len(indices), 'errors': len(failures),
                    'partial': partial, 'incomplete': incomplete,
                    'unavailable_sources': dict(unavailable_sources), 'prepared_tracks': prepared_tracks,
                    'candidate_count': candidate_count,
                    'not_found_tracks': not_found_tracks,
                    'cancelled': processed + deferred < len(indices) or cancelled(),
                    'performance': batch.stats() if batch is not None else {},
                }
                if searching:
                    summary['error_details'] = failures[:5]
                    summary['warning_details'] = [message+f' — треков: {count}'
                                                  for message, count in warning_counts.most_common(12)]
                    message = f'Обложки: {processed} из {len(indices)}. С вариантами: {prepared_tracks}.'
                    if refreshing:
                        summary.update(restored_originals=restored_originals, researched_tracks=researched_tracks)
                        message += f' Восстановлено оригиналов: {restored_originals}. Поиск в каталогах: {researched_tracks}.'
                    if unavailable_sources:
                        sources = ', '.join(f'{source} ({count})' for source, count in sorted(unavailable_sources.items()))
                        message += f' Сбои источников: {sources}.'
                    if incomplete:
                        message += f' Нужен повторный поиск: {incomplete}.'
                    if deferred:
                        summary.update(deferred=deferred, deezer_pause=deezer_pause)
                        message += f' Отложено до окончания паузы Deezer: {deferred}. {deezer_pause["reason"]}'
                    if failures:
                        message += f' Ошибок: {len(failures)}.' if len(indices) > 1 else ' ' + failures[0]
                else:
                    message = (f'Отметки обложек сохранены: {len(marked_paths)}' if action == 'cover_mark'
                               else f'Обложки: обработано {processed} / {len(indices)}')
                    message += f'. Ошибок {len(failures)}: ' + '; '.join(failures[:5]) if failures else '.'
                emit('cover_done', operation=action, written_paths=written_paths, marked_paths=marked_paths,
                     upscaled_choices=upscaled_choices, imported_choices=imported_choices, summary=summary, message=message)
            elif action == 'release_search':
                index=command['index']
                threading.Thread(target=find_releases,args=(command,copy.deepcopy(results[index])),daemon=True).start()
            elif action == 'release_search_sync':
                request_id=command['request_id']
                with search_lock:
                    path,signature,contexts,warnings,error=searches.pop(request_id)
                index=next((i for i,item in enumerate(results) if item['path']==path and item.get('analysis_file_signature')==signature),None)
                if index is None:
                    error='Трек изменился во время поиска. Выполните поиск заново.'
                elif not error:
                    data=results[index]
                    from ui.editor import release_options
                    known=release_options(data)
                    added=[context for context in contexts if context not in known]
                    contexts=[*(data.get('_ui_extra_releases') or []),*added]
                    data['_ui_extra_releases']=contexts
                    if added:
                        try:library_cache.save([data])
                        except Exception as failure:
                            warnings.append('Издания открыты, но сохранить их в локальном кеше не удалось: '+str(failure))
                emit('releases_found',path=path,request_id=request_id,index=index,contexts=contexts,warnings=warnings,error=error)
            elif action == 'tag_recheck':
                from ui.editor import check_saved_tags
                indices = command.get('indices', [])
                if not indices or any(type(i) is not int or not 0 <= i < len(results) for i in indices):
                    raise ValueError('Выберите треки для проверки тегов.')
                indices = list(dict.fromkeys(indices))
                def tag_progress(done, total, path):
                    if done % 20 == 0 or done == total:
                        emit('progress', stage='tag_recheck', file=Path(path).name,
                             details={'done': done, 'total': total})
                checks = check_saved_tags([results[i] for i in indices], uniformity.rules, tag_progress)
                for index, check in zip(indices, checks):
                    results[index]['_ui_tag_check'] = check
                emit('tag_rechecked', updates=[{'index':i, 'check':c} for i,c in zip(indices,checks)],
                     count=len(checks), flagged=sum(bool(c['issues']) and not c['error'] for c in checks),
                     errors=sum(c['error'] for c in checks))
            elif action == 'audit':
                try:
                    updated=uniformity.run(command['files'],results,command.get('rules'),audit_progress,audit_cancel,drafts,written_store)
                    results[:]=updated
                    emit('results',items=results)
                    emit('audit_report',**uniformity.event(results,open_view=True))
                except AuditCancelled:emit('audit_cancelled')
            elif action == 'audit_preview':
                indices=command.get('indices')
                if not isinstance(indices,list) or not indices or any(type(i) is not int or not 0<=i<len(results) for i in indices):raise ValueError('Выберите треки для исправления оформления.')
                from ui.editor import has_preview_work
                active=[i for i in dict.fromkeys(indices) if has_preview_work(results[i],uniformity=True)]
                pending,skipped=uniformity.preview(results,active)
                pending_kind='uniformity'
                emit('preview',operations=pending,operation_kind='uniformity',skipped=skipped,selected_count=len(active))
            elif action == 'collection_update':
                removed = set(command.get('remove_paths', []))
                selection_removed=set(command.get('selection_remove',removed))
                request['files']=[p for p in request['files'] if p not in selection_removed]
                selection_known=set(request['files'])
                request['files'].extend(p for p in command.get('selection_add',[]) if p not in selection_known)
                results[:] = [d for d in results if d['path'] not in removed]
                if command.get('append_path'):
                    seed = Path(command['append_path'])
                    additions = json.loads(seed.read_text(encoding='utf-8'))
                    seed.unlink(missing_ok=True)
                    known = {d['path'] for d in results}
                    results.extend(d for d in additions if d['path'] not in known)
                flags = {d['path']:d for d in command.get('flags', [])}
                for data in results:
                    patch = flags.get(data['path'])
                    if patch:
                        data['_ui_write_recheck_required'] = True
                        data['_ui_draft_warning'] = patch['warning']
                emit('collection_synced', paths=[d['path'] for d in results], message=command.get('message','Список обновлён'))
                if uniformity.active:
                    for changed in command.get('flags',[]):
                        if changed['path'] in uniformity.cache:uniformity.cache[changed['path']]['_audit_stale']=True
                    uniformity.recompute(results,changed_paths=[d['path'] for d in command.get('flags',[])]);emit('audit_report',**uniformity.event(results))
            elif action in ('edit', 'edit_preview', 'reset_edit'):
                index = command['index']
                if type(index) is not int or not 0 <= index < len(results): raise ValueError('Трек недоступен')
                if drafts is None: raise ValueError('Файл ручных черновиков повреждён. Сохраните manual_drafts.json для диагностики.')
                data = results[index]
                if action == 'reset_edit': drafts.remove(data)
                else: drafts.save(data, build_draft(data, command))
                message = ('Черновик сброшен. Теги файла не изменены.' if action == 'reset_edit' else
                           'Все значения уже записаны в тегах. Нажмите «Отметить исправленными», чтобы завершить обработку.' if draft_matches_current(data) else
                           'Черновик сохранён. Для записи в файл откройте «Посмотреть изменения» и подтвердите запись.')
                emit('edited', index=index, item=data, message=message)
                if action == 'edit_preview':
                    pending = preview_operations([data])
                    emit('preview', operations=pending,selected_count=1)
            elif action == 'complete':
                indices = command.get('indices')
                if not isinstance(indices,list) or not indices or any(type(i) is not int or not 0 <= i < len(results) for i in indices):
                    raise ValueError('Выберите исправленные треки.')
                outcome, updates = [], []
                for index in dict.fromkeys(indices):
                    data = results[index]
                    result = {'path':data['path'], 'file':data['file'], 'success':False}
                    try:
                        release=None
                        if command.get('release_index') is not None:
                            if len(indices)!=1:raise ValueError('Выбор издания доступен для одного трека.')
                            from ui.editor import current_values
                            release=build_draft(data,{**command,'values':current_values(data)})['release']
                        warnings = mark_completed(data, drafts, written_store,release)
                        result.update(success=True, audio_changed=False)
                        if warnings: result['state_warnings'] = warnings
                        updates.append({'index':index,'item':data})
                    except Exception as error:
                        result['error'] = str(error)
                    outcome.append(result)
                if uniformity.active:
                    uniformity.accept_confirmations(u['item']['path'] for u in updates)
                    uniformity.recompute(results,changed_paths=[u['item']['path'] for u in updates]);emit('audit_report',**uniformity.event(results))
                emit('written', operation_kind='complete', results=outcome, updates=updates)
            elif action == 'rename_preview':
                indices = command.get('indices')
                if not isinstance(indices,list) or not indices or any(type(i) is not int or not 0 <= i < len(results) for i in indices):
                    raise ValueError('Выберите треки для переименования.')
                name = command.get('name')
                if name is not None and (not isinstance(name,str) or len(indices)!=1):
                    raise ValueError('Ручное имя доступно для одного файла.')
                pending, skipped = preview_renames(results, indices, name)
                pending_kind = 'rename'
                emit('preview', operations=pending, operation_kind='rename', skipped=skipped,selected_count=len(set(indices)))
            elif action in ('preview', 'review'):
                if drafts is None: raise ValueError('Не удалось загрузить ручные черновики. Запись заблокирована до восстановления файла.')
                indices = command['indices']
                from ui.editor import has_preview_work
                # Checkboxes persist across writes. Only the current work queue
                # belongs to this preview; old evidence must not be copied again.
                selected = [results[i] for i in indices if action=='review' or has_preview_work(results[i])]
                if action == 'review':
                    selected=copy.deepcopy(selected)
                    stream.tail = ''
                    log = ROOT / 'reports' / 'UI_manual_review.txt'
                    stream.file = log.open('a', encoding='utf-8')
                    try:
                        from ui.editor import prepare_preview
                        automatic = [d for d in selected if not d.get('_ui_draft') and not d.get('_ui_draft_warning') and not d.get('_ui_write_recheck_required')]
                        manual_pending,_=prepare_preview([d for d in selected if d.get('_ui_draft') or d.get('_ui_draft_warning') or d.get('_ui_write_recheck_required')])
                        review = collect_interactive_operations(automatic, include_safe=True) if automatic else {'operations':[], 'decisions':[]}
                        pending = review['operations'] + manual_pending
                        export(selected, ROOT/'reports', preferences=app.preferences, review_decisions=review['decisions'])
                    finally:
                        stream.file.close(); stream.file = None
                else:
                    from ui.editor import prepare_preview
                    pending,skipped = prepare_preview(selected)
                from ui.editor import preview_skipped
                emit('preview', operations=pending, skipped=skipped if action=='preview' else preview_skipped(selected,pending),selected_count=len(selected))
            elif action == 'discard':
                pending = None; emit('ready')
            elif action == 'write':
                if pending is None: raise ValueError('Сначала откройте предпросмотр')
                if pending_kind == 'rename':
                    backup = create_backup_session(ROOT)
                    updates, outcome = [], []
                    for operation in pending:
                        result = execute_rename(operation, backup)
                        outcome.append(result)
                        if not result.get('renamed'): continue
                        index = operation['index']
                        data = results[index]
                        try:
                            warnings = refresh_after_rename(data, result, drafts, written_store)
                        except Exception as error:
                            # A successful filesystem operation must still reach
                            # the GUI if an ancillary metadata refresh fails.
                            data.update(path=result['new_path'], file=Path(result['new_path']).name,
                                        analysis_file_signature=None, _ui_write_recheck_required=True,
                                        _ui_draft_warning='Путь обновлён; перепроверьте трек после ошибки сохранения состояния.')
                            warnings = ['Состояние трека: '+str(error)]
                        updates.append({'index':index,'item':data})
                        # Persist the selection per file so a later failure never
                        # leaves successful earlier renames pointing at old paths.
                        request['files'] = [result['new_path'] if p == result['old_path'] else p for p in request['files']]
                        try:
                            atomic_write_json(ROOT/'ui_runtime/selection.json', request['files'])
                            atomic_write_json(ROOT/'config/last_selection.json', {'files':request['files']})
                        except Exception as error:
                            warnings.append('Прошлый набор: '+str(error))
                        try:
                            from core.run_state import save_completed_result
                            if data.get('analysis_file_signature'):
                                save_completed_result(data['path'], app.preferences, data)
                        except Exception as error:
                            warnings.append('Сохранённый анализ: '+str(error))
                        if warnings: result['state_warnings'] = warnings
                        # Save the updated mapping alongside the audio backup.
                        try: atomic_write_json(backup/'rename_results.json', outcome)
                        except Exception as error: result['journal_warning'] = str(error)
                    pending = None
                    if uniformity.active:
                        for result in outcome:
                            if result.get('renamed'):uniformity.cache.pop(result['old_path'],None)
                        uniformity.recompute(results,changed_paths=[u['item']['path'] for u in updates]);emit('audit_report',**uniformity.event(results))
                    emit('written', results=outcome, backup=str(backup), updates=updates, operation_kind='rename')
                    continue
                journal = RunJournal(ROOT/'reports', stream)
                backup = create_backup_session(ROOT)
                try:
                    journal.write('UI: запись выбранных изменений\n')
                    pending_paths={o['path'] for o in pending}
                    journal.write(json.dumps({'operations':pending, 'manual_drafts':[{'path':d['path'], **d['_ui_draft']} for d in results if d.get('_ui_draft') and d['path'] in pending_paths]}, ensure_ascii=False, default=str)+'\n')
                    outcome = execute_write_plan(pending, backup_root=backup, dry_run=False,
                        progress_callback=lambda result: journal.write(json.dumps(result, ensure_ascii=False, default=str)+'\n'))
                    updates = []
                    by_path = {data['path']:(i,data) for i,data in enumerate(results)}
                    for result in outcome:
                        match = by_path.get(result['path'])
                        if match is None: continue
                        index, data = match
                        try:
                            if pending_kind=='uniformity':uniformity.after_write(data,result,drafts)
                            else:
                                refresh_after_write(data,result,drafts)
                                if uniformity.active:uniformity.cache[data['path']]={k:data.get(k) for k in ('path','file','duration','analysis_file_signature','artist','title','remixer','album','albumartist','date','tracknumber','discnumber','raw_tags')}
                        except Exception as error:
                            data['analysis_file_signature'] = None
                            data['_ui_write_recheck_required'] = True
                            data['_ui_draft_warning'] = 'Не удалось обновить состояние после записи. Перепроверьте этот трек: '+str(error)
                        updates.append({'index':index,'item':data})
                    if written_store is not None:
                        try: written_store.save_batch([u['item'] for u in updates])
                        except Exception as error: emit('warning',message='Теги записаны, но отметки записи не сохранены: '+str(error))
                    pending = None
                    try: export([update['item'] for update in updates], ROOT/'reports', preferences=app.preferences, write_results=outcome)
                    except Exception as error: emit('warning', message='Запись выполнена, но итоговый JSON-план не сохранён: '+str(error))
                    if uniformity.active:
                        manual_paths={r['path'] for r in outcome if r.get('success') and any(
                            o.get('source')=='user_editor' for o in r.get('operations',[]))}
                        uniformity.accept_confirmations(u['item']['path'] for u in updates
                            if u['item']['path'] in manual_paths and not u['item'].get('_ui_write_recheck_required'))
                        uniformity.recompute(results,changed_paths=[u['item']['path'] for u in updates]);emit('audit_report',**uniformity.event(results))
                    emit('written', results=outcome, backup=str(backup), updates=updates,operation_kind='uniformity' if pending_kind=='uniformity' else 'tags')
                finally:
                    journal.close('ui_write_finished')
                pending = None
        except ReviewCancelled:
            pending = None
            emit('ready')
        except Exception as error:
            pending = None
            emit('error' if command.get('action')=='write' else 'action_error', message=str(error))

if __name__ == '__main__':
    try: main()
    except Exception:
        import traceback
        emit('error', message=traceback.format_exc())
