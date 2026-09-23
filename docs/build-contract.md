# Build contract

Scope: conversation + visible runtime harness only. No Judge, verdict, scores or eval product. Python FastAPI, SQLAlchemy (PostgreSQL production, SQLite local), plain browser JS/CSS.

## Engine contract

`app/engine.py`: `load_job() -> dict`; `initial_state(job, now: float) -> dict`; `initial_sheet(job) -> dict[criterion_id, dict]`; `build_context(job,state,sheet,hints,candidate_messages,latest)->dict`; `normalize(form)->dict`; `decide(job,state,sheet,form,message,message_id,now,hints=[]) -> dict`. Pure functions; copy inputs. Result `{state, sheet, history:[], hints:[], questions:[], flags:[], saved:[], facts:[], unknowns:[], prefix:[], question:str, path:'A'|'B', action:str}`. `state` is PROPOSED next state, committed only on delivery acknowledgement. history entries include criterion_id,event,accepted,reason,old_value,new_value,quote,message_id. hints include criterion_id,quote,message_id,resolved. Questions include text,type,fact_key. flags include type,quote,message_id,action. saved is list of accepted answer objects. Sheet rows include criterion_id + spec fields + missing_part. State uses Unix float times: started_at,deadline_at,last_candidate_msg_at; current_criterion_id,pending_action,last_question_asked,state,mode,counters,roleplay_turns,roleplay_breaks,roleplay_done,roleplay_last_line,callback_time,paused_at,close_reason. Engine also exposes `check_reply(output,facts,unknowns)->list[str]` and `fallback_reply(facts,unknowns)->dict` and `assemble(result,speech)->str`. Initial state current criterion first must-have, pending_action='ask', last_question_asked approved question. Roleplay decisions handle form.roleplay_event and an optional form.customer_line? Root orchestration handles customer generation separately.

## API / UI contract

GET `/api/bootstrap` -> `{product:'OnlyRound',job:{id,title,company,location,description,criteria:[{id,name,must_have,order}],facts:[]},model_ready:bool,model:str,auth_required:bool,authenticated:bool,harnesses:[{id,name,description}]}`.
POST `/api/login` `{access_code}` -> cookie auth; POST `/api/logout` clears.
All session endpoints require cookie auth when ACCESS_CODE configured. Browser owner cookie scopes sessions (cannot read other browser's sessions). Hosted ACCESS_CODE required.
GET `/api/sessions` -> `{sessions:[{id,state,created_at,updated_at,current_criterion_id,close_reason}]}`.
POST `/api/sessions` `{request_id:UUID}` -> full snapshot (idempotent). Client must persist and reuse request_id on retry.
GET `/api/sessions/{id}` -> full snapshot:
`{id,state:{...},job:{...},answer_sheet:[...],messages:[{id,role,content,created_at,turn_id,delivered}],history:[],hints:[],candidate_questions:[],flags:[],events:[{id,turn_id,kind,name,status,created_at,duration_ms,input,output,error}],pending_turn:{id,status,reply,error}|null,created_at,updated_at}`.
POST `/api/sessions/{id}/turns` `{request_id:UUID,message:str}` blocks while processing, returns `{turn_id,status:'awaiting_delivery'|'delivered',reply,snapshot}`. Poll snapshot every ~700ms while in flight to render live events. Idempotency key retry reuses same turn; a different payload for same key is 409. Errors safe JSON detail. 409 processing = poll; failed turns allow same-key retry.
POST `/api/sessions/{id}/turns/{turn_id}/ack` -> snapshot. Call ONLY after rendering reply. Pending new current_criterion/action/state (and follow-up counts) commit after ACK. On reload render pending reply then ACK. Candidate message is recorded immediately; answer updates + history persist atomically during preparation. Greeting similarly is returned as pending turn on create and must be rendered/ACKed before sending.
GET `/api/sessions/{id}/export` downloads full JSON audit (auth scoped).
GET `/health` -> health + DB readiness (no secrets).

UI: responsive polished dark studio; three areas: session sidebar, interview chat, inspector with Answer sheet / Live harness / History tabs. Expand prompt/results in events; no fake logs. Session resume/new, durable retry, local pending keys, live status, no judgment. Sample job explicitly demonstration config. Every snapshot contains only one session data. Never insert untrusted text as HTML.
