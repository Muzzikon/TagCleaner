"""TagCleaner local UI. Bind only to loopback; all mutations require a token."""
import json
import hashlib
import uuid
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
PYTHON = str(ROOT / '.venv-audio' / 'Scripts' / 'python.exe') if getattr(sys, 'frozen', False) else sys.executable
os.chdir(ROOT)
TOKEN = secrets.token_urlsafe(32)
UI_API_VERSION = 51
UI_BUILD = '2026.11.08-cover-preview-recovery'
STATE = {'phase':'idle', 'message':'Выберите папку с музыкой', 'done':0, 'total':0,
         'files':[], 'results':[], 'roots':[], 'undo_delete':'', 'prompt':None, 'operations':[], 'log':[],
         'ui_api_version':UI_API_VERSION, 'ui_build':UI_BUILD}
LOCK = threading.RLock()
COVER_ROW_CHANGES = deque(maxlen=64)
PROCESS = None
DELETE_PLAN = None
STOP_GRACE_SECONDS = 15
PREVIEW_TIMEOUT = 45
PREVIEW_LIMIT = 32 * 1024 * 1024
PREVIEW_LOCK = threading.Lock()
PREVIEW_IMAGES = OrderedDict()
PREVIEW_LOADING = {}
PREVIEW_NETWORK = threading.BoundedSemaphore(3)


def remember_preview(key, blob):
    with PREVIEW_LOCK:
        PREVIEW_IMAGES[key] = blob
        PREVIEW_IMAGES.move_to_end(key)
        while len(PREVIEW_IMAGES) > 16 or sum(map(len, PREVIEW_IMAGES.values())) > PREVIEW_LIMIT:
            PREVIEW_IMAGES.popitem(last=False)


