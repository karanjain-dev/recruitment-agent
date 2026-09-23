from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
load_dotenv()
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.engine import load_job, initial_state, initial_sheet
from app.model import ModelGateway, ModelError, model_ready
from app.runtime import handle_turn
from app.storage import (Store, Interview, Turn, Message, Answer, History, Hint,
                         CandidateQuestion, Flag, Trace, uid, save_sheet, add_history)

ROOT = Path(__file__).resolve().parent.parent
store = Store()
JOB = load_job()
ACCESS_CODE = os.getenv('ACCESS_CODE','')
PRODUCTION = os.getenv('ENVIRONMENT') == 'production'
COOKIE_SECRET = os.getenv('COOKIE_SECRET') or secrets.token_urlsafe(48)
HARNESSES = [
    {'id':'session','name':'Session guard','description':'Checks state, deadlines, ownership and retry keys.'},
    {'id':'understand','name':'Understand','description':'A model proposes a structured reading of the latest message.'},
    {'id':'validate','name':'Answer validator','description':'Code checks evidence, events, duplicates and confirmation holds.'},
    {'id':'facts','name':'Job facts lookup','description':'Answers only from approved job facts; unknowns go to the recruiter.'},
    {'id':'progress','name':'Progression engine','description':'Code chooses the next approved question and owns follow-up limits.'},
    {'id':'speak','name':'Speak & reply check','description':'Fixed wording or a bounded acknowledgement checked before delivery.'},
    {'id':'delivery','name':'Delivery commit','description':'Records the next question only after the browser displays the reply.'},
]

def public_job(job):
    return {**{k:job.get(k) for k in ('id','title','company','location','description')},
            'criteria':[{k:c.get(k) for k in ('id','name','must_have','order')} for c in job['criteria']],
            'facts':job['facts']}

def sign(value): return value + '.' + hmac.new(COOKIE_SECRET.encode(),value.encode(),hashlib.sha256).hexdigest()
def unsigned(value):
    if not value or '.' not in value: return None
    raw,signature = value.rsplit('.',1)
    return raw if hmac.compare_digest(sign(raw).rsplit('.',1)[1].encode(), signature.encode()) else None
def verified_cookies(request, kind):
    canonical = f'AIrecruiter_{kind}'
    names = [canonical] + [name for name in request.cookies if name != canonical and name.endswith('_' + kind)]
    pattern = r'[0-9]{1,12}' if kind == 'auth' else r'[0-9a-f]{48}'
    for name in names:
        raw = unsigned(request.cookies.get(name))
        if raw and re.fullmatch(pattern, raw): yield name, raw
def current_cookie(request, kind):
    for name, raw in verified_cookies(request, kind):
        if kind != 'auth' or int(raw) > time.time(): return name, raw
    return None, None
def authenticated(request):
    if not ACCESS_CODE: return True
    return current_cookie(request, 'auth')[1] is not None
def owner(request):
    if not authenticated(request): raise HTTPException(401,'Enter the studio access code to continue.')
    value = current_cookie(request, 'owner')[1]
    if not value: raise HTTPException(401,'Refresh the page to initialise your session.')
    return value
def cookie(response, name, value, age=2592000):
    response.set_cookie(name,sign(value),max_age=age,httponly=True,secure=PRODUCTION,samesite='strict',path='/')

def owned(db, session_id, owner_id, lock=False):
    q = db.query(Interview).filter_by(id=session_id,owner=owner_id)
    row = (q.with_for_update() if lock else q).first()
    if not row: raise HTTPException(404,'Interview not found.')
    return row

