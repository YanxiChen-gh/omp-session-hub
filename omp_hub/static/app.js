'use strict';

const $ = (id) => document.getElementById(id);
const state = { sessions: [], inventory: null, refreshing: false, inventoryLoading: false, creating: false, jobId: null, result: null, signature: null, sessionActions: new Map(), discoveryVersion: 0, refreshQueued: false, inventoryQueued: false };
const text = (value, fallback = 'Unknown') => typeof value === 'string' && value.trim() ? value : fallback;

async function api(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-OMP-Hub': '1' },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: 'no-store', signal: controller.signal,
    });
    let data;
    try { data = await response.json(); } catch {
      const error = new Error('The hub returned an unreadable response.');
      error.status = response.status;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(text(data?.error, `The request failed (${response.status}).`));
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') {
      const timeout = new Error('The hub took too long to respond. Check your connection and try again.');
      timeout.timedOut = true;
      throw timeout;
    }
    throw error;
  } finally { clearTimeout(timer); }
}

function showError(id, message) {
  $(id).textContent = message || '';
  $(id).hidden = !message;
}
function element(tag, content, className) {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (className) node.className = className;
  return node;
}
function canOpen(host) {
  return typeof host.instanceId === 'string' && host.instanceId.length > 0 && Number.isInteger(host.generation);
}
function canClose(host) {
  return canOpen(host) && /^[a-z0-9-]{8,64}$/.test(host.instanceId) && host.access === 'control'
    && Number.isSafeInteger(host.generation) && Number.isSafeInteger(host.pid) && host.pid > 0
    && typeof host.sessionId === 'string' && host.sessionId.trim().length > 0;
}
function updateSessionButtons(card) {
  const pending = state.sessionActions.get(card.dataset.instanceId);
  for (const button of card.querySelectorAll('[data-session-action]')) {
    button.disabled = button.dataset.available !== 'true' || Boolean(pending);
    if (pending === button.dataset.sessionAction) button.setAttribute('aria-busy', 'true');
    else button.removeAttribute('aria-busy');
  }
}
function setSessionAction(instanceId, action) {
  if (action) state.sessionActions.set(instanceId, action);
  else state.sessionActions.delete(instanceId);
  for (const card of $('session-list').children) {
    if (card.dataset.instanceId === instanceId) updateSessionButtons(card);
  }
}
function openButton(host) {
  const button = element('button', 'Open session');
  button.type = 'button';
  button.dataset.sessionAction = 'open';
  button.dataset.available = String(canOpen(host));
  button.setAttribute('aria-label', `Open ${text(host.sessionName, 'OMP session')}`);
  button.addEventListener('click', async () => {
    if (!canOpen(host) || state.sessionActions.has(host.instanceId)) return;
    setSessionAction(host.instanceId, 'open');
    showError('session-action-error', '');
    $('session-action-status').textContent = `Opening "${text(host.sessionName, 'OMP session')}"...`;
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(host.instanceId)}/open`, { generation: host.generation });
      if (typeof data.url !== 'string') throw new Error('The hub returned no session link.');
      const url = new URL(data.url);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('The hub returned an invalid session link.');
      setSessionAction(host.instanceId, null);
      window.location.assign(url.href);
    } catch (error) {
      $('session-action-status').textContent = '';
      showError('session-action-error', `Could not open the session: ${error.message}`);
      setSessionAction(host.instanceId, null);
    }
  });
  return button;
}
function closeButton(host) {
  const button = element('button', 'Close session', 'secondary danger');
  button.type = 'button';
  button.dataset.sessionAction = 'close';
  button.dataset.available = String(canClose(host));
  button.setAttribute('aria-label', `Close ${text(host.sessionName, 'OMP session')}`);
  button.addEventListener('click', () => closeSession(host));
  return button;
}
async function closeSession(host) {
  if (!canClose(host) || state.sessionActions.has(host.instanceId)) return;
  const selection = { instanceId: host.instanceId, generation: host.generation, title: text(host.sessionName, 'Untitled OMP session'), busy: host.busy === true };
  const restoreFocus = document.activeElement?.dataset.sessionAction === 'close'
    && document.activeElement.closest('.session-card')?.dataset.instanceId === selection.instanceId;
  setSessionAction(selection.instanceId, 'close');
  showError('session-action-error', '');
  $('session-action-status').textContent = `Preparing to close "${selection.title}"...`;
  let closeRequested = false;
  try {
    const preview = await api(`/api/sessions/${encodeURIComponent(selection.instanceId)}/close-preview`, { generation: selection.generation });
    const target = preview?.target;
    const keys = ['paneId', 'terminalId', 'tabId', 'workspaceId', 'closesTab', 'closesWorkspace'];
    if (!target || typeof target !== 'object' || Array.isArray(target)
      || Object.keys(target).sort().join(',') !== [...keys].sort().join(',')
      || !keys.slice(0, 4).every((key) => typeof target[key] === 'string' && target[key].trim())
      || typeof target.closesTab !== 'boolean' || typeof target.closesWorkspace !== 'boolean') {
      throw new Error('The hub returned an invalid close preview.');
    }
    const confirmedTarget = Object.freeze({ ...target });
    const message = [
      `Close "${selection.title}"?`,
      `This stops the live OMP agent and closes its Herdr pane (${confirmedTarget.paneId}). Saved session history is kept.`,
      selection.busy ? 'Warning: this session is working. Closing it interrupts the current work.' : '',
      confirmedTarget.closesTab ? 'This is the last pane in its tab, so the tab will also close.' : '',
      confirmedTarget.closesWorkspace ? 'This is the last pane in its workspace, so the workspace will also close.' : '',
      'Choose OK to close the session, or Cancel to leave it open.',
    ].filter(Boolean).join('\n\n');
    if (!window.confirm(message)) {
      $('session-action-status').textContent = 'Close canceled. No close request was sent.';
      return;
    }
    $('session-action-status').textContent = `Closing "${selection.title}"...`;
    closeRequested = true;
    const result = await api(`/api/sessions/${encodeURIComponent(selection.instanceId)}/close`, {
      generation: selection.generation, target: confirmedTarget, confirmed: true,
    });
    if (result?.closed !== true || result.paneId !== confirmedTarget.paneId) throw new Error('The hub did not confirm that the selected pane closed.');
    state.discoveryVersion += 1;
    state.sessions = state.sessions.filter((item) => item.instanceId !== selection.instanceId || item.generation !== selection.generation);
    renderSessions();
    $('session-action-status').textContent = `Closed "${selection.title}". Saved session history is kept.`;
    await Promise.all([refreshSessions(false, true), loadInventory(true)]);
  } catch (error) {
    $('session-action-status').textContent = '';
    const detail = error.timedOut ? 'The hub took too long to respond.' : error.message;
    const outcome = !closeRequested ? 'No close request was sent.'
      : [400, 403, 404, 409].includes(error.status) ? 'The close request was refused.'
        : 'The close outcome is unknown; the request may have reached the pane.';
    showError('session-action-error', `Could not complete closing "${selection.title}": ${detail} ${outcome} Refresh sessions and inspect Herdr before another close attempt. Do not retry blindly.`);
  } finally {
    setSessionAction(selection.instanceId, null);
    if (restoreFocus && document.activeElement === document.body) {
      const card = [...$('session-list').children].find((item) => item.dataset.instanceId === selection.instanceId);
      (card?.querySelector('[data-session-action="close"]') || $('refresh')).focus();
    }
  }
}
function renderSessions() {
  const signature = JSON.stringify(state.sessions);
  if (signature === state.signature) return;
  state.signature = signature;
  const fragment = document.createDocumentFragment();
  for (const host of state.sessions) {
    const card = element('li', undefined, 'session-card');
    card.setAttribute('data-instance-id', host.instanceId);
    card.append(element('h3', text(host.sessionName, 'Untitled OMP session')));
    const cwd = text(host.cwd, 'Directory unavailable');
    const repo = typeof host.cwd === 'string' ? host.cwd.replace(/\/+$/, '').split('/').pop() : '';
    card.append(element('p', repo || 'Repository unknown'), element('p', cwd, 'path'));
    const model = host.model && typeof host.model === 'object'
      ? [text(host.model.provider, ''), text(host.model.id, '')].filter(Boolean).join(' / ')
      : text(host.model, '');
    card.append(element('p', `Model: ${model || 'Unknown'}`, 'muted'));
    const badges = element('div', undefined, 'badges');
    const activity = host.inputRequired === true ? 'Input required' : host.busy === true ? 'Working' : host.busy === false ? 'Idle' : 'Activity unknown';
    const relay = host.relayConnected === true ? 'Relay connected' : host.relayConnected === false ? 'Relay disconnected' : 'Relay unknown';
    badges.append(element('span', activity, 'badge'), element('span', relay, 'badge'));
    if (typeof host.access === 'string') badges.append(element('span', `Access: ${host.access}`, 'badge'));
    card.append(badges, openButton(host), closeButton(host));
    if (!canClose(host)) card.append(element('p', host.access === 'view'
      ? 'View-only sessions cannot be closed here.'
      : 'Closing requires control access and valid live session details.', 'hint'));
    updateSessionButtons(card);
    fragment.append(card);
  }
  $('session-list').replaceChildren(fragment);
}
async function refreshSessions(manual = false, afterCurrent = false) {
  if (state.refreshing) {
    if (afterCurrent) state.refreshQueued = true;
    return;
  }
  state.refreshing = true;
  const version = state.discoveryVersion;
  $('refresh').disabled = true;
  $('session-list').setAttribute('aria-busy', 'true');
  if (manual) $('session-status').textContent = 'Refreshing sessions...';
  try {
    const data = await api('/api/sessions');
    if (version !== state.discoveryVersion) return;
    if (!Array.isArray(data.sessions)) throw new Error('The hub returned an invalid session list.');
    state.sessions = data.sessions.filter((host) => host && typeof host === 'object' && !Array.isArray(host));
    renderSessions();
    const message = state.sessions.length ? `${state.sessions.length} active session${state.sessions.length === 1 ? '' : 's'}. Updates automatically.` : 'No active sessions. Create a workspace below, or start OMP on your computer.';
    if ($('session-status').textContent !== message) $('session-status').textContent = message;
    showError('session-error', '');
  } catch (error) {
    if (version !== state.discoveryVersion) return;
    $('session-status').textContent = state.sessions.length ? 'Showing the last received sessions. They may be out of date.' : 'Sessions unavailable.';
    showError('session-error', `${error.message} Use Refresh to try again.`);
  } finally {
    state.refreshing = false;
    $('refresh').disabled = false;
    $('session-list').removeAttribute('aria-busy');
    if (state.refreshQueued) {
      state.refreshQueued = false;
      refreshSessions();
    }
  }
}
function fillOptions(id, values, placeholder) {
  const select = $(id);
  const selected = select.value;
  const options = [new Option(placeholder, '')];
  for (const item of values) {
    if (typeof item.id !== 'string') continue;
    options.push(new Option(`${text(item.name, item.id)}${item.cwd || item.path ? ` (${text(item.cwd || item.path)})` : ''}`, item.id));
  }
  select.replaceChildren(...options);
  if (options.some((option) => option.value === selected)) select.value = selected;
}
async function loadInventory(afterCurrent = false) {
  if (state.inventoryLoading) {
    if (afterCurrent === true) state.inventoryQueued = true;
    return;
  }
  state.inventoryLoading = true;
  const version = state.discoveryVersion;
  $('reload-inventory').disabled = true;
  $('inventory-status').textContent = 'Loading repositories and workspaces...';
  try {
    const data = await api('/api/inventory');
    if (version !== state.discoveryVersion) return;
    if (!Array.isArray(data.repositories) || !Array.isArray(data.workspaces)) throw new Error('The hub returned invalid inventory.');
    state.inventory = data;
    fillOptions('repository', data.repositories.filter((item) => item && typeof item === 'object'), 'Choose a repository');
    fillOptions('workspace', data.workspaces.filter((item) => item && typeof item === 'object'), 'Choose a workspace');
    $('inventory-status').textContent = `${data.repositories.length} repositories, ${data.workspaces.length} workspaces available.`;
    showError('inventory-error', '');
  } catch (error) {
    if (version !== state.discoveryVersion) return;
    $('inventory-status').textContent = state.inventory ? 'Using the last received inventory.' : 'Inventory unavailable. Local path and GitHub clone are still available.';
    showError('inventory-error', error.message);
  } finally {
    state.inventoryLoading = false;
    $('reload-inventory').disabled = false;
    if (state.inventoryQueued) {
      state.inventoryQueued = false;
      loadInventory();
    }
  }
}
function updateMode() {
  const mode = $('mode').value;
  for (const group of document.querySelectorAll('[data-mode]')) {
    group.hidden = group.dataset.mode !== mode;
    for (const control of group.querySelectorAll('input, select')) control.disabled = group.hidden || state.creating;
  }
  const existing = mode === 'workspace';
  $('workspace-options').hidden = existing;
  for (const control of $('workspace-options').querySelectorAll('input, select')) control.disabled = existing || state.creating;
  const shell = !existing && $('session-kind').value === 'shell';
  $('prompt-field').hidden = shell;
  $('prompt').disabled = shell || state.creating;
  $('submit-create').textContent = existing ? 'Create session' : 'Create workspace';
}
function setCreating(creating) {
  state.creating = creating;
  $('create-form').setAttribute('aria-busy', String(creating));
  for (const control of $('create-form').querySelectorAll('input, select, textarea, button[type=submit]')) control.disabled = creating;
  updateMode();
}
function creationSpec() {
  const mode = $('mode').value;
  const spec = { checkout: $('checkout').value, agent: $('session-kind').value === 'agent', editor: $('editor').checked, prompt: $('prompt').disabled ? '' : $('prompt').value };
  if (mode === 'workspace') {
    spec.workspaceId = $('workspace').value;
    spec.agent = true;
    spec.editor = false;
    spec.checkout = 'primary';
  } else if (mode === 'clone') {
    spec.cloneRepository = $('clone-repository').value.trim();
    if ($('clone-root').value.trim()) spec.cloneRoot = $('clone-root').value.trim();
  } else spec.repository = mode === 'local' ? $('local-path').value.trim() : $('repository').value;
  return spec;
}
function renderResult() {
  if (!state.result) return;
  const result = state.result;
  const box = $('create-result');
  box.hidden = false;
  box.replaceChildren(element('strong', 'Workspace ready'));
  if (typeof result.cwd === 'string') box.append(element('p', result.cwd));
  box.append(element('p', 'Your workspace is ready. Open its session from Active sessions when it appears. Shell-only workspaces have no session to open.'));
  const refresh = element('button', 'Refresh active sessions', 'secondary');
  refresh.type = 'button';
  refresh.addEventListener('click', () => refreshSessions(true));
  box.append(refresh);
}
async function pollJob() {
  if (!state.jobId) return;
  $('resume-job').hidden = true;
  setCreating(true);
  try {
    const data = await api(`/api/jobs/${encodeURIComponent(state.jobId)}`);
    if (data.status === 'preparing') {
      $('create-status').textContent = 'Preparing your workspace. This may take a little while...';
      setTimeout(pollJob, 1500);
      return;
    }
    if (data.status === 'failed') {
      state.jobId = null;
      showError('create-error', text(data.error, 'Workspace creation failed.'));
      $('create-status').textContent = 'Creation failed. Your form values are preserved; you can adjust them and try again.';
      setCreating(false);
      return;
    }
    if (data.status !== 'ready') throw new Error('The hub returned an unknown creation state.');
    state.jobId = null;
    state.result = data.result && typeof data.result === 'object' ? data.result : {};
    $('create-status').textContent = 'Creation complete.';
    showError('create-error', '');
    renderResult();
    setCreating(false);
    await refreshSessions();
  } catch (error) {
    if (error.status === 404) {
      state.jobId = null;
      showError('create-error', 'This creation job is no longer available. It may have completed before its status expired. Check Active sessions and your workspaces before creating again.');
      $('create-status').textContent = 'Creation status unavailable. Your form values are preserved.';
      setCreating(false);
      return;
    }
    showError('create-error', error.message);
    $('create-status').textContent = 'Creation status could not be completed. Your form values are preserved.';
    $('resume-job').hidden = false;
    setCreating(true);
  }
}
$('create-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.creating) return;
  const spec = creationSpec();
  state.result = null;
  $('create-result').hidden = true;
  showError('create-error', '');
  $('create-status').textContent = 'Requesting workspace creation...';
  setCreating(true);
  try {
    const data = await api('/api/workspaces', spec);
    if (typeof data.jobId !== 'string' || !data.jobId) throw new Error('The hub returned no creation job identifier.');
    state.jobId = data.jobId;
    await pollJob();
  } catch (error) {
    showError('create-error', `${error.message} The request may have reached the hub. Check Active sessions before submitting again.`);
    $('create-status').textContent = 'Your form values are preserved.';
    setCreating(false);
  }
});
$('mode').addEventListener('change', updateMode);
$('session-kind').addEventListener('change', updateMode);
$('refresh').addEventListener('click', () => refreshSessions(true));
$('reload-inventory').addEventListener('click', loadInventory);
$('resume-job').addEventListener('click', pollJob);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshSessions(); });
setInterval(() => { if (!document.hidden) refreshSessions(); }, 5000);
updateMode();
loadInventory();
refreshSessions();