def recover_preview(cache_key, candidate):
    """Share an in-flight request for the same original and source."""
    source = (*cache_key, candidate.get('image_url'))
    with PREVIEW_LOCK:
        if cache_key in PREVIEW_IMAGES:
            PREVIEW_IMAGES.move_to_end(cache_key)
            return PREVIEW_IMAGES[cache_key]
        pending = PREVIEW_LOADING.get(source)
        owner = pending is None
        if owner:
            pending = {'done': threading.Event()}
            PREVIEW_LOADING[source] = pending
    if not owner:
        if not pending['done'].wait(PREVIEW_TIMEOUT + 8):
            raise ValueError('Истекло время загрузки оригинала обложки.')
        if 'error' in pending:
            raise ValueError(pending['error'])
        return pending['blob']

    try:
        if not PREVIEW_NETWORK.acquire(timeout=6):
            raise ValueError('Сейчас загружаются другие оригиналы. Повторите попытку.')
        try:
            request = {k: candidate.get(k) for k in ('id', 'image_url', 'upscale')}
            restored = subprocess.run(
                [PYTHON, '-X', 'utf8', '-m', 'core.cover_image_restore'],
                input=json.dumps(request).encode('utf-8'), cwd=ROOT, capture_output=True,
                timeout=PREVIEW_TIMEOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        finally:
            PREVIEW_NETWORK.release()
        if restored.returncode:
            message = restored.stderr.decode('utf-8', errors='replace').strip()
            raise ValueError(message or 'Не удалось загрузить оригинал обложки.')
        blob = restored.stdout
        if hashlib.sha256(blob).hexdigest() != candidate['id']:
            raise ValueError('Изображение источника изменилось. Обновите варианты.')
        remember_preview(cache_key, blob)
        pending['blob'] = blob
        return blob
    except subprocess.TimeoutExpired:
        pending['error'] = 'Истекло время загрузки оригинала обложки.'
        raise ValueError(pending['error']) from None
    except Exception as error:
        pending['error'] = str(error)
        raise
    finally:
        with PREVIEW_LOCK:
            PREVIEW_LOADING.pop(source, None)
            pending['done'].set()


def candidate_preview(track_path, state, candidate):
    from core.covers import pictures, queued_image
    from core.storage import touch_artwork
    from mutagen import File

    key = candidate['id']
    cache_key = (ROOT, key)
    with PREVIEW_LOCK:
        blob = PREVIEW_IMAGES.get(cache_key)
        if blob is not None:
            PREVIEW_IMAGES.move_to_end(cache_key)
            return blob
    path = ROOT/'cache/covers'/(key+'.image')
    try:
        blob = path.read_bytes()
        if hashlib.sha256(blob).hexdigest() == key:
            touch_artwork(path)
            remember_preview(cache_key, blob)
            return blob
    except OSError:
        pass
    if state.get('queued'):
        try:
            blob = queued_image(ROOT, track_path)
            if blob and hashlib.sha256(blob).hexdigest() == key:
                remember_preview(cache_key, blob)
                return blob
        except sqlite3.Error:
            pass
    if state.get('status') == 'ready' and state.get('current_id') in (None, key):
        try:
            embedded = pictures(File(track_path))
            if embedded and hashlib.sha256(embedded[0]).hexdigest() == key:
                remember_preview(cache_key, embedded[0])
                return embedded[0]
        except Exception:
            # The independently verified catalog original is still usable.
            pass
    return recover_preview(cache_key, candidate)

def send(data):
    if PROCESS is None or PROCESS.poll() is not None: raise ValueError('Процесс проверки недоступен')
    PROCESS.stdin.write(json.dumps(data, ensure_ascii=True)+'\n'); PROCESS.stdin.flush()

def observe(process):
    failure = None
    streamed=[]
    from ui.protocol import decode_event
    try:
        for line in process.stdout:
            if not line.strip(): continue
            e = decode_event(line)
            with LOCK:
                if PROCESS is not process: return
                kind=e['kind']
                STATE['last_event_at']=time.time()
                if kind=='started':
                    if e.get('ui_api_version') != UI_API_VERSION:
                        process.terminate()
                        raise ValueError('Несовместимый ui/worker.py. Обновите все файлы последнего патча и перезапустите приложение.')
                    STATE['message']='Ядро запущено. Подготовка списка файлов…'
                elif kind=='progress':
                    if e['stage'] in ('cover_search','cover_write') and 'done' in e.get('details',{}):STATE.update(done=e['details']['done'],total=e['details']['total'])
                    if e['stage'] in ('audit_scanned','library_loaded'):STATE.update(done=e.get('details',{}).get('done',0),total=e.get('details',{}).get('total',0))
                    if e['stage']=='analysis_done' and STATE['phase']=='scanning':STATE['phase']='finishing'
                    if e['stage'] in ('plan_done','checkpoint_restored'): STATE['done']+=1
                    names={'cache_maintenance':'Обслуживание временных данных','cover_search':'Проверяем обложки','cover_write':'Вшиваем обложки','release_search':'Поиск изданий','library_loaded':'Открываем локальные теги','audit_scanned':'Диагностика оформления','scanned':'Чтение тегов', 'fingerprint_done':'Аудиоотпечаток',
                           'recognition_done':'Распознавание', 'plan_done':'Решение готово',
                           'checkpoint_restored':'Восстановлено', 'prepare_started':'Подготовка',
                           'run_configuration':'Сервисы настроены', 'analysis_done':'Все треки проверены. Формируем итоговый отчёт…',
                           'written_marks_restored':'Восстановлены отметки записанных треков', 'summary_tables_done':'Итоговый отчёт подготовлен', 'export_started':'Сохраняем план изменений…',
                           'write_mode_waiting':'Готовим предпросмотр изменений…', 'loading_results':'Передаём результаты в интерфейс…'}
                    if STATE['phase']!='stopping':
                        STATE['message']=names.get(e['stage'], e['stage']) + ': ' + e.get('file','')
                        if e['stage']=='shazam_transport': STATE['message']='Shazam: ожидание сервиса. Прогресс сохраняется.'
                elif kind=='results_chunk':
                    if e['offset']==0:streamed=[]
                    if e['offset']!=len(streamed):raise ValueError('Нарушен порядок частей списка файлов.')
                    streamed.extend(e['items'])
                    if STATE['phase']!='stopping':
                        STATE['results']=streamed+STATE['results'][len(streamed):e['total']]
                        STATE['results_revision']=STATE.get('results_revision',0)+1
                        STATE.update(done=len(streamed),total=e['total'],message=f"Открываем список: {len(streamed)} / {e['total']}")
                elif kind in ('results','results_complete'):
                    if kind=='results_complete':
                        if e['total']!=len(streamed):raise ValueError('Получен неполный список файлов.')
                        e['items']=streamed
                        streamed=[]
                    if STATE['phase']!='stopping':
                        STATE.update(results=e['items'], phase='ready', message=STATE.pop('ready_message','Проверка завершена'))
                        STATE['results_revision']=STATE.get('results_revision',0)+1
                        complete_local_operation(STATE['message'])
                elif kind=='collection_synced':
                    if e.get('paths') != [d['path'] for d in STATE['results']]:
                        raise ValueError('Рабочий список не совпал с интерфейсом. Нажмите «Обновить папку».')
                    complete_local_operation(e.get('message','Список обновлён'))
                elif kind=='audit_report':
                    for update in e['updates']:
                        if 0<=update['index']<len(STATE['results']):STATE['results'][update['index']]['_ui_audit']=update['audit']
                    STATE['audit_report']={k:e[k] for k in ('summary','groups','rules')}
                    STATE['audit_summary']=e['summary'];STATE['audit_revision']=STATE.get('audit_revision',0)+1
                    STATE['results_revision']=STATE.get('results_revision',0)+1
                    if e.get('open_view'):
                        STATE['audit_view_revision']=STATE.get('audit_view_revision',0)+1
                        STATE.update(phase='ready',done=e['summary']['checked'],total=e['summary']['checked'],message='Диагностика: отклонений '+str(e['summary']['deviations'])+', файлов проверено '+str(e['summary']['checked'])+'.',local_operation=None,audit_cancelling=False)
                elif kind=='audit_cancelled':STATE.update(phase='ready' if STATE['results'] else 'idle',message='Диагностика отменена. Прежние результаты сохранены.',local_operation=None,audit_cancelling=False)
                elif kind=='releases_found':
                    STATE['results'][e['index']]=e['item']
                    STATE['results_revision']=STATE.get('results_revision',0)+1
                    STATE.update(phase='ready',action_error=None,message='Поиск изданий завершён.',release_search_warnings=e.get('warnings',[]))
                elif kind in ('cover_updated', 'cover_updates', 'cover_queue_updates'):
                    updates = [e] if kind == 'cover_updated' else e['updates']
                    for update in updates:
                        index = update['index']
                        if kind == 'cover_queue_updates':
                            STATE['results'][index]['_ui_cover'] = update['cover']
                        else:
                            STATE['results'][index] = update['item']
                    revision = STATE.get('results_revision', 0) + 1
                    STATE['results_revision'] = revision
                    COVER_ROW_CHANGES.append((revision, tuple(update['index'] for update in updates)))
                elif kind=='cover_done':
                    STATE.update(phase='ready',message=e['message'],action_error=None,cover_running=False)
                    STATE['cover_operation_revision']=STATE.get('cover_operation_revision',0)+1
                    if e.get('operation')=='cover_upscale':
                        STATE['cover_upscale_revision']=STATE.get('cover_upscale_revision',0)+1
                        STATE['cover_upscaled_choices']=e.get('upscaled_choices',{})
                    if e.get('operation')=='cover_import':
                        STATE['cover_import_revision']=STATE.get('cover_import_revision',0)+1
                        STATE['cover_imported_choices']=e.get('imported_choices',{})
                    if e.get('operation') in ('cover_search','cover_diagnose'):STATE['cover_summary']=e.get('summary')
                    if e.get('operation') == 'cover_commit':
                        STATE['cover_commit_summary'] = {**e.get('summary', {}), 'written': len(e.get('written_paths', []))}
                    if e.get('operation') in ('cover_write', 'cover_commit') or e.get('written_paths'):
                        STATE['cover_write_revision']=STATE.get('cover_write_revision',0)+1
                        STATE['cover_written_paths']=e.get('written_paths',[])
                    if (e.get('summary') or {}).get('errors'):
                        STATE.update(action_error=e['message'],error_revision=STATE.get('error_revision',0)+1)
                    if e.get('operation') in ('cover_mark', 'cover_queue', 'cover_unqueue') or e.get('marked_paths'):
                        STATE['cover_mark_revision']=STATE.get('cover_mark_revision',0)+1
                        STATE['cover_marked_paths']=e.get('marked_paths',[])
                elif kind=='edited':
                    STATE['results'][e['index']]=e['item']
                    STATE['results_revision']=STATE.get('results_revision',0)+1
                    STATE.update(phase='ready',action_error=None,message=e.get('message','Черновик обновлён; аудиофайл пока не изменён.'))
                elif kind=='action_error':
                    STATE['cover_running']=False
                    if STATE.get('local_operation'):
                        STATE.update(phase='error',local_operation=None,refresh_requested=False,message=e['message'],action_error=e['message'],error_revision=STATE.get('error_revision',0)+1)
                    else:STATE.update(phase='ready',message=e['message'],action_error=e['message'],error_revision=STATE.get('error_revision',0)+1)
                elif kind=='warning':STATE['warning']=e['message']
                elif kind=='prompt': STATE.update(prompt=e, phase='prompt')
                elif kind=='preview': STATE.update(preview_revision=STATE.get('preview_revision',0)+1,preview_selected_count=e.get('selected_count'),operations=e['operations'], operation_kind=e.get('operation_kind','tags'), skipped=e.get('skipped',[]), prompt=None, phase='preview')
                elif kind=='written':
                    STATE['path_changes']={}
                    for update in e.get('updates',[]):STATE['results'][update['index']]=update['item']
                    for result in e.get('results',[]):
                        if result.get('renamed'):
                            STATE['path_changes'][result['old_path']]=result['new_path']
                            STATE['files']=[result['new_path'] if p==result['old_path'] else p for p in STATE['files']]
                    STATE['results_revision']=STATE.get('results_revision',0)+1
                    STATE['outcome_revision']=STATE.get('outcome_revision',0)+1
                    STATE.update(phase='ready',message=(('Оформление: записано '+str(sum(bool(r.get('success')) for r in e.get('results',[])))+', ошибок '+str(sum(not r.get('success') for r in e.get('results',[])))+'. Диагностика обновлена.') if e.get('operation_kind')=='uniformity' else 'Отмечено исправленными: '+str(sum(bool(r.get('success')) for r in e.get('results',[])))+'.' if e.get('operation_kind')=='complete' else 'Переименование завершено.' if e.get('operation_kind')=='rename' else 'Запись завершена. Можно продолжать редактировать треки.'),
                                 outcome={**{k:v for k,v in e.items() if k!='updates'},**({'selected_count':STATE.get('preview_selected_count'),'skipped':STATE.get('skipped',[])} if e.get('operation_kind') in ('tags','uniformity') else {})},prompt=None,operations=[],action_error=None)
                    if STATE['path_changes']:
                        try:persist_selection()
                        except Exception as error:STATE['warning']='Новые имена сохранены; не удалось обновить набор: '+str(error)
                elif kind=='ready': STATE.update(phase='ready', prompt=None, operations=[])
                elif kind=='export': STATE['export']=e['path']
                elif kind in ('error','stopped'): STATE.update(phase=kind, message=e.get('message',''), prompt=None,local_operation=None,refresh_requested=False)
                # Keep compact events; raw evidence lives in the TXT/JSON report.
                if kind not in ('results','results_chunk','results_complete','edited','written','cover_updated','cover_updates','cover_queue_updates','progress','collection_synced','audit_report') or e.get('stage') in ('shazam_transport','fingerprint_failed'):
                    event={k:v for k,v in e.items() if k not in ('written_paths','marked_paths')} if kind=='cover_done' else e
                    STATE['log']=(STATE['log']+[event])[-40:]
    except Exception as error:
        failure='Ошибка связи с процессом: '+str(error)
    finally:
        with LOCK:
            if PROCESS is process:
                if STATE['phase']=='stopping':
                    STATE.update(phase='stopped',message='Поиск остановлен. Сохранённые этапы доступны для продолжения.',prompt=None)
                elif failure or STATE['phase'] in ('scanning','finishing','review','writing','prompt','auditing'):
                    STATE['cover_running']=False
                    STATE.update(phase='error',message=failure or 'Рабочий процесс завершился. Откройте последний отчёт и ui_runtime/worker_errors.txt.',prompt=None,local_operation=None,refresh_requested=False,worker_synced=False)
                if failure:
                    STATE['log']=(STATE['log']+[{'kind':'error','message':failure}])[-40:]


def stop_search():
    with LOCK:
        if STATE['phase']=='auditing':
            (ROOT/'ui_runtime').mkdir(exist_ok=True);(ROOT/'ui_runtime/audit_stop').touch()
            STATE.update(message='Отменяем диагностику…',audit_cancelling=True)
            return
        if STATE['phase']=='stopping': return
        if STATE['phase'] not in ('scanning','finishing'): raise ValueError('Остановить можно только поиск')
        process=PROCESS
        STATE.update(phase='stopping',message='Останавливаем поиск; сохранённые этапы останутся доступными…')
        (ROOT/'ui_runtime').mkdir(exist_ok=True)
        (ROOT/'ui_runtime/stop').touch()
    def finish():
        if process is not None:
            try:process.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                # Analysis is DRY RUN. Never terminate a write or review process.
                with LOCK:
                    eligible=PROCESS is process and STATE['phase'] in ('stopping','stopped')
                if eligible:
                    try:
                        if os.name=='nt':
                            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],
                                capture_output=True,timeout=10,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        else:process.terminate()
                        process.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):pass
        with LOCK:
            if PROCESS is process and STATE['phase'] in ('stopping','stopped'):
                if process is not None and process.poll() is None:
                    STATE.update(phase='error',message='Процесс не завершился. Подробности: ui_runtime/worker_errors.txt.')
                else:
                    STATE.update(phase='stopped',message='Поиск остановлен. Продолжите сохранённую проверку при следующем запуске.')
    threading.Thread(target=finish,daemon=True).start()


