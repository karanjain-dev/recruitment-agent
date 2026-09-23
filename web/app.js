/* OnlyRound conversation studio. The server owns every interview decision. */
'use strict';

const $ = (id) => document.getElementById(id);
const STORAGE_PREFIX = 'onlyround.v1.';
const state = {
  bootstrap: null, sessions: [], snapshot: null, activeId: null, activeTab: 'answers',
  busy: false, creating: false, polling: null, pollingBusy: false, sending: new Set(),
  acknowledging: new Set(), acknowledged: new Set(), expandedEvents: new Set(),
  messageSignature: '', inspectorSignature: '', error: null, loadVersion: 0,
  toastTimer: null, lastSessionRefresh: 0, loginBusy: false,
};

class APIError extends Error {
  constructor(message, status, detail) { super(message); this.status = status; this.detail = detail; }
}

function storageGet(key) {
  try { return JSON.parse(localStorage.getItem(STORAGE_PREFIX + key)); } catch { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(STORAGE_PREFIX + key, JSON.stringify(value)); return true; }
  catch { return false; }
}
function storageRemove(key) {
  try { localStorage.removeItem(STORAGE_PREFIX + key); } catch { /* Storage may be unavailable. */ }
}
function pendingKey(id) { return `pending.${id}`; }
function uuid() { return crypto.randomUUID(); }
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function nice(value) {
  return String(value ?? '').replaceAll('_', ' ').replaceAll('-', ' ').replace(/^./, (c) => c.toUpperCase());
}
function parseTime(value) {
  if (typeof value === 'number') return new Date(value < 1e12 ? value * 1000 : value);
  return value ? new Date(value) : new Date();
}
function timeLabel(value) {
  const date = parseTime(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
function dateLabel(value) {
  const date = parseTime(value);
  if (Number.isNaN(date.getTime())) return '';
  const today = new Date();
  return date.toDateString() === today.toDateString() ? 'Today' : date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}
function textValue(value) {
  if (value === null || value === undefined) return '';
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}
function eventIsRunning(event, index, events) {
  if (!['running', 'started', 'pending', 'processing'].includes(event.status)) return false;
  return !events.slice(index + 1).some((later) => later.turn_id === event.turn_id && later.name === event.name && ['completed', 'complete', 'failed', 'error'].includes(later.status));
}
function errorMessage(error) {
  if (error instanceof APIError) return error.message;
  if (!navigator.onLine) return 'You’re offline. Your request is kept on this device. Reconnect and retry.';
  return 'The connection was interrupted. Retry to continue the same turn.';
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: 'same-origin',
    headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body.detail ?? body.error ?? 'The request could not be completed.';
    const message = typeof detail === 'string' ? detail : detail.message ?? 'The request could not be completed.';
    if (response.status === 401) showLogin();
    throw new APIError(message, response.status, detail);
  }
  return body;
}
function post(path, body) { return api(path, { method: 'POST', body: JSON.stringify(body ?? {}) }); }
function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}

function showLogin() {
  $('login-screen').hidden = false;
  document.querySelector('.app-shell').inert = true;
  stopPolling();
  state.busy = false;
  setTimeout(() => $('access-code').focus(), 50);
}
function hideLogin() {
  $('login-screen').hidden = true;
  document.querySelector('.app-shell').inert = false;
}

async function boot() {
  try {
    state.bootstrap = await api('/api/bootstrap');
    renderBootstrap();
    if (state.bootstrap.auth_required && !state.bootstrap.authenticated) { showLogin(); return; }
    hideLogin();
    await refreshSessions();
    const previous = storageGet('activeSession');
    if (previous && state.sessions.some((s) => s.id === previous)) await openSession(previous);
    else renderEmpty();
  } catch (error) {
    if (error.status === 401) return;
    $('model-label').textContent = 'Studio connection unavailable';
    $('conversation-notice').textContent = 'We couldn’t connect to the workspace. Check your connection, then reload this page.';
    $('conversation-notice').hidden = false;
    toast(errorMessage(error));
  }
}

function renderBootstrap() {
  const job = state.bootstrap?.job ?? {};
  $('job-title').textContent = job.title ?? 'Screening interview';
  $('job-company').textContent = job.company ?? 'Demo employer';
  $('job-location').textContent = job.location ?? 'Demo configuration';
  const ready = Boolean(state.bootstrap?.model_ready);
  $('model-dot').classList.toggle('ready', ready);
  $('model-label').textContent = ready ? 'Agent connected' : 'Model setup needed';
  $('model-label').title = ready ? `Configured model: ${state.bootstrap.model}` : 'Configure a model API key on the server.';
  $('logout').hidden = !state.bootstrap?.auth_required;
  renderNotice();
  renderInspector(true);
}