def snapshot(db, session):
    sid = session.id
    messages = db.query(Message).filter_by(session_id=sid).order_by(Message.created_at,Message.id).all()
    traces = db.query(Trace).filter_by(session_id=sid).order_by(Trace.created_at,Trace.id).all()
    turn = db.get(Turn,session.active_turn_id) if session.active_turn_id else None
    return {
        'id':sid,'state':session.state,'job':public_job(session.job),
        'answer_sheet':[dict(a.payload,id=a.id,criterion_id=a.criterion_id) for a in db.query(Answer).filter_by(session_id=sid)],
        'messages':[{'id':m.id,'role':m.role,'content':m.content,'created_at':m.created_at,'turn_id':m.turn_id,'delivered':m.delivered,'criterion_id':m.criterion_id} for m in messages],
        'history':[dict(h.payload,id=h.id,created_at=h.created_at) for h in db.query(History).filter_by(session_id=sid).order_by(History.created_at,History.id)],
        'hints':[dict(h.payload,id=h.id) for h in db.query(Hint).filter_by(session_id=sid)],
        'candidate_questions':[dict(q.payload,id=q.id,message_id=q.message_id) for q in db.query(CandidateQuestion).filter_by(session_id=sid)],
        'flags':[dict(f.payload,id=f.id) for f in db.query(Flag).filter_by(session_id=sid)],
        'events':[{k:getattr(e,k) for k in ('id','turn_id','kind','name','status','created_at','duration_ms','input','output','error')} for e in traces],
        'pending_turn':{'id':turn.id,'status':turn.status,'reply':turn.reply,'error':turn.error} if turn else None,
        'created_at':session.created_at,'updated_at':session.updated_at,
    }

def redact(value):
    encoded=json.dumps(value,ensure_ascii=False,default=str)
    for name in ('OPENAI_API_KEY','ACCESS_CODE','COOKIE_SECRET','DATABASE_URL'):
        secret=os.getenv(name)
        if secret and len(secret)>6: encoded=encoded.replace(secret,'[redacted]')
    return json.loads(encoded)

def emitter(sid,tid):
    async def emit(kind,name,status,input=None,output=None,duration_ms=None,error=None):
        with store.tx() as db:
            db.add(Trace(session_id=sid,turn_id=tid,kind=kind,name=name,status=status,
                         input=redact(input),output=redact(output),duration_ms=duration_ms,
                         error=json.dumps(redact(error)) if isinstance(error,dict) else redact(error)))
    return emit

async def abandon_worker():
    while True:
        await asyncio.sleep(60)
        now=time.time()
        with store.tx() as db:
            for session in db.query(Interview).filter(Interview.active_turn_id.is_(None)).with_for_update():
                state=copy.deepcopy(session.state)
                if state.get('state') not in ('open','roleplay') or now-state.get('last_candidate_msg_at',session.created_at)<1800: continue
                state.update(state='closed',close_reason='abandoned',pending_action=None,current_criterion_id=None)
                session.state=state
                session.updated_at=now
                for answer in db.query(Answer).filter_by(session_id=session.id):
                    item=copy.deepcopy(answer.payload)
                    if item['status']=='needs_confirmation': item['status']='unclear_final'
                    elif item['status'] in ('not_asked','asked','off_target'): item['status']='unasked'
                    answer.payload=item

@asynccontextmanager
async def lifespan(app):
    if PRODUCTION and (not ACCESS_CODE or not os.getenv('COOKIE_SECRET') or not os.getenv('DATABASE_URL','').startswith(('postgres','postgresql'))):
        raise RuntimeError('Production requires PostgreSQL, ACCESS_CODE and COOKIE_SECRET.')
    store.init()
    store.migrate_product_name()
    worker=asyncio.create_task(abandon_worker())
    yield
    worker.cancel()
    try: await worker
    except asyncio.CancelledError: pass

app=FastAPI(title='AIrecruiter Conversation Studio',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None)

@app.middleware('http')
async def security(request,call_next):
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin=request.headers.get('origin')
        if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail':'Cross-origin requests are not allowed.'},status_code=403)
        try:
            content_length=int(request.headers.get('content-length','0') or 0)
        except ValueError:
            return JSONResponse({'detail':'Invalid request length.'},status_code=400)
        if content_length>20000:
            return JSONResponse({'detail':'Request too large.'},status_code=413)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    response.headers['Cache-Control']='no-store' if request.url.path.startswith('/api/') else 'no-cache'
    if PRODUCTION: response.headers['Strict-Transport-Security']='max-age=31536000'
    for kind in ('owner', 'auth'):
        # These endpoints own the auth response; migration must never undo logout.
        if kind == 'auth' and request.url.path in ('/api/login', '/api/logout'): continue
        canonical = f'AIrecruiter_{kind}'
        source, raw = current_cookie(request, kind)
        if raw:
            if source != canonical:
                age = max(1, int(raw) - int(time.time())) if kind == 'auth' else 2592000
                cookie(response, canonical, raw, age)
            for name, _ in verified_cookies(request, kind):
                if name != canonical: response.delete_cookie(name, path='/')
        elif kind == 'owner': cookie(response, canonical, secrets.token_hex(24))
    return response

