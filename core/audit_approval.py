"""Explicit review of saved diagnostics, bound to file state and source facts."""
from core.cache_utils import stable_digest

VERSION=1
SEMANTIC={'edition_identity','credit_placement','album_artist','name_variant'}
UNAPPROVABLE={'stale','unreadable'}
FIELD_REVIEWABLE={'credit_placement','album_artist','name_variant','single_album',
                  'verified_name','feat','title_structure','version_author','filename_credit'}

def values(data):
    from core.uniformity import FIELDS
    return {f:str(data.get(f) or '') for f in FIELDS}

def evidence(data):
    keys=('source','release_id','track_id','title','date','track_title','track_artist_credit',
          'track_artist_credit_items','album_artist_credit','album_artist_credit_items','duration','credit_roles_verified')
    selection=data.get('release_context_selection') or {}
    contexts=[*(data.get('release_contexts') or []),*(data.get('_ui_extra_releases') or []),
              data.get('_ui_manual_release'),data.get('selected_release_context'),selection.get('selected')]
    facts=sorted({stable_digest({k:c.get(k) for k in keys}) for c in contexts if isinstance(c,dict)})
    mb=data.get('musicbrainz') or {}
    return stable_digest([facts,{k:mb.get(k) for k in ('id','title','artist-credit')}])

def issue_key(issue):
    item={k:issue.get(k) for k in ('code','field','current','proposed','reason')}
    if item['field']=='file':item['current']=None
    return stable_digest(item)

def capture(data,issues=(),*,legacy=False):
    return {'version':VERSION,'signature':data.get('analysis_file_signature'),'values':values(data),
            'evidence':evidence(data),'issues':[issue_key(i) for i in issues if i.get('code') not in UNAPPROVABLE],
            'legacy_semantic':legacy}


def valid(data,approval=None):
    approval=data.get('_ui_audit_approval') if approval is None else approval
    return (isinstance(approval,dict) and approval.get('version')==VERSION
            and bool(approval.get('signature')) and approval['signature']==data.get('analysis_file_signature')
            and approval.get('values')==values(data) and approval.get('evidence')==evidence(data))


def reviewed_fields(data,rules=None):
    if (rules or {}).get('recheck_confirmed') and not data.get('_audit_confirmed_after_recheck'):
        return {}
    if not valid(data):return {}
    current=values(data)
    return {f:v for f,v in _field_map(data['_ui_audit_approval']).items()
            if f in current and current[f]==v}


def _field_map(approval):
    fields=approval.get('manual_fields')
    return fields if isinstance(fields,dict) else {}


def before_write(data,outcome):
    """Save only small approval facts, never copy old recognition payloads."""
    approval=data.get('_ui_audit_approval')
    return {'approval':dict(approval) if valid(data,approval) else None,
            'manual':{op['field']:str(op['proposed']) for op in outcome.get('operations',[])
                      if op.get('source')=='user_editor' and op.get('status')=='MANUAL'
                      and op.get('field') in values(data)}}


def after_write(data,review):
    """Rebind unchanged field choices after an application-controlled write.

    New source facts invalidate old choices. Explicit editor operations only
    approve their own successfully read-back fields, never unrelated warnings.
    """
    current=values(data);old=review['approval'];fields={};issues=[]
    if old and old.get('evidence')==evidence(data):
        fields={f:v for f,v in _field_map(old).items() if current.get(f)==v}
        if old.get('values')==current:issues=list(old.get('issues') or [])
    fields.update({f:v for f,v in review['manual'].items() if current.get(f)==v})
    data.pop('_ui_audit_approval',None)
    if fields or issues:
        approval=capture(data)
        approval.update(manual_fields=fields,issues=issues)
        data['_ui_audit_approval']=approval

def apply(data,entry):
    approval=data.get('_ui_audit_approval')
    if not valid(data,approval):return entry
    current=values(data)
    fields={f:v for f,v in _field_map(approval).items() if f in current and current[f]==v}
    approved=set(approval.get('issues') or []);pending=[];acknowledged=[]
    for i in entry['issues']:
        accepted=i.get('code') not in UNAPPROVABLE and (issue_key(i) in approved or
                 (i.get('code') in FIELD_REVIEWABLE and i.get('field') in fields) or
                 (approval.get('legacy_semantic') and i.get('code') in SEMANTIC and not i.get('automatic')))
        (acknowledged if accepted else pending).append(i)
    if not acknowledged:return entry
    # Artist/Title role edits are coupled. A reviewed half must not leave an
    # automatic edit that moves a guest into the other half of the credit.
    for index,i in enumerate(pending):
        if i.get('code')=='credit_placement' and any(f in fields for f in ('artist','title')):
            pending[index]={**i,'automatic':False}
    return {**entry,'issues':pending,'acknowledged':acknowledged,
            'fixes':{f:v for f,v in entry['fixes'].items() if any(i['field']==f and i['automatic'] and i.get('proposed')==v for i in pending)},
            'proposals':{f:v for f,v in entry['proposals'].items() if any(i['field']==f and i.get('proposed') is not None for i in pending)}}