function renderNotice() {
  const notice = $('conversation-notice');
  const sessionState = state.snapshot?.state;
  if (!state.bootstrap?.model_ready) {
    notice.textContent = 'The workspace is ready. An administrator needs to connect the model before the agent can respond.';
    notice.hidden = false;
  } else if (sessionState?.state === 'paused_callback') {
    notice.textContent = sessionState.callback_time ? `Interview paused. Requested callback: ${sessionState.callback_time}. Send a message to resume.` : 'Interview paused. Send a message when you’re ready to resume.';
    notice.hidden = false;
  } else if (sessionState?.state === 'closed') {
    notice.textContent = 'This conversation has ended. Your answer sheet and full activity record are available to review or export.';
    notice.hidden = false;
  } else notice.hidden = true;
}

async function refreshSessions() {
  const result = await api('/api/sessions');
  state.sessions = result.sessions ?? [];
  state.lastSessionRefresh = Date.now();
  renderSessions();
}

function renderSessions() {
  const list = $('session-list');
  list.replaceChildren();
  $('session-count').textContent = state.sessions.length;
  if (!state.sessions.length) {
    list.append(node('p', 'rail-empty', 'Your interviews will appear here. Every conversation keeps its own record.'));
    return;
  }
  for (const session of state.sessions) {
    const selected = session.id === state.activeId;
    const button = node('button', `session-item${selected ? ' active' : ''}`);
    button.type = 'button';
    button.setAttribute('aria-current', selected ? 'true' : 'false');
    button.title = `Interview ${session.id}`;
    button.append(node('span', 'session-item-icon', '◧'));
    const main = node('div', 'session-item-main');
    main.append(node('strong', '', `Interview ${session.id.slice(0, 6)}`));
    const meta = node('span', 'session-item-meta');
    const status = typeof session.state === 'object' ? session.state.state : session.state;
    meta.append(node('span', '', dateLabel(session.created_at)), node('i'), node('span', '', status === 'closed' ? 'Ended' : status === 'paused_callback' ? 'Paused' : 'In progress'));
    main.append(meta); button.append(main);
    if (selected) button.append(node('span', 'session-item-active-dot'));
    button.addEventListener('click', () => openSession(session.id));
    list.append(button);
  }
}

async function createSession() {
  if (state.creating) return;
  state.creating = true;
  $('new-session').disabled = true;
  $('welcome-start').disabled = true;
  const requestId = storageGet('newSessionRequest') ?? uuid();
  if (!storageSet('newSessionRequest', requestId)) {
    toast('Allow browser storage to start an interview with reliable recovery.');
    state.creating = false; $('new-session').disabled = false; $('welcome-start').disabled = false;
    return;
  }
  try {
    const snapshot = await post('/api/sessions', { request_id: requestId });
    storageRemove('newSessionRequest');
    stopPolling();
    state.loadVersion++;
    state.activeId = snapshot.id;
    state.snapshot = null;
    state.error = null;
    state.busy = false;
    state.messageSignature = '';
    state.inspectorSignature = '';
    storageSet('activeSession', snapshot.id);
    closeMobileMenu();
    applySnapshot(snapshot);
    await refreshSessions();
    $('message-input').focus();
  } catch (error) {
    if (error.status !== 401) toast(errorMessage(error));
  } finally {
    state.creating = false;
    $('new-session').disabled = false;
    $('welcome-start').disabled = false;
  }
}

async function openSession(id) {
  if (id === state.activeId && state.snapshot) { closeMobileMenu(); return; }
  const version = ++state.loadVersion;
  stopPolling();
  state.activeId = id;
  state.snapshot = null;
  state.error = null;
  state.busy = true;
  state.messageSignature = '';
  state.inspectorSignature = '';
  storageSet('activeSession', id);
  closeMobileMenu();
  renderSessions();
  renderComposer();
  $('welcome').hidden = true;
  $('messages').hidden = false;
  $('messages').replaceChildren();
  $('processing').hidden = false;
  $('processing-label').textContent = 'Opening your conversation';
  try {
    const snapshot = await api(`/api/sessions/${id}`);
    if (version !== state.loadVersion) return;
    const pending = storageGet(pendingKey(id));
    state.busy = Boolean(pending) || snapshot.pending_turn?.status === 'processing';
    applySnapshot(snapshot);
    if (pending) {
      // Reusing the durable key is safe even if the original response was lost.
      sendTurn(pending);
    } else if (snapshot.pending_turn?.status === 'processing') startPolling();
  } catch (error) {
    if (version !== state.loadVersion) return;
    state.busy = false;
    if (error.status === 404) {
      storageRemove('activeSession');
      state.activeId = null;
      renderEmpty();
      await refreshSessions().catch(() => {});
    } else if (error.status !== 401) {
      state.error = { message: errorMessage(error), action: 'load' };
      renderError(); renderComposer();
    }
  }
}