def persist_selection():
    from core.cache_utils import atomic_write_json
    files=list(STATE['files'])
    atomic_write_json(ROOT/'ui_runtime/selection.json',files)
    atomic_write_json(ROOT/'ui_runtime/collection.json',{'schema':1,'roots':STATE.get('roots',[]),'files':files})
    atomic_write_json(ROOT/'config/last_selection.json',{'files':files})


def detach_worker():
    global PROCESS
    with LOCK:
        old=PROCESS;PROCESS=None
    if old is not None and old.poll() is None:
        old.stdin.close()
        old.wait(timeout=10)


def complete_local_operation(message):
    with LOCK:
        queued=STATE.pop('refresh_requested',False)
        STATE.update(phase='ready',message=message,done=0,total=0,local_operation=None,worker_synced=True)
    if queued:background(lambda:local_action('refresh',{}))


def sync_collection(files,results,previous,message):
    """Keep the idle worker and send only removed paths / restored records."""
    with LOCK:
        process=PROCESS
        previous_files=set(STATE['files'])
        live=process is not None and process.poll() is None and STATE.get('worker_synced') is not False
    if not live:
        launch_worker(files,[],'browse',message)
        return
    old={d['path'] for d in previous};new={d['path'] for d in results}
    additions=[d for d in results if d['path'] not in old]
    command={'action':'collection_update','remove_paths':list(old-new),'message':message,
             'selection_remove':list(previous_files-set(files)),
             'selection_add':[p for p in files if p not in previous_files],
             'flags':[{'path':d['path'],'warning':d.get('_ui_draft_warning','Файл изменён; перепроверьте трек.')}
                      for d in results if d.get('_ui_write_recheck_required')]}
    if additions:
        from core.cache_utils import atomic_write_json
        seed=ROOT/'ui_runtime'/('append_'+uuid.uuid4().hex+'.json')
        atomic_write_json(seed,additions);command['append_path']=str(seed)
    # Publish the filesystem outcome immediately. Actions remain blocked until
    # the worker acknowledges matching paths/order, so indices cannot drift.
    with LOCK:
        STATE.update(files=files,results=results,results_revision=STATE.get('results_revision',0)+1,
                     path_changes={},phase='review',worker_synced=False,done=0,total=0,message='Синхронизируем список…')
    persist_selection()
    sync_id=uuid.uuid4().hex
    with LOCK:STATE['local_sync_id']=sync_id
    send(command)
    def check_ack():
        with LOCK:
            if STATE.get('local_sync_id')==sync_id and STATE.get('worker_synced') is False and STATE.get('local_operation'):
                message='Рабочий процесс не подтвердил обновление списка. Нажмите «Обновить папку» для восстановления связи.'
                STATE.update(phase='error',local_operation=None,refresh_requested=False,message=message,action_error=message,
                             error_revision=STATE.get('error_revision',0)+1)
    timer=threading.Timer(30,check_ack);timer.daemon=True;timer.start()