@app.get('/health')
def health():
    try:
        with store.read() as db: db.execute(text('SELECT 1'))
    except Exception: return JSONResponse({'status':'unhealthy'},status_code=503)
    return {'status':'ok','database':'connected','model_ready':model_ready()}

@app.get('/api/bootstrap')
def bootstrap(request:Request):
    return {'product':'AIrecruiter','job':public_job(JOB),'model_ready':model_ready(),
            'model':os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'auth_required':bool(ACCESS_CODE),
            'authenticated':authenticated(request),'harnesses':HARNESSES}

class LoginBody(BaseModel): access_code:str=Field(max_length=256)
login_attempts={}
@app.post('/api/login')
def login(body:LoginBody,request:Request,response:Response):
    ip=request.client.host if request.client else 'unknown'
    now=time.time()
    attempts=[t for t in login_attempts.get(ip,[]) if now-t<600]
    if len(attempts)>=10: raise HTTPException(429,'Too many attempts. Try again in ten minutes.')
    if ACCESS_CODE and not hmac.compare_digest(body.access_code.encode(),ACCESS_CODE.encode()):
        login_attempts[ip]=attempts+[now]
        raise HTTPException(401,'That access code is not correct.')
    cookie(response,'AIrecruiter_auth',str(int(now+86400)),86400)
    for name, _ in verified_cookies(request, 'auth'):
        if name != 'AIrecruiter_auth': response.delete_cookie(name, path='/')
    login_attempts.pop(ip,None)
    return {'ok':True}

@app.post('/api/logout')
def logout(request:Request,response:Response):
    response.delete_cookie('AIrecruiter_auth',path='/')
    for name, _ in verified_cookies(request, 'auth'):
        if name != 'AIrecruiter_auth': response.delete_cookie(name, path='/')
    return {'ok':True}

@app.get('/api/sessions')
def sessions(request:Request):
    owner_id=owner(request)
    with store.read() as db:
        return {'sessions':[{'id':s.id,'state':s.state['state'],'created_at':s.created_at,'updated_at':s.updated_at,
                            'current_criterion_id':s.state.get('current_criterion_id'),'close_reason':s.state.get('close_reason')}
                           for s in db.query(Interview).filter_by(owner=owner_id).order_by(Interview.updated_at.desc()).limit(100)]}

class NewSession(BaseModel): request_id:UUID
@app.post('/api/sessions')
def create_session(body:NewSession,request:Request):
    owner_id=owner(request)
    try:
        return create_session_once(body,owner_id)
    except IntegrityError:
        # PostgreSQL can race on the initial read. Its unique owner/request key
        # makes one transaction win; return that durable result after rollback.
        with store.read() as db:
            session=db.query(Interview).filter_by(owner=owner_id,request_id=str(body.request_id)).first()
            if session: return snapshot(db,session)
        raise

def create_session_once(body,owner_id):
    with store.tx() as db:
        session=db.query(Interview).filter_by(owner=owner_id,request_id=str(body.request_id)).first()
        if session: return snapshot(db,session)
        if db.query(Interview).filter_by(owner=owner_id).filter(Interview.created_at>time.time()-3600).count()>=30:
            raise HTTPException(429,'Session limit reached. Resume an existing interview.')
        now=time.time(); sid=uid(); tid=uid()
        desired=initial_state(JOB,now); sheet=initial_sheet(JOB)
        state=copy.deepcopy(desired)
        state.update(current_criterion_id=None,pending_action=None,last_question_asked=None)
        session=Interview(id=sid,owner=owner_id,request_id=str(body.request_id),job=JOB,state=state,active_turn_id=tid)
        db.add(session); db.flush()
        question=desired['last_question_asked']
        reply=JOB['fixed_lines']['greeting']+' '+question
        result={'state':desired,'sheet':sheet,'history':[]}
        db.add(Turn(id=tid,session_id=sid,request_id=str(body.request_id),status='awaiting_delivery',reply=reply,result=result))
        db.flush()
        db.add(Message(session_id=sid,turn_id=tid,role='assistant',content=reply,delivered=False))
        undelivered_sheet=copy.deepcopy(sheet)
        for answer in undelivered_sheet.values(): answer['status']='not_asked'
        save_sheet(db,sid,undelivered_sheet)
        db.add(Trace(session_id=sid,turn_id=tid,kind='harness',name='Delivery commit',status='waiting',output={'action':'greeting','awaiting':'browser acknowledgement'}))
        db.flush()
        return snapshot(db,session)