function renderEmpty() {
  state.snapshot = null;
  state.activeId = null;
  state.busy = false;
  state.error = null;
  $('welcome').hidden = false;
  $('messages').hidden = true;
  $('processing').hidden = true;
  $('session-status').textContent = 'Ready when you are';
  $('session-status').className = 'session-status';
  $('session-clock').hidden = true;
  $('export').disabled = true;
  renderComposer(); renderError(); renderNotice(); renderInspector(true); renderSessions();
}

function applySnapshot(snapshot) {
  if (!snapshot || snapshot.id !== state.activeId) return;
  // A completed ACK is authoritative over a GET that was already in flight.
  if (snapshot.pending_turn && state.acknowledged.has(snapshot.pending_turn.id)) return;
  state.snapshot = snapshot;
  const pending = snapshot.pending_turn;
  if (pending?.status === 'failed' && !state.error) {
    state.busy = false;
    state.error = { message: typeof pending.error === 'string' ? pending.error : 'This turn could not finish. Retry to continue the same turn.', action: 'turn' };
  } else if (pending?.status === 'processing') state.busy = true;
  const local = storageGet(pendingKey(snapshot.id));
  if (local && pending?.id && !local.turn_id) {
    local.turn_id = pending.id;
    storageSet(pendingKey(snapshot.id), local);
  }
  renderMessages(); renderSessionHeader(); renderComposer(); renderError(); renderNotice(); renderInspector();
  if (pending?.reply && pending.status === 'awaiting_delivery') acknowledgeRenderedReply(snapshot);
  if (pending?.status === 'processing') startPolling();
  if (pending?.status === 'failed' && !state.sending.has(snapshot.id)) stopPolling();
}

function renderSessionHeader() {
  const session = state.snapshot?.state;
  const label = $('session-status');
  label.className = 'session-status';
  if (!session) return;
  if (session.state === 'closed') label.textContent = 'Interview ended';
  else if (session.state === 'paused_callback') { label.textContent = 'Paused'; label.classList.add('is-paused'); }
  else if (session.state === 'roleplay') { label.textContent = 'Roleplay'; label.classList.add('is-live'); }
  else { label.textContent = 'In progress'; label.classList.add('is-live'); }
  $('export').disabled = !state.snapshot;
  updateClock();
}