def launch_worker(files, retained, mode='resume', message='Проверка завершена',audit=False,audit_rules=None):
    global PROCESS
    detach_worker()
    from core.cache_utils import atomic_write_json
    from core.preferences import load_preferences
    runtime=ROOT/'ui_runtime';runtime.mkdir(exist_ok=True)
    (runtime/'stop').unlink(missing_ok=True)
    if audit:(runtime/'audit_stop').unlink(missing_ok=True)
    else:
        for item in retained:item.pop('_ui_audit',None)
        with LOCK:STATE.update(audit_summary=None,audit_report=None)
    seed=runtime/('retained_'+uuid.uuid4().hex+'.json')
    atomic_write_json(seed,retained)
    visible=retained if mode!='browse' else [
        {'path':str(p),'file':Path(p).name,'title':Path(p).stem,'_ui_loading':True,
         'change_plan':{'identity':'UNSCANNED'}} for p in files]
    with LOCK:
        STATE.update(files=list(files),results=visible,results_revision=STATE.get('results_revision',0)+1,
                     path_changes={},phase='review' if mode=='browse' else ('auditing' if audit else ('scanning' if len(files)>len(retained) else 'review')),
                     done=0,total=len(files) if audit else max(0,len(files)-len(retained)),prompt=None,operations=[],action_error=None,
                     message='Открываем локальные теги…' if mode=='browse' else ('Диагностика оформления…' if audit else ('Проверяем новые файлы…' if len(files)>len(retained) else 'Обновляем список…')),ready_message=message)
    persist_selection()
    if not files:
        seed.unlink(missing_ok=True)
        complete_local_operation(message)
        if audit:
            from ui.uniformity import UniformitySession
            from core.uniformity import audit_records
            rules=audit_rules if audit_rules is not None else UniformitySession(ROOT).rules
            report=audit_records([],rules)
            with LOCK:STATE.update(audit_summary=report['summary'],audit_report=report,audit_revision=STATE.get('audit_revision',0)+1,audit_view_revision=STATE.get('audit_view_revision',0)+1)
        return
    err=(runtime/'worker_errors.txt').open('a',encoding='utf-8')
    try:
        process=subprocess.Popen([PYTHON,'-X','utf8','-u',str(ROOT/'ui/worker.py')],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=err,text=True,encoding='utf-8',errors='strict',bufsize=1,
                env={**os.environ,'PYTHONIOENCODING':'utf-8:strict','PYTHONUTF8':'1'},
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    finally:err.close()
    with LOCK:PROCESS=process
    threading.Thread(target=observe,args=(process,),daemon=True).start()
    send({'files':files,'retained_path':str(seed),'mode':mode,'preferences':load_preferences(),'audit_only':audit,'audit_rules':audit_rules,'maintain_cache':True})


def background(function):
    def run():
        try:function()
        except Exception as error:
            with LOCK:
                STATE.update(phase='error' if STATE.get('worker_synced') is False else ('ready' if STATE['results'] else 'idle'),local_operation=None,refresh_requested=False,message=str(error),action_error=str(error),
                             error_revision=STATE.get('error_revision',0)+1)
    threading.Thread(target=run,daemon=True).start()


def restore_library(automatic=False):
    """Restore the folder without starting catalog/audio recognition."""
    def restore():
        selection=ROOT/'ui_runtime/selection.json'
        collection=ROOT/'ui_runtime/collection.json'
        if collection.exists():
            saved=json.loads(collection.read_text(encoding='utf-8'))
            roots=saved.get('roots',[]);files=saved.get('files',[])
        else:
            roots=[]
            if selection.exists():files=json.loads(selection.read_text(encoding='utf-8'))
            else:
                from core.selection_history import load_last_selection
                files,_=load_last_selection()
        if roots:
            from core.library_sync import discover_audio
            files=discover_audio(roots)
        else:files=[str(p) for p in files if Path(p).is_file()]
        if not files and not roots:
            with LOCK:STATE.update(phase='idle',message='Выберите папку с музыкой',local_operation=None)
            if not automatic:raise ValueError('Прошлый набор пуст или файлы перемещены')
            return
        with LOCK:STATE['roots']=roots
        launch_worker(files,[],'browse','Папка открыта. Распознавание запускается кнопкой «Начать сканирование».')
    with LOCK:
        STATE.update(phase='review',local_operation='restore',message='Открываем последнюю папку…',restore_requested=True)
    background(restore)


def validate_revision(data):
    if 'revision' in data and data['revision']!=STATE.get('results_revision',0):
        raise ValueError('Список обновился. Проверьте выбор и повторите действие.')
    indices=data.get('indices',[])
    if not isinstance(indices,list) or not indices or any(type(i)!=int or not 0<=i<len(STATE['results']) for i in indices):
        raise ValueError('Выберите треки')
    if 'paths' in data and data['paths']!=[STATE['results'][i]['path'] for i in indices]:
        raise ValueError('Пути выбранных файлов изменились. Повторите выбор.')
    return list(dict.fromkeys(indices))


def pick(kind):
    code="from tkinter import Tk,filedialog; import json; r=Tk(); r.withdraw(); r.attributes('-topmost',True); print(json.dumps(" + ("filedialog.askdirectory()" if kind=='folder' else "filedialog.askopenfilenames(filetypes=[('Аудио','*.mp3 *.flac *.m4a *.ogg *.opus *.wav *.aiff *.aif *.mp4')])") + ")); r.destroy()"
    p=subprocess.run([PYTHON,'-X','utf8','-c',code],capture_output=True,text=True,encoding='utf-8',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if p.returncode:raise ValueError('Не удалось открыть выбор файлов: '+p.stderr[-500:])
    selected=json.loads(p.stdout)
    if not selected:return None
    from core.library_sync import discover_audio
    roots=[str(Path(selected).resolve())] if kind=='folder' else []
    files=discover_audio(roots) if roots else [str(Path(f).resolve()) for f in selected]
    return {'files':files,'roots':roots}


def deletion_plan(data):
    """Review exact files and sizes before enabling an irreversible action."""
    global DELETE_PLAN
    from core.library_sync import TrashStore
    from core.permanent_delete import selected_plan
    with LOCK:
        DELETE_PLAN = None
        mode = data.get('mode')
        if mode not in ('selected','trash'):
            raise ValueError('Некорректный режим удаления.')
        if STATE['phase'] not in ('idle','ready','stopped','error'):
            raise ValueError('Дождитесь окончания текущего действия.')
        warnings = []
        revision = STATE.get('results_revision',0)
        phase = STATE['phase']
        if mode == 'selected':
            if STATE['phase'] != 'ready':
                raise ValueError('Сначала дождитесь загрузки списка файлов.')
            if data.get('revision') != STATE.get('results_revision',0):
                raise ValueError('Список обновился. Повторите выбор.')
            indices = validate_revision(data)
            chosen = [STATE['results'][i] for i in indices]
    # Large trash journals must not block state polling or worker messages.
    if mode == 'selected':
        rows = selected_plan(chosen)
    else:
        rows, warnings = TrashStore(ROOT).purge_plan()
    with LOCK:
        if STATE['phase'] != phase or STATE.get('results_revision',0) != revision:
            raise ValueError('Список или текущее действие изменились. Откройте удаление заново.')
        DELETE_PLAN = {'id':secrets.token_urlsafe(32), 'mode':mode, 'rows':rows,
                       'revision':revision, 'created':time.monotonic()}
        return {'plan_id':DELETE_PLAN['id'], 'mode':mode, 'count':len(rows),
                'bytes':sum(r['bytes'] for r in rows), 'warnings':warnings,
                'files':[{key:r[key] for key in ('path','file','bytes')} for r in rows]}


def local_action(action,data):
    global DELETE_PLAN
    from core.library_sync import reconcile,TrashStore
    with LOCK:
        if action=='refresh' and STATE.get('local_operation') in ('delete','delete_forever','empty_trash','undo_delete','refresh'):
            STATE['refresh_requested']=True
            return
        if STATE['phase'] not in ('idle','ready','stopped','error'):
            raise ValueError('Сначала завершите текущее действие или закройте предпросмотр.')
        if action=='audit' and data.get('rules') is not None:
            from core.uniformity import validate_rules
            validate_rules(data['rules'])
        previous_phase=STATE['phase']
        files=list(STATE['files']);results=list(STATE['results']);roots=list(STATE.get('roots',[]))
        if action=='delete':
            if STATE['phase']!='ready':raise ValueError('Удаление доступно после окончания проверки.')
            indices=validate_revision(data)
            if data.get('confirmed') is not True:raise ValueError('Подтвердите перенос файлов в корзину TagCleaner.')
            chosen=[results[i] for i in indices]
        if action in ('delete_forever','empty_trash'):
            mode = 'trash' if action == 'empty_trash' else 'selected'
            plan = DELETE_PLAN
            if (not plan or not isinstance(data.get('plan_id'),str)
                    or not secrets.compare_digest(data['plan_id'],plan['id']) or plan['mode'] != mode
                    or time.monotonic()-plan['created'] > 600):
                raise ValueError('Откройте список удаления заново: подтверждение устарело.')
            if data.get('confirmed') is not True:
                raise ValueError('Подтвердите безвозвратное удаление показанных файлов.')
            if mode == 'selected' and (STATE['phase'] != 'ready' or plan['revision'] != STATE.get('results_revision',0)):
                raise ValueError('Список обновился. Проверьте файлы заново.')
            if not plan['rows']:
                raise ValueError('Нет файлов для удаления.')
            chosen = plan['rows']
            DELETE_PLAN = None  # One confirmation can start at most one batch.
        if action in ('refresh','audit') and not files and not roots:raise ValueError('Сначала выберите музыкальную папку.')
        STATE.update(phase='review',local_operation=action,done=0,total=0,message={'audit':'Подготовка диагностики…','refresh':'Проверяем содержимое папки…','delete':'Переносим файлы в корзину TagCleaner…',
                     'delete_forever':'Удаляем выбранные файлы безвозвратно…','empty_trash':'Очищаем корзину TagCleaner…',
                     'undo_delete':'Восстанавливаем файлы…'}.get(action,'Открываем выбор…'),action_error=None)
    def run():
        if action in ('folder','files'):
            picked=pick(action)
            if picked is None:
                with LOCK:STATE.update(phase=previous_phase,local_operation=None,message='Выбор отменён')
                return
            detach_worker()
            with LOCK:STATE.update(roots=picked['roots'])
            launch_worker(picked['files'],[],'browse','Папка открыта. Распознавание запускается кнопкой «Начать сканирование».')
            return
        if action=='audit':
            from core.library_sync import discover_audio
            actual=discover_audio(roots) if roots else files
            with LOCK:
                live=PROCESS is not None and PROCESS.poll() is None and STATE.get('worker_synced') is not False
            if live:
                (ROOT/'ui_runtime/audit_stop').unlink(missing_ok=True)
                with LOCK:STATE.update(files=actual,phase='auditing',message='Диагностика оформления…',done=0,total=len(actual),audit_cancelling=False)
                persist_selection();send({'action':'audit','files':actual,'rules':data.get('rules')})
            else:launch_worker(actual,results,'resume','Диагностика завершена',audit=True,audit_rules=data.get('rules'))
            return
        if action=='refresh':
            def checking(stage,done,total,file):
                with LOCK:STATE.update(message=stage+(': '+file if file else ''),done=done,total=total)
            update=reconcile(files,results,roots,progress=checking)
            summary=f"Обновлено: новых {len(update['added'])}, удалённых {len(update['removed'])}, изменённых {len(update['changed'])}."
            with LOCK:STATE['refresh_summary']={k:len(update[k]) for k in ('added','removed','changed','pending')}
            if update['pending'] or update['changed']:launch_worker(update['files'],[],'browse',summary+' Распознавание не запускалось.')
            else:sync_collection(update['files'],update['retained'],results,summary)
            return
        previous_results=list(results)
        def progress(stage,done,total,file):
            with LOCK:STATE.update(message=stage+(': '+file if file else ''),done=done,total=total)
        store=TrashStore(ROOT)
        if action in ('delete','delete_forever'):
            if action=='delete':outcome=store.delete(chosen,progress=progress)
            else:
                from core.permanent_delete import execute_plan
                outcome=execute_plan(chosen,progress=progress)
            removed={r['path'] for r in outcome['results'] if r['success']}
            files[:]=[p for p in files if p not in removed]
            results[:]=[r for r in results if r['path'] not in removed]
            kind=action
            summary='Убрано из списка: '+str(len(removed))+('. Файлы можно восстановить из корзины TagCleaner.' if action=='delete' else '. Удаление безвозвратное.')
        elif action=='empty_trash':
            outcome=store.purge(chosen,progress=progress)
            with LOCK:
                refresh_requested=STATE.get('refresh_requested')
                STATE.update(phase=previous_phase,local_operation=None,undo_delete=store.latest() or '',
                    outcome={'operation_kind':action,'results':outcome['results']},
                    outcome_revision=STATE.get('outcome_revision',0)+1,
                    message='Очистка корзины завершена.',refresh_requested=False)
            if refresh_requested and (files or roots):local_action('refresh',{})
            return
        else:
            batch=store.latest()
            if not batch:raise ValueError('В корзине нет файлов для восстановления.')
            restored=store.restore(batch,progress=progress)
            outcome={'results':restored}
            for result in restored:
                if result['success'] and result['path'] not in files:
                    files.append(result['path'])
                    if result.get('item'):results.append(result['item'])
            kind='undo_delete';summary='Восстановлено файлов: '+str(sum(r['success'] for r in restored))+'.'
        with LOCK:
            STATE.update(undo_delete=store.latest() or '',outcome={'operation_kind':kind,'results':[{k:v for k,v in r.items() if k!='item'} for r in outcome['results']]},
                         outcome_revision=STATE.get('outcome_revision',0)+1)
        sync_collection(files,results,previous_results,summary)
    background(run)


def result_row(item):
    from ui.editor import draft_matches_current, has_preview_work

    row = {key: item.get(key) for key in ('file','path','artist','title','remixer','_checkpoint_retry_required')}
    cover = item.get('_ui_cover') or {'status': 'pending'}
    row['cover'] = {key: value for key, value in cover.items() if key in (
        'status','message','verification','manual_decision','confirmed_embedded','queued','write_error',
        'changed','changed_at','change_kind','checked_at','diagnosis_outcome')}
    row['cover']['candidate_count'] = len(cover.get('candidates') or [])
    row['audit'] = item.get('_ui_audit') if STATE.get('audit_summary') is not None else None
    row['manual_draft'] = bool(item.get('_ui_draft'))
    row['draft_matches_file'] = draft_matches_current(item)
    row['draft_values'] = (item.get('_ui_draft') or {}).get('values')
    row['draft_warning'] = item.get('_ui_draft_warning')
    row['written'] = bool(item.get('_ui_written'))
    row['completion_kind'] = item.get('_ui_completion_kind', 'write')
    row['write_recheck_required'] = bool(item.get('_ui_write_recheck_required'))
    row['write_pending'] = has_preview_work(item)
    row['audit_write_pending'] = has_preview_work(item, uniformity=True)
    plan = item.get('change_plan') or {}
    row['change_plan'] = {'identity': plan.get('identity')}
    for field in ('artist', 'title'):
        row['change_plan'][field] = {key: (plan.get(field) or {}).get(key) for key in ('current','proposed','status')}
    return row


def result_snapshot(since):
    revision = STATE.get('results_revision', 0)
    results = STATE['results']
    changes = [indices for number, indices in COVER_ROW_CHANGES if since < number <= revision]
    # A gap means the list changed outside artwork operations, or the
    # revision history expired. Indices are safe only without a gap.
    if 0 <= since <= revision and len(changes) == revision - since:
        indices = sorted({index for group in changes for index in group})
        return {'revision': revision, 'updates': [
            {'index': index, 'row': result_row(results[index])} for index in indices]}
    return {'revision': revision, 'rows': [result_row(item) for item in results]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def respond(self, data, status=200, content='application/json; charset=utf-8'):
        blob=json.dumps(data,ensure_ascii=True,default=str).encode() if content.startswith('application/json') else data
        self.send_response(status);self.send_header('Content-Type',content);self.send_header('Content-Length',str(len(blob)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(blob)
    def valid_host(self):
        return self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port)
    def do_GET(self):
        if not self.valid_host(): self.respond({'error':'Неверный адрес'},403);return
        if self.path in ('/fonts/Inter-400.ttf','/fonts/Inter-500.ttf','/fonts/Inter-600.ttf','/fonts/Inter-700.ttf'):
            self.respond((ROOT/'ui'/self.path.lstrip('/')).read_bytes(),content='font/ttf');return
        if self.path=='/':
            with LOCK:
                if not STATE.get('restore_requested') and not STATE['files'] and STATE['phase']=='idle':
                    restore_library(automatic=True)
            self.respond((ROOT/'ui/index.html').read_bytes().replace(b'__TOKEN__',TOKEN.encode()),content='text/html; charset=utf-8');return
        if self.headers.get('X-Token')!=TOKEN: self.respond({'error':'Нет доступа'},403);return
        if self.path=='/state':
            with LOCK:
                data={k:v for k,v in STATE.items() if k not in ('results','files','audit_report')}
                if not STATE.get('trash_checked'):
                    from core.library_sync import TrashStore
                    STATE['undo_delete']=TrashStore(ROOT).latest() or '';STATE['trash_checked']=True
                data['undo_delete']=STATE['undo_delete']
                data['count']=len(STATE['files']); data['result_count']=len(STATE['results'])
            self.respond(data)
        elif self.path=='/results':
            with LOCK:
                rows=[result_row(item) for item in STATE['results']]
            self.respond(rows)
        elif self.path.startswith('/results?since='):
            value = self.path.removeprefix('/results?since=')
            since = int(value) if re.fullmatch(r'[0-9]{1,18}', value) else -1
            with LOCK:
                snapshot = result_snapshot(since)
            self.respond(snapshot)
        elif self.path.startswith('/cover_detail?'):
            from urllib.parse import parse_qs
            try:
                index=int(parse_qs(urlparse(self.path).query)['index'][0])
                with LOCK:
                    if not 0<=index<len(STATE['results']):raise ValueError('Трек недоступен.')
                    item=dict(STATE['results'][index])
                from core.storage import available_artwork
                from core.cover_query import default_query
                payload = {**{k:item.get(k) for k in ('path','file','artist','title','album')},
                           'suggested_query':default_query(item),'_ui_cover':available_artwork(item.get('_ui_cover'),ROOT)}
                self.respond(payload)
            except (ValueError,KeyError) as error:self.respond({'error':str(error)},400)
            except Exception as error:
                self.respond({'error':'Не удалось открыть подбор обложки: '+str(error)+'. Перезапустите приложение после обновления.'},500)
        elif self.path.startswith('/cover_image?'):
            from urllib.parse import parse_qs
            import base64
            try:
                q=parse_qs(urlparse(self.path).query);index=int(q['index'][0]);key=q['id'][0]
                with LOCK:
                    if not 0<=index<len(STATE['results']):raise ValueError('Список изменился. Повторите выбор.')
                    item=STATE['results'][index]
                    track_path=item['path']
                    if 'path' in q:
                        if q['path'][0] != track_path:raise ValueError('Список изменился. Повторите выбор.')
                    elif int(q['revision'][0]) != STATE.get('results_revision',0):
                        raise ValueError('Список изменился. Повторите выбор.')
                    state=item.get('_ui_cover') or {}
                    candidate=next((c for c in state.get('candidates',[]) if c.get('id')==key),None)
                    current=key=='current' or (candidate is None and key==state.get('current_id'))
                    if not current and (not re.fullmatch(r'[a-f0-9]{64}',key) or candidate is None):raise ValueError('Обложка недоступна.')
                if current:
                    from core.covers import pictures
                    from mutagen import File
                    from core.cache_utils import file_signature
                    if state.get('signature')!=file_signature(track_path):raise ValueError('Файл изменился. Обновите папку.')
                    embedded=pictures(File(track_path))
                    if not embedded:raise ValueError('В файле нет обложки.')
                    blob=embedded[0]
                    digest=hashlib.sha256(blob).hexdigest()
                    if key!='current' and digest!=key:raise ValueError('Встроенная обложка изменилась. Обновите папку.')
                    remember_preview((ROOT, digest), blob)
                else:
                    blob = candidate_preview(track_path, state, candidate)
                mime='image/png' if blob.startswith(b'\x89PNG') else 'image/jpeg'
                info=state if current else candidate
                self.respond({'image':'data:'+mime+';base64,'+base64.b64encode(blob).decode(),'width':info['width'],'height':info['height']})
            except (ValueError,KeyError,OSError) as error:self.respond({'error':str(error)},400)
        elif self.path.startswith('/detail?'):
            from urllib.parse import parse_qs
            try:
                index=int(parse_qs(urlparse(self.path).query)['index'][0])
                with LOCK:
                    if not 0<=index<len(STATE['results']):raise ValueError()
                    item=dict(STATE['results'][index])
                from ui.editor import editor_info
                item['editor']=editor_info(item)
                self.respond(item)
            except (ValueError,KeyError):self.respond({'error':'Трек не найден'},404)
            except Exception as error:
                self.respond({'error':'Не удалось подготовить редактор: '+str(error)+'. Установите полный комплект последнего патча и перезапустите приложение.'},500)
        elif self.path=='/storage':
            from core.storage import summary
            self.respond(summary(ROOT))
        elif self.path=='/audit_report':
            with LOCK:report=STATE.get('audit_report')
            if report is None:
                from ui.uniformity import UniformitySession
                report={'summary':None,'groups':[],'rules':UniformitySession(ROOT).rules}
            self.respond(report)
        elif self.path=='/preferences':
            from core.preferences import load_preferences
            self.respond(load_preferences())
        else:self.respond({'error':'Не найдено'},404)
    def do_POST(self):
        global PROCESS
        if not self.valid_host() or self.headers.get('X-Token')!=TOKEN or self.headers.get('Origin') not in (None, 'http://'+self.headers.get('Host','')):
            self.respond({'error':'Нет доступа'},403);return
        try:
            n=int(self.headers.get('Content-Length',0))
            if not 0<=n<=(23000000 if self.path in ('/cover_import','/cover_queue') else 1000000):raise ValueError('Слишком большой запрос')
            data=json.loads(self.rfile.read(n) or b'{}')
            if self.path=='/storage_cleanup':
                with LOCK:
                    if STATE['phase'] not in ('idle','ready','stopped','error'):raise ValueError('Сначала завершите текущую операцию.')
                    STATE.update(phase='maintenance',message='Уплотняем базы и очищаем устаревшие временные данные…')
                def clean_storage():
                    from core.storage import maintain,summary
                    result=maintain(ROOT,force=True)
                    snapshot=summary(ROOT)
                    with LOCK:STATE.update(phase='ready' if STATE['results'] else 'idle',storage_result=result,storage_summary=snapshot,storage_revision=STATE.get('storage_revision',0)+1,message='Обслуживание кэша завершено.' if not result.get('errors') else 'Обслуживание завершено с замечаниями: '+'; '.join(result['errors']))
                background(clean_storage)
                self.respond({'ok':True});return
            if self.path == '/delete_plan':
                self.respond(deletion_plan(data));return
            if self.path in ('/diagnostic_proposal','/editor_diagnostics','/editor_release'):
                import copy
                from ui.editor import diagnostic_proposal,editor_diagnostics,release_options
                with LOCK:
                    if STATE['phase']!='ready':raise ValueError('Дождитесь окончания текущей операции.')
                    index=data.get('index')
                    if type(index)!=int or not 0<=index<len(STATE['results']):raise ValueError('Трек не найден.')
                    if data.get('revision')!=STATE.get('results_revision',0):raise ValueError('Список изменился; откройте редактор заново.')
                    if data.get('path')!=STATE['results'][index]['path']:raise ValueError('Трек перемещён; откройте редактор заново.')
                    item=copy.deepcopy(STATE['results'][index])
                    audit_rules=copy.deepcopy((STATE.get('audit_report') or {}).get('rules'))
                if self.path in ('/editor_diagnostics','/editor_release'):
                    context=None;release_index=data.get('release_index')
                    if release_index is not None:
                        options=release_options(item)
                        if type(release_index)!=int or not 0<=release_index<len(options):raise ValueError('Издание недоступно; откройте редактор заново.')
                        context=options[release_index]
                    self.respond(editor_diagnostics(item,data.get('values'),context,audit_rules,data.get('field'),data.get('use_local_identity',False),apply_release=self.path=='/editor_release',use_filename_identity=data.get('use_filename_identity',False)));return
                self.respond(diagnostic_proposal(item,data.get('field'),data.get('values')));return
            if self.path == '/albumartist_style':
                import copy
                from core.album_artist import proposal,album_key
                from core.release_titles import same_album
                from ui.editor import release_options
                for key in ('artist','title','album','albumartist'):
                    value=data.get(key)
                    if not isinstance(value,str) or len(value)>4096 or any(ord(c)<32 for c in value):raise ValueError('Некорректное поле редактора.')
                with LOCK:
                    if STATE['phase']!='ready':raise ValueError('Дождитесь окончания текущей операции.')
                    index=data.get('index')
                    if type(index)!=int or not 0<=index<len(STATE['results']):raise ValueError('Трек не найден.')
                    if data.get('revision')!=STATE.get('results_revision',0):raise ValueError('Список изменился; откройте редактор заново.')
                    if data.get('path')!=STATE['results'][index]['path']:raise ValueError('Трек перемещён; откройте редактор заново.')
                    item=copy.deepcopy(STATE['results'][index])
                options=release_options(item)
                item.update(artist=data['artist'],title=data['title'],album=data['album'])
                context=None;release_index=data.get('release_index')
                if release_index is not None:
                    if type(release_index)!=int or not 0<=release_index<len(options):raise ValueError('Издание недоступно; откройте редактор заново.')
                    context=options[release_index]
                    if not same_album(context,data['album']):raise ValueError('Название альбома изменено. Выберите подходящее издание заново.')
                    from ui.editor import release_choice_reason
                    reason=release_choice_reason(item,context,data)
                    if reason:raise ValueError(reason)
                self.respond(proposal(item,data['albumartist'],context=context));return
            if self.path == '/credit_style':
                for key in ('artist','title'):
                    value=data.get(key)
                    if not isinstance(value,str) or len(value)>4096 or any(ord(c)<32 for c in value):raise ValueError('Некорректные исполнители или название.')
                from core.credit_style import credit_values,editor_credit_values
                if type(data.get('format_title',False)) is not bool:raise ValueError('Некорректный режим оформления.')
                filename=data.get('filename','')
                if not isinstance(filename,str) or len(filename)>4096 or any(ord(c)<32 for c in filename):raise ValueError('Некорректное имя файла.')
                if 'index' in data:
                    import copy
                    from core.editor_identity import format_identity
                    remixer=data.get('remixer','')
                    if not isinstance(remixer,str) or len(remixer)>4096 or any(ord(c)<32 for c in remixer):raise ValueError('Некорректный автор версии.')
                    featured=data.get('featured_choice','')
                    if not isinstance(featured,str) or len(featured)>4096 or any(ord(c)<32 for c in featured):raise ValueError('Некорректный участник feat.')
                    with LOCK:
                        if STATE['phase']!='ready':raise ValueError('Дождитесь окончания текущей операции.')
                        index=data['index']
                        if type(index)!=int or not 0<=index<len(STATE['results']):raise ValueError('Трек не найден.')
                        if data.get('revision')!=STATE.get('results_revision',0):raise ValueError('Список изменился; откройте редактор заново.')
                        if data.get('path')!=STATE['results'][index]['path']:raise ValueError('Трек перемещён; откройте редактор заново.')
                        item=copy.deepcopy(STATE['results'][index])
                    from ui.editor import release_options
                    from core.release_titles import same_album
                    options=release_options(item)
                    album=data.get('album',item.get('album') or '')
                    if not isinstance(album,str) or len(album)>4096 or any(ord(c)<32 for c in album):raise ValueError('Некорректный альбом.')
                    item['album']=album
                    release_index=data.get('release_index')
                    if release_index is not None:
                        if type(release_index)!=int or not 0<=release_index<len(options):raise ValueError('Издание недоступно; откройте редактор заново.')
                        context=options[release_index]
                        if not same_album(context,album):raise ValueError('Название альбома изменено. Выберите подходящее издание заново.')
                        from ui.editor import release_choice_reason
                        reason=release_choice_reason(item,context,data)
                        if reason:raise ValueError(reason)
                        item['_ui_editor_release']=context
                    self.respond(format_identity(item,data['artist'],data['title'],remixer,item.get('file') or item['path'],featured));return
                values=editor_credit_values(data['artist'],data['title'],filename) if data.get('format_title') else credit_values(data['artist'],data['title'])
                self.respond({'values':values});return
            if self.path == '/version_authors':
                title = data.get('title')
                if not isinstance(title,str) or len(title)>4096 or any(ord(c)<32 for c in title):
                    raise ValueError('Некорректное название трека')
                from core.version_authors import version_author_options
                self.respond({'options':version_author_options(title)})
                return
            if self.path[1:] in ('folder','files','refresh','delete','delete_forever','empty_trash','undo_delete','audit'):
                local_action(self.path[1:],data)
                self.respond({'ok':True});return
            with LOCK:
                action=self.path[1:];phase=STATE['phase']
                if action in ('folder','files','start','preferences','service') and phase in ('scanning','finishing','stopping','writing','review','prompt','preview','auditing','maintenance'):
                    raise ValueError('Дождитесь окончания текущего действия')
                if action=='preferences':
                    from core.preferences import load_preferences,save_preferences
                    prefs=load_preferences()
                    for key in ('release_preference','cover_preference'):
                        if data.get(key) not in ('ask','prefer_single','prefer_album','keep_current'):raise ValueError('Неверная настройка')
                    if data.get('artist_name_preference') not in ('historical','current','ask'):raise ValueError('Неверная настройка имени')
                    if not isinstance(data.get('normalize_names'),bool):raise ValueError('Неверный формат настройки')
                    prefs.update(data);save_preferences(prefs)
                elif action=='start':
                    retained=[];files=list(STATE['files'])
                    if 'indices' in data:
                        ids=validate_revision(data)
                        targets={STATE['results'][i]['path'] for i in ids}
                        retained=[r for r in STATE['results'] if r['path'] not in targets]
                    if not files:raise ValueError('Сначала выберите музыку')
                    mode=data.get('mode')
                    if mode not in ('resume','recheck_cached','recheck'):raise ValueError('Неверный режим')
                    STATE.update(phase='review',message='Подготовка проверки…',action_error=None)
                    background(lambda:launch_worker(files,retained,mode))
                elif action=='last':
                    if phase in ('scanning','finishing','stopping','writing','review','prompt','preview','auditing','maintenance'):raise ValueError('Дождитесь окончания действия')
                    restore_library()
                elif action=='stop':
                    stop_search()
                elif action=='cover_cancel':
                    if not STATE.get('cover_running'):raise ValueError('Нет активной операции с обложками.')
                    (ROOT/'ui_runtime/cover_stop').touch()
                    STATE['message']='Останавливаем после текущего трека…'
                elif action in ('cover_search','cover_diagnose','cover_write','cover_import','cover_source','cover_upscale','cover_mark','cover_queue','cover_unqueue','cover_commit'):
                    if phase!='ready':raise ValueError('Дождитесь окончания текущей операции.')
                    ids=validate_revision(data)
                    if action == 'cover_queue':
                        choice = data.get('choice_key')
                        if choice != 'current' and (len(ids) != 1 or not isinstance(choice, str) or not re.fullmatch('[a-f0-9]{64}', choice)):
                            raise ValueError('Выберите конкретную обложку для очереди.')
                        if choice != 'current' and (not isinstance(data.get('original_image'), str) or not data['original_image']):
                            raise ValueError('Выберите обложку и дождитесь загрузки оригинала.')
                        if any(type(data.get(k, False)) is not bool for k in ('review_confirmed', 'upscale_small')):
                            raise ValueError('Некорректные параметры обложки.')
                    if (action in ('cover_write','cover_import','cover_source','cover_upscale') or 'query' in data) and len(ids)!=1:raise ValueError('Выберите один трек.')
                    if 'query' in data:
                        query=data['query']
                        if not isinstance(query,dict) or set(query)!={'artist','title','album'} or any(not isinstance(v,str) or len(v)>4096 or any(ord(c)<32 for c in v) for v in query.values()):raise ValueError('Некорректный поиск обложек.')
                        if not query['artist'].strip() or not query['title'].strip():raise ValueError('Укажите исполнителя и название.')
                    if action=='cover_mark' and data.get('decision') not in ('review','confirmed','clear'):raise ValueError('Некорректная отметка.')
                    if action=='cover_write' and any(type(data.get(k,False)) is not bool for k in ('replace_existing','review_confirmed','upscale_small','confirm_embedded','crop_square')):raise ValueError('Некорректное подтверждение.')
                    if action=='cover_write' and (not isinstance(data.get('candidate_id'),str) or len(data['candidate_id'])!=64):raise ValueError('Выберите найденную обложку.')
                    if action=='cover_write' and data.get('choice_key') is not None and (not isinstance(data['choice_key'],str) or len(data['choice_key'])!=64):raise ValueError('Выберите конкретный вариант обложки.')
                    if action in ('cover_upscale','cover_write','cover_queue'):
                        if type(data.get('crop_square',False)) is not bool or type(data.get('crop_position',50)) not in (int,float) or not 0<=data.get('crop_position',50)<=100:raise ValueError('Некорректная область обрезки.')
                    if action=='cover_upscale' and data.get('candidate_id')!='current' and (not isinstance(data.get('candidate_id'),str) or not re.fullmatch('[a-f0-9]{64}',data['candidate_id'])):raise ValueError('Выберите обложку для увеличения.')
                    if action=='cover_upscale' and data.get('choice_key') is not None and (not isinstance(data['choice_key'],str) or not re.fullmatch('[a-f0-9]{64}',data['choice_key'])):raise ValueError('Выберите конкретный вариант обложки.')
                    if action=='cover_import' and not isinstance(data.get('image'),str):raise ValueError('Выберите JPEG или PNG.')
                    if action=='cover_import' and data.get('origin','file') not in ('file','clipboard'):raise ValueError('Некорректный источник изображения.')
                    if action=='cover_source':
                        from core.cover_urls import source_url
                        data['source_url']=source_url(data.get('source_url',''))
                    (ROOT/'ui_runtime/cover_stop').unlink(missing_ok=True)
                    writing = action in ('cover_write', 'cover_commit')
                    STATE['cover_running']=action in ('cover_search','cover_diagnose','cover_commit')
                    STATE.update(phase='writing' if writing else 'review', action_error=None, done=0, total=len(ids),
                                 message='Вшиваем обложки…' if writing else 'Обрабатываем обложки…')
                    try:send({**data,'action':action,'indices':ids})
                    except Exception as error:
                        message='Не удалось передать команду рабочему процессу: '+str(error)+'. Обновите папку (F5), чтобы восстановить связь.'
                        STATE.update(phase='error',worker_synced=False,
                            cover_running=False,action_error=message,message=message,error_revision=STATE.get('error_revision',0)+1)
                        raise
                elif action=='release_search':
                    if phase!='ready':raise ValueError('Дождитесь окончания текущей операции.')
                    if data.get('revision')!=STATE.get('results_revision',0):raise ValueError('Список изменился; откройте редактор заново.')
                    index=data.get('index')
                    if type(index)!=int or not 0<=index<len(STATE['results']):raise ValueError('Трек не найден')
                    if data.get('path')!=STATE['results'][index]['path']:raise ValueError('Трек перемещён; откройте редактор заново.')
                    for key in ('artist','title','album_query'):
                        value=data.get(key,'')
                        if not isinstance(value,str) or len(value)>4096 or any(ord(c)<32 for c in value):raise ValueError('Некорректный запрос издания.')
                    if not data.get('artist','').strip() or not data.get('title','').strip():raise ValueError('Сначала заполните исполнителя и название.')
                    source=data.get('source_url','')
                    if not isinstance(source,str) or len(source)>4096:raise ValueError('Некорректная ссылка на источник.')
                    if source:
                        from core.cover_urls import bandcamp_source_url as source_url
                        source_url(source)
                    STATE.update(phase='review',action_error=None,message='Ищем синглы, альбомы и сборники…',release_search_warnings=[])
                    send({**data,'action':action})
                elif action in ('edit','edit_preview','reset_edit'):
                    if phase!='ready':raise ValueError('Редактор доступен после окончания проверки и перед записью')
                    if 'revision' in data and data['revision']!=STATE.get('results_revision',0):raise ValueError('Список изменился; откройте редактор заново.')
                    index=data.get('index')
                    if type(index)!=int or not 0<=index<len(STATE['results']):raise ValueError('Трек не найден')
                    if data.get('path',STATE['results'][index]['path'])!=STATE['results'][index]['path']:raise ValueError('Трек перемещён; откройте редактор заново.')
                    if action!='reset_edit':
                        from ui.editor import build_draft
                        build_draft(STATE['results'][index],data)
                    STATE.update(phase='review',action_error=None,message='Сохраняем ручной черновик…')
                    send({**data,'action':action,'index':index})
                elif action in ('preview','review','rename_preview','complete','audit_preview'):
                    if phase!='ready':raise ValueError('Сначала завершите проверку')
                    ids=validate_revision(data)
                    if action=='rename_preview' and 'name' in data and (len(ids)!=1 or not isinstance(data['name'],str)):
                        raise ValueError('Ручное имя доступно для одного файла')
                    if action=='complete' and data.get('release_index') is not None:
                        if len(ids)!=1:raise ValueError('Выбор издания доступен для одного трека.')
                        from ui.editor import build_draft,current_values
                        build_draft(STATE['results'][ids[0]],{**data,'values':current_values(STATE['results'][ids[0]])})
                    STATE['phase']='review'
                    if action=='complete':STATE.update(message='Сохраняем отметки исправления…',action_error=None)
                    send({**data,'action':action,'indices':list(dict.fromkeys(ids))})
                elif action=='answer':
                    if phase!='prompt':raise ValueError('Нет вопроса для ответа')
                    STATE.update(phase='review',prompt=None);send({'answer':str(data.get('answer','')), 'cancel':bool(data.get('cancel'))})
                elif action=='discard':
                    if phase!='preview':raise ValueError('Нет предпросмотра')
                    STATE['phase']='review';send({'action':'discard'})
                elif action=='write':
                    if phase!='preview' or not STATE['operations']:raise ValueError('Нет изменений для записи')
                    STATE['phase']='writing';send({'action':'write'})
                elif action=='open':
                    target=data.get('target')
                    if target in ('reports','backups'):
                        path=ROOT/target;path.mkdir(exist_ok=True)
                    else:
                        idx=data.get('index')
                        if type(idx)!=int or not 0<=idx<len(STATE['results']):raise ValueError('Не выбран трек')
                        path=Path(STATE['results'][idx]['path'])
                    if os.name=='nt':os.startfile(str(path))
                    else:subprocess.Popen(['xdg-open',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                elif action=='service':
                    if os.name!='nt':raise ValueError('Настройка сервисов этим мастером доступна в Windows')
                    os.startfile(str(ROOT/'configure_extra_catalogs.cmd'))
                else:raise ValueError('Неизвестное действие')
            self.respond({'ok':True})
        except Exception as error:
            with LOCK:
                if self.path in ('/cover_search','/cover_diagnose','/cover_write','/cover_import','/cover_source','/cover_upscale','/cover_mark','/cover_queue','/cover_unqueue','/cover_commit','/release_search','/start','/preview','/review','/rename_preview','/complete','/audit_preview','/answer','/discard','/write','/edit','/edit_preview','/reset_edit') and STATE['phase'] in ('scanning','review','writing') and (PROCESS is None or PROCESS.poll() is not None):
                    STATE['cover_running']=False
                    STATE.update(phase='error',message=str(error),prompt=None)
            self.respond({'error':str(error)},400)

if __name__=='__main__':
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    restore_library(automatic=True)
    url='http://127.0.0.1:'+str(server.server_port)
    print('TagCleaner UI: '+url+'\nОставьте это окно открытым. Отчёты и этапы сохраняются автоматически.',flush=True)
    if '--no-browser' not in sys.argv:webbrowser.open(url)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