@app.get('/api/sessions/{session_id}')
def get_session(session_id:str,request:Request):
    with store.read() as db: return snapshot(db,owned(db,session_id,owner(request)))

@app.get('/api/sessions/{session_id}/export')
def export_session(session_id:str,request:Request):
    with store.read() as db: data=snapshot(db,owned(db,session_id,owner(request)))
    return JSONResponse(data,headers={'Content-Disposition':f'attachment; filename="AIrecruiter-{session_id}.json"'})

def persist_result(db,sid,tid,mid,result):
    save_sheet(db,sid,result.get('pre_delivery_sheet',result['sheet']))
    add_history(db,sid,tid,result.get('history',[]),'prepare')
    for hint in result.get('hints',[]):
        key=dict(session_id=sid,criterion_id=hint['criterion_id'],message_id=hint.get('message_id',mid))
        row=db.query(Hint).filter_by(**key).first()
        if row: row.payload=hint
        else: db.add(Hint(**key,payload=hint))
    for q in result.get('questions',[]):
        if not db.query(CandidateQuestion).filter_by(message_id=mid,question_text=q['text']).first():
            db.add(CandidateQuestion(session_id=sid,message_id=mid,question_text=q['text'],payload=q))
    for flag in result.get('flags',[]):
        if not db.query(Flag).filter_by(message_id=mid,type=flag['type']).first():
            db.add(Flag(session_id=sid,message_id=mid,type=flag['type'],payload=flag))

class TurnBody(BaseModel):
    request_id:UUID
    message:str=Field(min_length=1,max_length=6000)

class SupersededAttempt(Exception):
    """A recovered processing lease now belongs to another request attempt."""

def active_attempt(db,session_id,turn_id,attempt_token):
    session=db.query(Interview).filter_by(id=session_id).with_for_update().one()
    turn=db.get(Turn,turn_id)
    if session.active_turn_id!=turn_id or turn.attempt_token!=attempt_token or turn.status!='processing':
        raise SupersededAttempt()
    return session,turn

