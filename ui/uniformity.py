"""Uniformity review sessions, independent of online recognition."""
import copy,json
from pathlib import Path
from datetime import datetime
from core.cache_utils import atomic_write_json,file_signature
from core.uniformity import FIELDS,DEFAULT_RULES,validate_rules,read_collection,audit_records,known_spellings
from core.tag_writer import make_operation

class UniformitySession:
    def __init__(self,root):
        self.root=Path(root);self.cache={};self.report=None;self.active=False
        self.confirmed_since_run=set()
        self.rules=copy.deepcopy(DEFAULT_RULES)
        try:self.rules=validate_rules(json.loads((self.root/'config/uniformity_rules.json').read_text(encoding='utf-8')))
        except (OSError,ValueError,TypeError):pass

    def run(self,files,previous,rules=None,progress=None,cancel=None,drafts=None,written=None):
        selected=validate_rules(rules if rules is not None else self.rules)
        fresh=read_collection(files,progress,cancel)
        groups=known_spellings(fresh)
        if self.report:
            for g in self.report['groups']:groups[g['id']].update(v['value'] for v in g['variants'])
        for gid,value in selected['canonical'].items():
            if self.rules['canonical'].get(gid)!=value and value not in groups.get(gid,set()):
                raise ValueError('Выберите написание из вариантов последнего отчёта.')
        old={d['path']:d for d in previous};merged=[]
        for record in fresh:
            original=old.get(record['path']);data=dict(original or {})
            changed=bool(original and (original.get('analysis_file_signature')!=record.get('analysis_file_signature') or any(str(original.get(f) or '')!=str(record.get(f) or '') for f in FIELDS)))
            data.pop('_audit_error',None)
            data.update(record)
            if not original or changed:
                plan=original.get('change_plan',{}) if original else {}
                data['change_plan']={'identity':'REVIEW',**{f:{'current':str(record.get(f) or ''),'proposed':str(record.get(f) or ''),
                    'recognition_proposed':(plan.get(f) or {}).get('proposed'),'status':'KEEP',
                    'reason':'Текущие теги прочитаны для диагностики оформления; распознавание не выполнялось.'} for f in FIELDS}}
            if changed:
                for k in ('_ui_written','_ui_completion_kind','_ui_written_at','_ui_name_approval','_ui_audit_approval','_ui_manual_release','_ui_extra_releases'):data.pop(k,None)
                if data.get('_ui_draft'):data['_ui_draft_warning']='Файл изменён вне приложения. Проверьте сохранённый черновик.'
                if any(str(original.get(f) or '')!=str(record.get(f) or '') for f in ('artist','title','remixer')):
                    data['release_contexts']=[];data['selected_release_context']=None;data['release_context_selection']={}
            if drafts:drafts.attach(data)
            if written:written.attach(data)
            merged.append(data)
        if written:written.flush_approval_migrations()
        self.rules=selected;self.active=True;self.cache={d['path']:d for d in fresh}
        self.confirmed_since_run.clear()
        atomic_write_json(self.root/'config/uniformity_rules.json',self.rules)
        self.recompute(merged)
        try:
            stamp=datetime.now().strftime('%Y-%m-%d_%H-%M-%S_%f')
            atomic_write_json(self.root/'reports'/('Uniformity_'+stamp+'.json'),self.report)
        except OSError:pass
        return merged

    def accept_confirmations(self,paths):
        """Only successful explicit confirmations made after this audit run."""
        self.confirmed_since_run.update(paths)

    def recompute(self,results,changed_paths=None):
        if not self.active:return None
        dirty=None if changed_paths is None else set(changed_paths)
        if self.report and self.report.get('rules')!=self.rules:dirty=None
        if dirty is not None:dirty.update(d['path'] for d in results if d['path'] not in self.cache)
        paths={d['path'] for d in results};self.cache={p:d for p,d in self.cache.items() if p in paths}
        for data in results:
            if dirty is not None and data['path'] not in dirty:continue
            if data['path'] not in self.cache:
                self.cache[data['path']]={k:data.get(k) for k in (*FIELDS,'path','file','duration','analysis_file_signature','_audit_error','raw_tags')}
            # Approval belongs to saved values, never a draft or an old snapshot.
            cached = self.cache[data['path']]
            cached['_audit_confirmed_after_recheck']=data['path'] in self.confirmed_since_run
            approval = data.get('_ui_name_approval') or {}
            if data.get('_ui_completion_kind') == 'manual' and data.get('_ui_written'):
                approval = {f:str(data.get(f) or '') for f in ('artist','albumartist','remixer','album')}
            cached['_ui_name_approval'] = {f:v for f,v in approval.items() if str(cached.get(f) or '') == v}
            # Audio fields always come from the fresh scan. Release credits
            # provide scoped evidence only, never replacements for saved tags.
            for key in ('release_contexts','_ui_extra_releases','selected_release_context','release_context_selection','_ui_manual_release','_ui_identity_manually_changed','musicbrainz','fallback_fusion','change_plan','_ui_audit_approval'):
                cached.pop(key,None)
                if key in data:cached[key]=copy.deepcopy(data[key])
        previous=self.report
        self.report=audit_records([self.cache[d['path']] for d in results],self.rules,
            reuse={e['path']:e for e in previous['entries']} if previous and dirty is not None else None,
            changed_paths=dirty,previous_groups=previous['groups'] if previous else None)
        bypath={r['path']:r for r in self.report['entries']}
        for data in results:
            data['_ui_audit']=bypath[data['path']]
            data['_audit_confirmed_after_recheck']=data['path'] in self.confirmed_since_run
        return self.report

    def event(self,results,open_view=False):
        return {'updates':[{'index':i,'audit':d.get('_ui_audit')} for i,d in enumerate(results)],
                'summary':self.report['summary'],'groups':self.report['groups'],'rules':self.rules,'open_view':open_view}

    def preview(self,results,indices):
        if not self.active:raise ValueError('Сначала выполните диагностику единообразия.')
        operations,skipped=[],[]
        for index in indices:
            data=results[index];audit=data.get('_ui_audit') or {};fresh=self.cache.get(data['path'])
            reason=None
            if data.get('_ui_draft_warning'):reason='Черновик требует проверки. Откройте редактор перед записью.'
            elif not fresh or not audit.get('signature'):reason='Неизвестно состояние файла. Повторите диагностику.'
            else:
                try:
                    if file_signature(data['path'])!=audit['signature']:reason='Файл изменился. Повторите диагностику.'
                except OSError:reason='Файл недоступен.'
            if reason:skipped.append({'file':data['file'],'reason':reason});continue
            if data.get('_ui_draft'):
                from ui.editor import draft_operations
                try:
                    planned=draft_operations(data)
                    operations.extend(planned)
                    if not planned:skipped.append({'file':data['file'],'reason':'Значения черновика уже записаны в файле. Можно подтвердить текущие теги.'})
                except ValueError as error:skipped.append({'file':data['file'],'reason':str(error)})
                continue
            fixes=audit.get('fixes') or {}
            if not fixes:skipped.append({'file':data['file'],'reason':'Нужна ручная проверка или выбор написания в правилах диагностики.'});continue
            for field,proposed in fixes.items():
                op=make_operation(fresh,field,{'current':str(fresh.get(field) or ''),'proposed':proposed,'status':'MANUAL'})
                if op:op['source']='uniformity';operations.append(op)
                elif str(fresh.get(field) or '')!=proposed:skipped.append({'file':data['file'],'reason':'Запись поля не поддерживается: '+field})
        return operations,skipped

    def after_write(self,data,outcome,drafts):
        from ui.editor import refresh_after_write
        from core.scanner import scan_file
        if not outcome.get('success'):refresh_after_write(data,outcome,drafts);return
        if data.get('_ui_draft'):
            refresh_after_write(data,outcome,drafts)
            self.cache[data['path']]={k:data.get(k) for k in
                (*FIELDS,'path','file','duration','analysis_file_signature','raw_tags')}
            return
        from core.audit_approval import before_write,after_write
        review=before_write(data,outcome)
        fresh=scan_file(data['path']);plan=copy.deepcopy(data.get('change_plan') or {})
        fields={o['field'] for o in outcome.get('operations',[])}
        data.update(fresh)
        for field in FIELDS:
            old=plan.get(field) or {};old['current']=str(fresh.get(field) or '')
            if field in fields:old.update(proposed=str(fresh.get(field) or ''),status='KEEP',reason='Оформление исправлено по выбранным правилам.')
            plan[field]=old
        data['change_plan']=plan
        after_write(data,review)
        self.cache[data['path']]=fresh