function updateClock() {
  const session = state.snapshot?.state;
  if (!session?.started_at) { $('session-clock').hidden = true; return; }
  const start = parseTime(session.started_at).getTime();
  const end = session.state === 'closed' ? parseTime(state.snapshot.updated_at).getTime() : session.state === 'paused_callback' && session.paused_at ? parseTime(session.paused_at).getTime() : Date.now();
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  $('session-clock').textContent = `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
  $('session-clock').title = 'Time since interview started';
  $('session-clock').hidden = false;
}

function renderMessages() {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  $('welcome').hidden = true;
  $('messages').hidden = false;
  const messages = [...(snapshot.messages ?? [])];
  const pending = snapshot.pending_turn;
  if (pending?.reply && !messages.some((m) => m.role === 'assistant' && m.turn_id === pending.id)) {
    messages.push({ id: `pending-${pending.id}`, role: 'assistant', content: pending.reply, turn_id: pending.id, created_at: snapshot.updated_at, delivered: false });
  }
  const local = storageGet(pendingKey(snapshot.id));
  if (local && !messages.some((m) => m.role === 'candidate' || m.role === 'user' ? m.turn_id === local.turn_id || (m.content === local.message && messages.indexOf(m) === messages.findLastIndex((r) => r.role === 'candidate' || r.role === 'user')) : false)) {
    messages.push({ id: `local-${local.request_id}`, role: 'candidate', content: local.message, created_at: local.started_at, local: true });
  }
  const signature = JSON.stringify(messages.map((m) => [m.id, m.content, m.delivered, m.local]));
  if (signature !== state.messageSignature) {
    state.messageSignature = signature;
    const scroll = $('chat-scroll');
    const nearBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 130;
    const container = $('messages');
    const fragment = document.createDocumentFragment();
    for (const message of messages) {
      const candidate = ['candidate', 'user'].includes(message.role);
      const entry = node('article', `message ${candidate ? 'candidate' : 'assistant'}`);
      entry.dataset.messageId = message.id ?? '';
      const avatar = node('div', `message-avatar${candidate ? '' : ' bot-mark'}`, candidate ? 'Y' : undefined);
      avatar.setAttribute('aria-hidden', 'true');
      const body = node('div', 'message-body');
      const meta = node('div', 'message-meta');
      meta.append(node('strong', 'message-name', candidate ? 'You' : 'OnlyRound'));
      if (!candidate) meta.append(node('span', 'message-tag', 'Interview agent'));
      meta.append(node('time', 'message-time', timeLabel(message.created_at)));
      body.append(meta, node('p', 'message-text', message.content));
      if (message.local) body.append(node('span', 'message-pending', state.error ? 'Kept on this device · ready to retry' : 'Sending securely…'));
      entry.append(avatar, body); fragment.append(entry);
    }
    container.replaceChildren(fragment);
    if (nearBottom || messages.length < 3 || state.busy) requestAnimationFrame(() => { scroll.scrollTop = scroll.scrollHeight; });
  }
  const processing = pending?.status === 'processing' || (state.busy && !pending?.reply);
  $('processing').hidden = !processing;
  const events = snapshot.events ?? [];
  const running = events.findLast((event, index) => eventIsRunning(event, index, events));
  $('processing-label').textContent = running ? `${nice(running.name)}…` : 'Preparing the next question';
}

async function acknowledgeRenderedReply(snapshot) {
  const turn = snapshot.pending_turn;
  if (!turn?.reply || state.acknowledging.has(turn.id) || state.acknowledged.has(turn.id)) return;
  state.acknowledging.add(turn.id);
  const sessionId = snapshot.id;
  try {
    // Two animation frames ensure the reply has been painted before delivery is committed.
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (state.activeId !== sessionId) return;
    const committed = await post(`/api/sessions/${sessionId}/turns/${turn.id}/ack`);
    state.acknowledged.add(turn.id);
    const local = storageGet(pendingKey(sessionId));
    if (local && (!local.turn_id || local.turn_id === turn.id)) storageRemove(pendingKey(sessionId));
    if (state.activeId === sessionId) {
      state.busy = false; state.error = null;
      stopPolling();
      applySnapshot(committed);
      refreshSessions().catch(() => {});
    }
  } catch (error) {
    if (state.activeId === sessionId && error.status !== 401) {
      state.busy = false;
      state.error = { message: 'The reply is visible, but delivery could not be confirmed. Retry to continue safely.', action: 'ack' };
      renderError(); renderComposer();
      stopPolling();
    }
  } finally { state.acknowledging.delete(turn.id); }
}

function renderComposer() {
  const session = state.snapshot?.state;
  const pending = state.snapshot?.pending_turn;
  const blocked = !session || state.busy || Boolean(state.error) || Boolean(pending && ['processing', 'awaiting_delivery'].includes(pending.status)) || session.state === 'closed' || !state.bootstrap?.model_ready;
  $('message-input').disabled = blocked;
  $('send-message').disabled = blocked || !$('message-input').value.trim();
  $('message-input').placeholder = !session ? 'Start an interview to join the conversation…' : session.state === 'closed' ? 'This interview has ended.' : !state.bootstrap?.model_ready ? 'Waiting for the model connection…' : state.busy ? 'The agent is preparing your next question…' : state.error ? 'Retry your saved turn to continue…' : session.state === 'paused_callback' ? 'Send a message to resume your interview…' : 'Write your response…';
  $('composer-hint').replaceChildren(node('span', 'tiny-lock', '◈'), document.createTextNode(session?.state === 'closed' ? 'Record complete · available to export' : 'Your words stay linked to your answers'));
}

function renderError() {
  $('turn-error').hidden = !state.error;
  if (!state.error) return;
  $('turn-error-title').textContent = state.error.action === 'ack' ? 'Your reply is here. Let’s confirm delivery.' : state.error.action === 'load' ? 'We couldn’t open this interview.' : 'Your turn can be safely retried.';
  $('turn-error-message').textContent = state.error.message;
  $('retry-turn').textContent = state.error.action === 'load' ? 'Reload' : 'Retry';
  $('retry-turn').disabled = state.busy;
}

async function submitMessage(event) {
  event.preventDefault();
  const message = $('message-input').value.trim();
  if (!message || state.busy || !state.snapshot || $('message-input').disabled) return;
  const existing = storageGet(pendingKey(state.activeId));
  if (existing) {
    toast('Finishing the existing turn before sending another response.');
    await sendTurn(existing); return;
  }
  const pending = { session_id: state.activeId, request_id: uuid(), message, started_at: new Date().toISOString() };
  if (!storageSet(pendingKey(state.activeId), pending)) {
    toast('Allow browser storage before sending so your turn can be recovered safely.'); return;
  }
  $('message-input').value = '';
  $('message-input').style.height = 'auto';
  await sendTurn(pending);
}

async function sendTurn(pending) {
  const sessionId = pending.session_id;
  if (state.sending.has(sessionId)) return;
  state.sending.add(sessionId);
  if (state.activeId === sessionId) {
    state.busy = true; state.error = null; state.messageSignature = '';
    renderMessages(); renderError(); renderComposer(); startPolling();
  }
  try {
    const result = await post(`/api/sessions/${sessionId}/turns`, { request_id: pending.request_id, message: pending.message });
    pending.turn_id = result.turn_id;
    if (result.status === 'delivered') storageRemove(pendingKey(sessionId));
    else if (!state.acknowledged.has(result.turn_id)) storageSet(pendingKey(sessionId), pending);
    if (state.activeId !== sessionId) return;
    if (result.status === 'delivered' || state.acknowledged.has(result.turn_id)) {
      state.busy = false; state.error = null; stopPolling();
    }
    if (result.snapshot) applySnapshot(result.snapshot);
    else await pollOnce();
  } catch (error) {
    if (state.activeId !== sessionId || error.status === 401) return;
    if (error.status === 409 && /processing|progress|pending|delivery/i.test(error.message)) {
      await pollOnce(); startPolling();
    } else {
      // The server might have completed after the browser lost its response.
      const recovered = await api(`/api/sessions/${sessionId}`).catch(() => null);
      if (recovered?.pending_turn?.status === 'awaiting_delivery' || recovered?.pending_turn?.status === 'processing') {
        applySnapshot(recovered);
      } else if (state.activeId === sessionId) {
        if (recovered) applySnapshot(recovered);
        state.busy = false;
        state.error = { message: errorMessage(error), action: 'turn' };
        stopPolling(); renderError(); renderComposer();
      }
    }
  } finally { state.sending.delete(sessionId); }
}

async function retryCurrent() {
  const action = state.error?.action;
  state.error = null; renderError();
  if (action === 'load') {
    const id = state.activeId;
    state.activeId = null;
    await openSession(id);
  } else if (action === 'ack' && state.snapshot?.pending_turn?.reply) {
    state.busy = true; renderComposer();
    await acknowledgeRenderedReply(state.snapshot);
  } else {
    const pending = storageGet(pendingKey(state.activeId));
    if (pending) await sendTurn(pending);
    else {
      await pollOnce();
      if (state.snapshot?.pending_turn?.status === 'failed') {
        toast('Reopen this interview in the browser that sent its last turn to retry it.');
      }
    }
  }
}

function startPolling() {
  if (state.polling || !state.activeId) return;
  state.polling = setInterval(pollOnce, 750);
}
function stopPolling() {
  clearInterval(state.polling); state.polling = null;
}
async function pollOnce() {
  if (state.pollingBusy || !state.activeId || !$('login-screen').hidden) return;
  const sessionId = state.activeId;
  state.pollingBusy = true;
  try {
    const snapshot = await api(`/api/sessions/${sessionId}`);
    if (sessionId === state.activeId) applySnapshot(snapshot);
  } catch (error) {
    if (error.status === 401) stopPolling();
    // Transient polling failures must not create a second turn.
  } finally { state.pollingBusy = false; }
}

function renderInspector(force = false) {
  const snapshot = state.snapshot;
  const signature = JSON.stringify([snapshot?.id, snapshot?.answer_sheet, snapshot?.state?.current_criterion_id, snapshot?.events, snapshot?.history, snapshot?.candidate_questions, state.bootstrap?.job]);
  if (!force && signature === state.inspectorSignature) return;
  state.inspectorSignature = signature;
  renderAnswers(); renderHarness(); renderHistory();
}

function criterionConfig(id) {
  return (state.snapshot?.job?.criteria ?? state.bootstrap?.job?.criteria ?? []).find((c) => c.id === id);
}
function renderAnswers() {
  const criteria = state.snapshot?.job?.criteria ?? state.bootstrap?.job?.criteria ?? [];
  const rows = state.snapshot?.answer_sheet ?? [];
  const byId = new Map(rows.map((row) => [row.criterion_id ?? row.id, row]));
  const captured = rows.filter((row) => ['complete', 'conditional', 'declined', 'needs_confirmation', 'partial', 'unclear', 'unclear_final'].includes(row.status) && Boolean(row.quote || row.value)).length;
  $('answer-progress').replaceChildren(document.createTextNode(String(captured)), node('span', '', `/ ${criteria.length} captured`));
  const percent = criteria.length ? Math.round(captured / criteria.length * 100) : 0;
  $('progress-ring').style.setProperty('--progress', `${percent}%`);
  $('progress-fraction').textContent = `${percent}%`;
  const list = $('answer-sheet'); list.replaceChildren();
  for (const [index, criterion] of criteria.entries()) {
    const row = byId.get(criterion.id) ?? { status: 'not_asked' };
    const current = state.snapshot?.state?.current_criterion_id === criterion.id && state.snapshot?.state?.state !== 'closed';
    const card = node('article', `answer-card${current ? ' current' : ''}`);
    const top = node('div', 'answer-card-top');
    const name = node('h3', 'answer-name');
    name.append(node('span', 'criterion-number', String(index + 1).padStart(2, '0')), document.createTextNode(criterion.name));
    const statusLabel = current && ['not_asked', 'asked'].includes(row.status) ? 'Asking now' : ({ not_asked: 'Not asked yet', complete: 'Captured', needs_confirmation: 'To confirm', off_target: 'Re-asking', unclear_final: 'Unclear', skipped_time: 'Time limit', asked: 'Asked' }[row.status] ?? nice(row.status));
    const statusStyle = row.status === 'complete' ? ' captured' : ['partial', 'conditional', 'needs_confirmation', 'unclear', 'unclear_final'].includes(row.status) ? ' attention' : current ? ' current' : '';
    top.append(name, node('span', `status-pill${statusStyle}`, statusLabel));
    card.append(top, node('p', `answer-value${row.value ? '' : ' empty'}`, row.value || (current ? 'Listening for the candidate’s answer…' : ['unasked', 'skipped_time'].includes(row.status) ? 'No answer collected.' : 'The conversation will fill this in.')));
    if (row.quote) card.append(node('blockquote', 'answer-quote', `“${row.quote}”`));
    if (row.condition) card.append(node('p', 'answer-condition', `Condition: ${row.condition}`));
    if (row.quote || row.obtained_via || row.followups_used || row.corrected) {
      const metadata = node('div', 'answer-metadata');
      if (row.obtained_via) metadata.append(node('span', '', nice(row.obtained_via)));
      if (row.corrected) metadata.append(node('span', '', 'Corrected'));
      if (row.confirmed) metadata.append(node('span', '', 'Confirmed'));
      if (row.implied) metadata.append(node('span', '', 'Implied'));
      if (row.followups_used) metadata.append(node('span', '', `${row.followups_used} follow-up${row.followups_used === 1 ? '' : 's'}`));
      if (metadata.childElementCount) card.append(metadata);
    }
    list.append(card);
  }
  if (!criteria.length) list.append(emptyCard('Your answer sheet', 'The configured interview criteria will appear here.'));
  const questions = $('candidate-questions'); questions.replaceChildren();
  const questionRows = state.snapshot?.candidate_questions ?? [];
  if (questionRows.length) {
    const section = node('section', 'question-section');
    section.append(node('h3', '', 'QUESTIONS FROM THE CANDIDATE'));
    for (const question of questionRows) {
      const card = node('div', 'question-card', question.text);
      const unknown = !question.fact_key || question.fact_key === 'unknown';
      card.append(node('span', unknown ? 'unknown' : '', unknown ? 'Recruiter to confirm' : `Source: ${nice(question.fact_key)}`));
      section.append(card);
    }
    questions.append(section);
  }
}

function emptyCard(title, description) {
  const card = node('div', 'empty-inspector');
  card.append(node('strong', '', title), document.createTextNode(description));
  return card;
}
function harnessMap() {
  const list = node('div', 'harness-map');
  list.append(node('p', 'harness-map-title', 'THE CONVERSATION LOOP'));
  const harnesses = state.bootstrap?.harnesses ?? [];
  harnesses.forEach((harness, index) => {
    const step = node('div', 'harness-map-step');
    const body = node('div');
    body.append(node('strong', '', harness.name), node('p', '', harness.description));
    step.append(node('span', '', String(index + 1).padStart(2, '0')), body); list.append(step);
  });
  return list;
}

function renderHarness() {
  const list = $('harness-stack');
  const scroller = document.querySelector('.inspector-content');
  const scrollPosition = scroller.scrollTop;
  // Preserve the user's expanded calls while polling updates their status and output.
  for (const detail of list.querySelectorAll('details[data-event-id]')) {
    if (detail.open) state.expandedEvents.add(detail.dataset.eventId);
    else state.expandedEvents.delete(detail.dataset.eventId);
  }
  list.replaceChildren();
  const events = state.snapshot?.events ?? [];
  $('event-count').textContent = String(events.length);
  if (!events.length) {
    list.append(emptyCard('Waiting for the first turn', 'Actual model calls and code steps will appear here as the interview runs.'), harnessMap());
    return;
  }
  const turnGroups = new Map();
  for (const event of events) {
    const key = event.turn_id ?? 'session';
    if (!turnGroups.has(key)) turnGroups.set(key, []);
    turnGroups.get(key).push(event);
  }
  const entries = [...turnGroups.entries()];
  for (let i = entries.length - 1; i >= 0; i--) {
    const [turnId, turnEvents] = entries[i];
    const divider = node('div', 'turn-divider');
    divider.append(node('span', '', turnId === 'session' ? 'SESSION ACTIVITY' : `TURN ${String(i + 1).padStart(2, '0')}`), node('span', '', timeLabel(turnEvents[0].created_at)));
    list.append(divider);
    for (const [eventIndex, event] of turnEvents.entries()) {
      const running = eventIsRunning(event, eventIndex, turnEvents);
      const failed = ['failed', 'error'].includes(event.status) || Boolean(event.error);
      const detail = node('details', `event-card${running ? ' is-running' : ''}${failed ? ' is-error' : ''}`);
      detail.dataset.eventId = String(event.id);
      detail.open = state.expandedEvents.has(String(event.id));
      detail.addEventListener('toggle', () => {
        if (detail.open) state.expandedEvents.add(String(event.id));
        else state.expandedEvents.delete(String(event.id));
      });
      const summary = node('summary', 'event-summary');
      const symbol = failed ? '!' : running ? '···' : event.status === 'waiting' ? '◷' : event.status === 'started' ? '↳' : event.kind === 'model' || /model|understand|speak|customer/.test(event.name) ? '✧' : '✓';
      summary.append(node('span', 'event-icon', symbol));
      const heading = node('div', 'event-heading');
      heading.append(node('strong', '', nice(event.name)));
      const meta = node('div', 'event-meta');
      meta.append(node('span', '', nice(event.kind ?? 'Code step')), node('span', '', '·'), node('span', '', nice(event.status ?? 'complete')));
      if (event.duration_ms !== null && event.duration_ms !== undefined) meta.append(node('span', 'event-duration', event.duration_ms >= 1000 ? `${(event.duration_ms / 1000).toFixed(2)} s` : `${Math.round(event.duration_ms)} ms`));
      heading.append(meta); summary.append(heading, node('span', 'event-chevron', '⌄'));
      const body = node('div', 'event-details');
      if (event.input !== undefined && event.input !== null) body.append(payload('INPUT / PROMPT', event.input));
      if (event.output !== undefined && event.output !== null) body.append(payload('OUTPUT / RESPONSE', event.output));
      else if (running) body.append(node('p', 'event-detail-label', 'Waiting for the result…'));
      if (event.error) body.append(payload('ERROR', event.error, true));
      body.append(node('p', 'event-identifier', `Event ${event.id}`));
      detail.append(summary, body); list.append(detail);
    }
  }
  scroller.scrollTop = scrollPosition;
}

function payload(label, value, error = false) {
  const group = node('div');
  const title = node('div', 'event-detail-label');
  const copy = node('button', 'copy-button', 'Copy'); copy.type = 'button';
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(textValue(value)); copy.textContent = 'Copied'; setTimeout(() => { copy.textContent = 'Copy'; }, 1600); }
    catch { toast('Select the text to copy it from this browser.'); }
  });
  title.append(node('span', '', label), copy);
  group.append(title, node('pre', `event-payload${error ? ' error' : ''}`, textValue(value)));
  return group;
}

function renderHistory() {
  const list = $('history-list'); list.replaceChildren();
  const history = state.snapshot?.history ?? [];
  if (!history.length) {
    list.append(emptyCard('A clean slate', 'When an answer is proposed, its accepted change or rejection will be recorded here.'));
    return;
  }
  for (const change of [...history].reverse()) {
    const entry = node('article', `history-item${change.accepted ? '' : ' rejected'}`);
    const heading = node('div', 'history-item-heading');
    heading.append(node('strong', '', criterionConfig(change.criterion_id)?.name ?? nice(change.criterion_id ?? 'Answer')), node('span', 'history-event', `${change.accepted ? 'Accepted' : 'Rejected'} · ${nice(change.event)}`));
    entry.append(heading);
    if (change.accepted) {
      const oldValue = typeof change.old_value === 'object' ? change.old_value?.value : change.old_value;
      const newValue = typeof change.new_value === 'object' ? change.new_value?.value : change.new_value;
      entry.append(node('p', 'history-change', oldValue ? `${textValue(oldValue)} → ${textValue(newValue) || 'Updated'}` : textValue(newValue) || 'Answer updated'));
    }
    if (change.reason) entry.append(node('p', change.accepted ? 'history-change' : 'history-reason', change.reason));
    if (change.quote) entry.append(node('blockquote', '', `“${change.quote}”`));
    if (change.message_id) entry.append(node('p', 'history-message-id', `Message ${change.message_id.slice(0, 8)}`));
    list.append(entry);
  }
}

function selectTab(name) {
  state.activeTab = name;
  for (const tab of document.querySelectorAll('[data-tab]')) {
    const selected = tab.dataset.tab === name;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    $(`panel-${tab.dataset.tab}`).hidden = !selected;
  }
}

function openDialog(type) {
  const content = $('dialog-content'); content.replaceChildren();
  if (type === 'job') {
    const job = state.snapshot?.job ?? state.bootstrap?.job ?? {};
    $('dialog-eyebrow').textContent = 'DEMONSTRATION JOB CONFIGURATION';
    content.append(node('h2', '', job.title ?? 'Screening interview'), node('p', '', `${job.company ?? 'Demo employer'} · ${job.location ?? 'Demo location'}`));
    if (job.description) content.append(node('p', '', job.description));
    content.append(node('p', 'dialog-notice', 'This workspace currently uses a demonstration job. Questions and job facts are configured for this sample role.'));
    content.append(node('h3', '', 'What the conversation covers'));
    const list = node('ul');
    for (const criterion of job.criteria ?? []) list.append(node('li', '', criterion.name));
    content.append(list);
    if (job.facts?.length) {
      content.append(node('h3', '', 'Facts the agent can share'));
      const facts = node('ul');
      for (const fact of job.facts) {
        const item = node('li'); item.append(node('strong', '', `${fact.topic ?? nice(fact.key)}: `), document.createTextNode(fact.text ?? ''));
        facts.append(item);
      }
      content.append(facts);
    }
  } else {
    $('dialog-eyebrow').textContent = 'HOW ONLYROUND WORKS';
    content.append(node('h2', '', 'A conversation you can follow.'), node('p', '', 'OnlyRound conducts a structured first screening interview. The model interprets the candidate’s message and writes a short acknowledgement. Code validates the proposed answers, maintains the record, and chooses the next approved question.'), node('p', '', 'The answer sheet keeps the candidate’s own supporting words. Live harness shows actual model calls and code steps, including their inputs, outputs, and timing. History retains both accepted changes and rejected proposals.'), harnessMap(), node('p', 'dialog-notice', 'This version covers the bot and its tool interaction. It does not score candidates, produce hiring judgments, or run evaluations.'));
  }
  $('detail-dialog').showModal();
}

function closeMobileMenu() { $('sidebar').classList.remove('is-open'); $('mobile-menu').setAttribute('aria-expanded', 'false'); }
$('new-session').addEventListener('click', createSession);
$('welcome-start').addEventListener('click', createSession);
$('composer-form').addEventListener('submit', submitMessage);
$('message-input').addEventListener('input', () => {
  const field = $('message-input'); field.style.height = 'auto'; field.style.height = `${Math.min(field.scrollHeight, 150)}px`;
  renderComposer();
});
$('message-input').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer-form').requestSubmit(); }
});
$('retry-turn').addEventListener('click', retryCurrent);
$('export').addEventListener('click', () => {
  if (!state.activeId) return;
  const link = node('a'); link.href = `/api/sessions/${encodeURIComponent(state.activeId)}/export`;
  link.download = `onlyround-${state.activeId}.json`; document.body.append(link); link.click(); link.remove();
});
for (const tab of document.querySelectorAll('[data-tab]')) {
  tab.addEventListener('click', () => selectTab(tab.dataset.tab));
  tab.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const tabs = [...document.querySelectorAll('[data-tab]')];
    const index = tabs.indexOf(tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    selectTab(tabs[next].dataset.tab); tabs[next].focus();
  });
}
$('about-trigger').addEventListener('click', () => openDialog('about'));
$('job-details').addEventListener('click', () => openDialog('job'));
$('close-dialog').addEventListener('click', () => $('detail-dialog').close());
$('detail-dialog').addEventListener('click', (event) => { if (event.target === $('detail-dialog')) $('detail-dialog').close(); });
$('mobile-menu').setAttribute('aria-expanded', 'false');
$('mobile-menu').addEventListener('click', () => {
  const open = $('sidebar').classList.toggle('is-open'); $('mobile-menu').setAttribute('aria-expanded', String(open));
});
document.addEventListener('click', (event) => {
  if ($('sidebar').classList.contains('is-open') && !$('sidebar').contains(event.target) && !$('mobile-menu').contains(event.target)) closeMobileMenu();
});
$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (state.loginBusy) return;
  state.loginBusy = true; $('login-submit').disabled = true; $('login-error').hidden = true;
  try {
    await post('/api/login', { access_code: $('access-code').value });
    $('access-code').value = '';
    hideLogin();
    await boot();
  } catch (error) {
    $('login-error').textContent = error.status === 401 || error.status === 403 ? 'That access code wasn’t accepted. Please try again.' : errorMessage(error);
    $('login-error').hidden = false;
  } finally { state.loginBusy = false; $('login-submit').disabled = false; }
});
$('logout').addEventListener('click', async () => {
  try {
    await post('/api/logout'); stopPolling(); state.loadVersion++; state.snapshot = null; state.activeId = null; state.sessions = [];
    storageRemove('activeSession'); renderEmpty(); showLogin();
  } catch (error) { toast(errorMessage(error)); }
});
window.addEventListener('online', () => { if (state.activeId && state.busy) { pollOnce(); startPolling(); } });
window.addEventListener('storage', (event) => {
  if (state.activeId && event.key === STORAGE_PREFIX + pendingKey(state.activeId)) {
    const pending = storageGet(pendingKey(state.activeId));
    state.busy = Boolean(pending);
    renderComposer();
    pollOnce();
    if (pending) startPolling();
  }
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && state.activeId && $('login-screen').hidden) pollOnce();
});
setInterval(updateClock, 1000);
selectTab('answers');
boot();