@app.post('/api/sessions/{session_id}/turns')
async def candidate_turn(session_id:str,body:TurnBody,request:Request):
    owner_id=owner(request); message=body.message.strip()
    if not message: raise HTTPException(422,'Enter a message.')
    with store.tx() as db:
        session=owned(db,session_id,owner_id,True)
        turn=db.query(Turn).filter_by(session_id=session_id,request_id=str(body.request_id)).first()
        if turn and turn.candidate_text!=message: raise HTTPException(409,'This retry key belongs to a different message.')
        if turn and turn.status in ('awaiting_delivery','delivered'):
            return {'turn_id':turn.id,'status':turn.status,'reply':turn.reply,'snapshot':snapshot(db,session)}
        if session.active_turn_id and (not turn or session.active_turn_id!=turn.id):
            raise HTTPException(409,'The previous reply must finish and be displayed first.')
        if turn and turn.status=='processing' and (session.lease_until or 0)>time.time():
            raise HTTPException(409,'This message is still processing. Its result will appear shortly.')
        if session.state['state']=='closed': raise HTTPException(409,'This interview is closed. Start a new interview.')
        if not model_ready(): raise HTTPException(503,'The model connection is not configured. Add the server API key.')
        if not turn:
            turn=Turn(id=uid(),session_id=session_id,request_id=str(body.request_id),candidate_text=message)
            db.add(turn); db.flush()
            candidate=Message(id=uid(),session_id=session_id,turn_id=turn.id,role='candidate',content=message,
                              delivered=True,criterion_id=session.state.get('current_criterion_id'))
            db.add(candidate); db.flush()
        else:
            candidate=db.query(Message).filter_by(turn_id=turn.id,role='candidate').one()
            turn.status='processing'; turn.error=None
        attempt_token=uid(); turn.attempt_token=attempt_token
        session.active_turn_id=turn.id; session.lease_until=time.time()+180; session.updated_at=time.time()
        state=copy.deepcopy(session.state)
        sheet={a.criterion_id:copy.deepcopy(a.payload) for a in db.query(Answer).filter_by(session_id=session_id)}
        hints=[h.payload for h in db.query(Hint).filter_by(session_id=session_id)]
        history=db.query(Message).filter_by(session_id=session_id,role='candidate').order_by(Message.created_at.desc()).limit(7).all()
        candidate_messages=[m.content for m in reversed(history)]
        job=copy.deepcopy(session.job); tid=turn.id; mid=candidate.id
        prepared=copy.deepcopy(turn.result)
    emit=emitter(session_id,tid)
    gateway=ModelGateway()
    try:
        await emit('harness','Session guard','completed',output={'state':state['state'],'message_id':mid,'request_id':str(body.request_id)})
        async def on_prepared(result):
            with store.tx() as db:
                _,active=active_attempt(db,session_id,tid,attempt_token)
                persist_result(db,session_id,tid,mid,result)
                active.result=result
        result,reply=await handle_turn(job=job,state=state,sheet=sheet,hints=hints,
                                     candidate_messages=candidate_messages,message=message,message_id=mid,
                                     gateway=gateway,emit=emit,now=time.time,prepared=prepared,
                                     on_prepared=on_prepared)
        with store.tx() as db:
            active_session,turn=active_attempt(db,session_id,tid,attempt_token)
            turn.result=result; turn.reply=reply; turn.status='awaiting_delivery'; turn.error=None
            assistant=db.query(Message).filter_by(turn_id=tid,role='assistant').first()
            if not assistant: db.add(Message(session_id=session_id,turn_id=tid,role='assistant',content=reply,delivered=False))
            else: assistant.content=reply
            active_session.lease_until=None
        await emit('harness','Delivery commit','waiting',output={'awaiting':'browser acknowledgement','next_question_committed':False})
        with store.read() as db:
            return {'turn_id':tid,'status':'awaiting_delivery','reply':reply,'snapshot':snapshot(db,db.get(Interview,session_id))}
    except SupersededAttempt:
        raise HTTPException(409,'A retry is already processing this message. Refresh for its current result.')
    except (Exception,asyncio.CancelledError) as exc:
        safe=str(exc) if isinstance(exc,ModelError) else 'The turn could not finish. Retry the same message; saved evidence is retained.'
        with store.tx() as db:
            session=db.query(Interview).filter_by(id=session_id).with_for_update().one()
            turn=db.get(Turn,tid)
            if session.active_turn_id!=tid or turn.attempt_token!=attempt_token:
                raise HTTPException(409,'A retry is already processing this message. Refresh for its current result.')
            turn.status='failed'; turn.error=safe
            session.lease_until=None
        await emit('harness','Turn recovery','failed',error=safe,output={'retry_same_request_id':True,'next_question_committed':False})
        if isinstance(exc,asyncio.CancelledError): raise
        raise HTTPException(502,safe)

@app.post('/api/sessions/{session_id}/turns/{turn_id}/ack')
def acknowledge(session_id:str,turn_id:str,request:Request):
    with store.tx() as db:
        session=owned(db,session_id,owner(request),True)
        turn=db.query(Turn).filter_by(id=turn_id,session_id=session_id).first()
        if not turn: raise HTTPException(404,'Turn not found.')
        if turn.status=='delivered': return snapshot(db,session)
        if turn.status!='awaiting_delivery' or session.active_turn_id!=turn.id: raise HTTPException(409,'This reply is not ready to acknowledge.')
        result=turn.result
        session.state=result['state']; session.updated_at=time.time(); session.active_turn_id=None; session.lease_until=None
        save_sheet(db,session_id,result['sheet'])
        add_history(db,session_id,turn_id,result.get('history',[]),'delivery')
        turn.status='delivered'
        db.query(Message).filter_by(turn_id=turn_id,role='assistant').one().delivered=True
        db.add(Trace(session_id=session_id,turn_id=turn_id,kind='harness',name='Delivery commit',status='completed',
                     output={'next_question_committed':True,'pending_action':session.state.get('pending_action')}))
        db.flush()
        return snapshot(db,session)

app.mount('/static',StaticFiles(directory=ROOT/'web'),name='static')
@app.get('/')
def index(): return FileResponse(ROOT/'web'/'index.html')
