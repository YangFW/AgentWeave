const PREFERENCE_PREFIX = 'agentnexus-';
const LEGACY_PREFERENCE_PREFIXES = ['zhishu-'];

function readPreference(name) {
  const value = localStorage.getItem(`${PREFERENCE_PREFIX}${name}`);
  if (value !== null) return value;
  for (const prefix of LEGACY_PREFERENCE_PREFIXES) {
    const legacy = localStorage.getItem(`${prefix}${name}`);
    if (legacy !== null) {
      localStorage.setItem(`${PREFERENCE_PREFIX}${name}`, legacy);
      return legacy;
    }
  }
  return null;
}

function writePreference(name, value) {
  localStorage.setItem(`${PREFERENCE_PREFIX}${name}`, String(value));
}

const state = {
  pendingSubmission: null,
  currentUser: null,
  adminUsers: [],
  adminSelectedUser: null,
  authenticated: false,
  authEnabled: false,
  skills: [],
  mcp: [],
  agents: [],
  tasks: [],
  models: [],
  executionEngines: [],
  loops: [],
  workspaces: [],
  memories: [],
  knowledgeBases: [],
  knowledgeDocuments: [],
  knowledgeSearchResults: [],
  diagnostics: null,
  diagnosticSelfTestFilter: readPreference('diagnostics-self-test-filter') || 'all',
  diagnosticSelfTestCategory: readPreference('diagnostics-self-test-category') || 'all',
  diagnosticSelfTestArtifact: readPreference('diagnostics-self-test-artifact') || 'all',
  conversationSummaries: [],
  artifacts: [],
  expertTemplates: [],
  expertInstallations: [],
  expertTeams: [],
  expertTeamRuns: [],
  loopTriggerEvents: [],
  loopNotifications: [],
  loopEditorDirty: false,
  loopStateDirty: false,
  capabilities: {},
  marketplace: { skills: [], mcp_servers: [] },
  marketplaceFocusId: '',
  modelTestResults: {},
  uploads: [],
  selectedSkill: null,
  selectedSkillFile: null,
  skillFiles: [],
  selectedMcp: null,
  selectedAgent: null,
  selectedModel: null,
  selectedExecutionEngine: null,
  selectedWorkspace: null,
  selectedLoop: null,
  selectedMemory: null,
  selectedKnowledgeBase: null,
  selectedConversationSummary: null,
  selectedArtifact: null,
  selectedExpertTemplate: null,
  selectedExpertTeam: null,
  selectedExpertRun: null,
  workbenchMode: readPreference('workbench-mode') === 'expert' ? 'expert' : 'agent',
  currentExpertSelection: null,
  expertRunPollTimer: null,
  expertRunPollToken: 0,
  loopPollTimer: null,
  currentTask: null,
  taskRuntime: null,
  runtimeTaskId: null,
  runtimeTimer: null,
  eventSource: null,
  streamTaskId: null,
  streamCursor: 0,
  streamGeneration: 0,
  streamRetryTimer: null,
  streamRetryCount: 0,
  seenEventIds: new Set(),
  taskUiRunning: false,
  taskUiCancelRequested: false,
  taskUiTaskId: null,
  taskUiStatusNode: null,
  // Public, user-facing execution summaries shown inside the conversation.
  // These are structured runtime events only; model reasoning is never
  // copied into this state.
  agentThinkingCards: new Map(),
  pptxConfiguration: null,
  conversationId: readPreference('conversation') || createConversationId(),
  workspaceId: readPreference('workspace') || 'default',
};

const DIAGNOSTIC_CAPABILITY_ENTRIES = [
  { label: '模型配置', checkId: 'models.capability', entry: '模型设置', tab: 'models' },
  { label: '文件上传', checkId: 'file.upload_context', entry: '工作台 → 添加附件', tab: 'chat' },
  { label: '文档输出', checkId: 'document.output_formats', entry: '工作台或产物页', tab: 'artifacts' },
  { label: 'Skill/MCP', checkId: 'skill_mcp.capability', entry: '技能中心 / 工具接入 / 市场', tab: 'marketplace' },
  { label: '知识库', checkId: 'knowledge.capability', entry: '知识库', tab: 'knowledge' },
  { label: '联网与远程', checkId: 'network.capability', entry: '工具接入 / 环境变量开关', tab: 'mcp' },
  { label: '上下文记忆', checkId: 'memory.capability', entry: '记忆', tab: 'memory' },
  { label: '专家模式', checkId: 'expert.capability', entry: '专家团 / 工作台模式切换', tab: 'experts' },
  { label: '自动化', checkId: 'automation.capability', entry: '自动化', tab: 'loops' },
  { label: '执行闭环', checkId: 'runtime.contract_capability', entry: '工作台运行控制', tab: 'chat' },
  { label: '权限安全', checkId: 'security.permissions', entry: '自检 / 模型设置 / 工具接入', tab: 'diagnostics' },
];

const $ = (id) => document.getElementById(id);

function createConversationId() {
  const value = globalThis.crypto?.randomUUID?.().replaceAll('-', '') || `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `conv_${value.slice(0, 24)}`;
}

async function api(path, options = {}) {
  const isForm = options.body instanceof FormData;
  const res = await fetch(path, {
    ...options,
    headers: { ...(isForm ? {} : { 'Content-Type': 'application/json' }), ...(options.headers || {}) },
  });
  if (!res.ok) {
    if (res.status === 401 && state.authenticated) {
      state.authenticated = false;
      document.body.classList.add('auth-pending');
      state.eventSource?.close();
      location.reload();
    }
    const text = await res.text();
    let message = text || res.statusText;
    try {
      const parsed = JSON.parse(text);
      message = parsed.detail || parsed.message || message;
    } catch (_) {}
    const error = new Error(message);
    error.status = res.status;
    error.path = path;
    throw error;
  }
  if (res.status === 204) return null;
  return res.json();
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m]));
}

function agentIconSvg(agent = {}) {
  const id = String(agent.id || agent.agent_id || '');
  const name = String(agent.name || agent.agent_name || '');
  if (id === 'general-agent') {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3.5 7.2 4.2v8.6L12 20.5l-7.2-4.2V7.7Z"/><circle cx="12" cy="12" r="2.7"/><path d="M12 9.3V6.5M9.7 13.4l-2.5 1.5M14.3 13.4l2.5 1.5"/></svg>';
  }
  if (/expert|专家/.test(`${id} ${name}`)) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="8" r="2.7"/><circle cx="17" cy="9" r="2.3"/><path d="M3.8 18.5c.5-2.9 1.9-4.5 4.2-4.5s3.7 1.6 4.2 4.5M13.3 18c.4-2.3 1.6-3.7 3.7-3.7s3.3 1.4 3.7 3.7"/></svg>';
  }
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 4 1.8 4.2L18 10l-4.2 1.8L12 16l-1.8-4.2L6 10l4.2-1.8Z"/><path d="m18.5 15 .8 1.8 1.7.7-1.7.8-.8 1.7-.7-1.7-1.8-.8 1.8-.7Z"/></svg>';
}

function agentAvatarTone(agent = {}) {
  const id = String(agent.id || agent.agent_id || '');
  const name = String(agent.name || agent.agent_name || '');
  if (id === 'general-agent') return 'core';
  if (/expert|专家/.test(`${id} ${name}`)) return 'expert';
  return 'custom';
}

function formatJson(value) {
  return JSON.stringify(value, null, 2);
}

function currentWorkspaceId() {
  return state.workspaceId || 'default';
}

function platformScopeValues() {
  return {
    organization_id: 'local-org',
    workspace_id: currentWorkspaceId(),
    user_id: 'local-user',
  };
}

function workspaceQuery(extra = {}) {
  return new URLSearchParams({ organization_id: 'local-org', user_id: 'local-user', ...extra }).toString();
}

let toastTimer;
function notify(message, type = 'success') {
  const toast = $('toast');
  toast.textContent = message;
  toast.className = `toast ${type} show`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = 'toast'; }, 3200);
}

function setSendButtonState(mode = 'idle') {
  const button = $('sendBtn');
  if (!button) return;
  button.classList.toggle('stop-action', mode !== 'idle');
  button.disabled = mode === 'cancel_requested';
  if (mode === 'running') {
    button.innerHTML = '<span class="action-label">停止</span><span class="action-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="1.5"/></svg></span>';
    button.setAttribute('aria-label', '停止当前任务');
    button.title = '停止当前任务';
  } else if (mode === 'cancel_requested') {
    button.innerHTML = '<span class="action-label">停止中…</span><span class="action-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="7"/></svg></span>';
    button.setAttribute('aria-label', '正在停止当前任务');
    button.title = '正在停止当前任务';
  } else {
    button.innerHTML = '发送 <span class="action-icon"><svg viewBox="0 0 24 24"><path d="m6 12 6-6 6 6M12 6v12"/></svg></span>';
    button.setAttribute('aria-label', '发送消息');
    button.title = '发送消息';
  }
}

function clearTaskUiStatus() {
  if (state.taskUiStatusNode?.isConnected) state.taskUiStatusNode.remove();
  state.taskUiStatusNode = null;
  state.taskUiTaskId = null;
}

function setTaskUiStatus(taskId, label, kind = 'thinking') {
  if (!taskId || (taskId !== '__pending__' && state.currentTask?.id !== taskId)) return;
  let node = state.taskUiStatusNode;
  if (!node || !node.isConnected || state.taskUiTaskId !== taskId) {
    clearTaskUiStatus();
    node = document.createElement('div');
    node.className = 'conversation-status';
    node.dataset.taskStatus = String(taskId);
    $('conversation').appendChild(node);
    state.taskUiStatusNode = node;
    state.taskUiTaskId = taskId;
  }
  node.className = `conversation-status ${escapeHtml(kind)}`;
  node.innerHTML = `<span class="status-pulse" aria-hidden="true"></span><span>${escapeHtml(label)}</span>`;
  const thinking = agentThinkingCard(taskId);
  if (thinking) {
    node.classList.add('agent-thinking-legacy-status');
    setAgentThinkingCurrent(taskId, label, thinking.current?.detail || '', 'progress', kind === 'verifying' ? 'verification' : kind === 'outputting' ? 'output' : kind === 'error' ? 'error' : 'process');
  } else {
    node.classList.remove('agent-thinking-legacy-status');
  }
  $('conversation').appendChild(node);
  $('conversation').scrollTop = $('conversation').scrollHeight;
}

function finishTaskUi(taskId, status = 'completed') {
  if (taskId && state.taskUiTaskId && state.taskUiTaskId !== taskId) return;
  state.taskUiRunning = false;
  state.taskUiCancelRequested = false;
  setSendButtonState('idle');
  finishAgentThinkingCard(taskId, status);
  const labels = {
    completed: ['已完成', 'done'], succeeded: ['已完成', 'done'],
    cancelled: ['已停止', 'done'], failed: ['执行失败', 'error'],
    submission_failed: ['发送失败', 'error'],
    waiting_approval: ['等待你的确认', 'verifying'], waiting_input: ['等待补充信息', 'verifying'],
  };
  const [label, kind] = labels[status] || ['已结束', 'done'];
  if (state.taskUiTaskId && state.currentTask?.id === state.taskUiTaskId) {
    setTaskUiStatus(state.taskUiTaskId, label, kind);
  } else if (state.taskUiStatusNode?.isConnected) {
    state.taskUiStatusNode.className = `conversation-status ${kind}`;
    state.taskUiStatusNode.innerHTML = `<span class="status-pulse" aria-hidden="true"></span><span>${escapeHtml(label)}</span>`;
  }
  if (status === 'failed') {
    if ($('taskOverviewSection')) $('taskOverviewSection').open = true;
    if ($('runtimeInspectorSection')) $('runtimeInspectorSection').open = true;
    if ($('timelineSection')) $('timelineSection').open = true;
    if ($('timelineStatus')) $('timelineStatus').textContent = '执行失败 · 点击追踪';
  } else if (['completed', 'succeeded'].includes(status)) {
    // 成功交付后恢复为简洁视图；用户仍可点击“查看”展开完整追踪。
    if ($('taskOverviewSection')) $('taskOverviewSection').open = false;
    if ($('runtimeInspectorSection')) $('runtimeInspectorSection').open = false;
    if ($('timelineSection')) $('timelineSection').open = false;
    if ($('taskOverviewStatus')) $('taskOverviewStatus').textContent = '已完成 · 点击查看';
    if ($('runtimeInspectorStatus')) $('runtimeInspectorStatus').textContent = '已完成 · 点击追踪';
    if ($('timelineStatus')) $('timelineStatus').textContent = '执行完成 · 点击追踪';
  }
}

function finishSubmissionFailure(message, { notifyUser = true } = {}) {
  const detail = String(message || '模型配置不可用，请检查模型设置后重试。').trim();
  const statusNodePresent = Boolean(state.taskUiStatusNode?.isConnected);
  const taskId = state.taskUiTaskId || '__pending__';
  finishTaskUi(taskId, 'submission_failed');
  // A stale page can have lost the temporary status node while the POST is
  // still in flight.  Always create a terminal status in that case so the UI
  // can never remain visually stuck at “正在提交任务…”.
  if (!statusNodePresent) setTaskUiStatus('__pending__', '发送失败', 'error');
  addMessage('agent', `发送失败：${detail}`);
  if (notifyUser) notify(`发送失败：${detail}`, 'error');
}

const agentThinkingStatusLabels = {
  pending: '等待中', running: '执行中', completed: '已完成', succeeded: '已完成',
  failed: '未完成', cancelled: '已停止', waiting_approval: '等待确认',
  waiting_input: '等待补充信息',
};

function thinkingIconSvg(kind = 'process') {
  const icons = {
    goal: '<circle cx="12" cy="12" r="7.2"/><circle cx="12" cy="12" r="2.4"/><path d="M12 2.8v2M12 19.2v2M2.8 12h2M19.2 12h2"/>',
    plan: '<path d="M5 5h5M5 12h8M5 19h11"/><circle cx="17.5" cy="5" r="2"/><circle cx="19.5" cy="12" r="2"/><circle cx="21" cy="19" r="2"/>',
    skill: '<path d="m8.5 4.5 3.5 3.5-4 4-3.5-3.5 4-4Z"/><path d="m14.8 11.4 4.7 4.7a2 2 0 0 1-2.8 2.8L12 14.2"/><path d="M5 19h5"/>',
    mcp: '<path d="m14.5 5.5 4 4M13 4l2-2 5 5-2 2M4 20l4.8-1 9.3-9.3-4-4L4.8 15 4 20Z"/><path d="m3 21 4-4"/>',
    tool: '<path d="m14.8 5.2 4-2.2 2.2 2.2-2.2 4-2.5-1.5-5.7 5.7"/><path d="m9.5 14.5-5.8 5.8a1.5 1.5 0 0 1-2.1-2.1l5.8-5.8"/><path d="m14 10 3 3"/>',
    model: '<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M9 9h6v6H9zM9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    agent: '<circle cx="8" cy="8" r="2.6"/><circle cx="17" cy="9" r="2.2"/><path d="M3.8 18.5c.5-2.8 1.9-4.4 4.2-4.4s3.7 1.6 4.2 4.4M13.3 18c.4-2.2 1.6-3.6 3.7-3.6s3.3 1.4 3.7 3.6"/>',
    expert: '<circle cx="8" cy="8" r="2.6"/><circle cx="17" cy="8" r="2.2"/><path d="M3.8 18.5c.5-2.8 1.9-4.4 4.2-4.4s3.7 1.6 4.2 4.4M13.3 18c.4-2.2 1.6-3.6 3.7-3.6s3.3 1.4 3.7 3.6"/><path d="m12 4 .8 1.7 1.7.8-1.7.8L12 9l-.8-1.7-1.7-.8 1.7-.8Z"/>',
    knowledge: '<path d="M5 4.5A2.5 2.5 0 0 1 7.5 2H19v17H7.5A2.5 2.5 0 0 0 5 21.5v-17Z"/><path d="M5 4.5v17M9 6h6M9 9h7"/>',
    verification: '<path d="M12 3 19 6v5c0 4.2-2.8 7.6-7 9-4.2-1.4-7-4.8-7-9V6l7-3Z"/><path d="m8.5 12 2.2 2.2 4.8-5"/>',
    output: '<path d="M5 3.5h9l5 5V20H5z"/><path d="M14 3.5V9h5M8 13h8M8 16h6"/>',
    error: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v6M12 16.5v.2"/>',
    process: '<path d="M12 3.5 19 7v10l-7 3.5L5 17V7l7-3.5Z"/><circle cx="12" cy="12" r="2.4"/><path d="M12 9.6V7M9.9 13.2l-2.1 1.2M14.1 13.2l2.1 1.2"/>',
  };
  return `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[kind] || icons.process}</svg>`;
}

function agentThinkingStatusLabel(status) {
  return agentThinkingStatusLabels[status] || '处理中';
}

function agentThinkingCard(taskId) {
  return state.agentThinkingCards?.get(String(taskId)) || null;
}

function thinkingStatusFromEvent(type) {
  if (['error', 'tool_error'].includes(type)) return 'failed';
  if (['approval_required'].includes(type)) return 'waiting_approval';
  if (['clarification'].includes(type)) return 'waiting_input';
  if (['done'].includes(type)) return 'completed';
  if (['cancelled'].includes(type)) return 'cancelled';
  return 'running';
}

function thinkingIconForEvent(type, kind = '') {
  if (kind) return kind;
  const mapping = {
    intent: 'goal', goal_spec: 'goal', goal_spec_progress: 'goal', plan: 'plan', plan_progress: 'plan',
    skill: 'skill', model: 'model', tool_call: 'mcp', tool_result: 'mcp', tool_error: 'mcp',
    tool_blocked: 'tool', agent: 'agent', expert_selection: 'expert', knowledge: 'knowledge',
    memory: 'knowledge', verification_started: 'verification', verification_result: 'verification',
    output_check: 'verification', answer: 'output', error: 'error', clarification: 'goal',
  };
  return mapping[type] || 'process';
}

function formatThinkingDuration(startedAt, finishedAt = Date.now()) {
  if (!startedAt) return '';
  const seconds = Math.max(0, (finishedAt - startedAt) / 1000);
  if (seconds < 1) return '不到 1 秒';
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

function ensureThinkingStep(item, id, title, kind = 'process', status = 'pending') {
  const key = String(id || `step-${item.steps.size + 1}`);
  let step = item.steps.get(key);
  if (!step) {
    step = { id: key, title: String(title || key), kind, status, detail: '', children: new Map() };
    item.steps.set(key, step);
  } else {
    if (title) step.title = String(title);
    if (kind) step.kind = kind;
  }
  if (status) step.status = status;
  return step;
}

function ensureThinkingChild(step, id, title, kind = 'detail', status = 'pending') {
  const key = String(id || `child-${step.children.size + 1}`);
  let child = step.children.get(key);
  if (!child) {
    child = { id: key, title: String(title || key), kind, status, detail: '' };
    step.children.set(key, child);
  } else {
    if (title) child.title = String(title);
    if (kind) child.kind = kind;
  }
  if (status) child.status = status;
  return child;
}

function addThinkingRole(item, key, kind, label, detail = '') {
  const roleKey = String(key || `${kind}:${label}`);
  const existing = item.roles.get(roleKey);
  if (existing) {
    if (detail) existing.detail = String(detail);
    return existing;
  }
  const role = { key: roleKey, kind: kind || 'process', label: String(label || '执行角色'), detail: String(detail || '') };
  item.roles.set(roleKey, role);
  return role;
}

function thinkingRoleMarkup(role) {
  const detail = role.detail ? ` · ${escapeHtml(role.detail)}` : '';
  return `<span class="agent-thinking-role ${escapeHtml(role.kind)}"><span class="agent-thinking-role-icon">${thinkingIconSvg(role.kind)}</span><span><strong>${escapeHtml(role.label)}</strong>${detail ? `<small>${detail}</small>` : ''}</span></span>`;
}

function thinkingActivityMarkup(activity) {
  const detail = activity.detail ? `<small>${escapeHtml(activity.detail)}</small>` : '';
  const statusIcon = activity.status === 'completed' ? '✓' : activity.status === 'failed' ? '!' : activity.status === 'waiting' ? '…' : '·';
  return `<div class="agent-thinking-activity ${escapeHtml(activity.status)}" data-depth="${activity.depth || 0}" data-thinking-activity="${escapeHtml(activity.id)}">
    <span class="agent-thinking-activity-icon">${thinkingIconSvg(activity.kind || 'process')}</span>
    <span class="agent-thinking-activity-copy"><strong>${escapeHtml(activity.label)}</strong>${detail}</span>
    <span class="agent-thinking-activity-status" aria-label="${escapeHtml(agentThinkingStatusLabel(activity.status))}">${statusIcon}</span>
  </div>`;
}

function thinkingActivityText(value, fallback = '') {
  const text = String(value || fallback || '').replace(/\s+/g, ' ').trim();
  if (/^(prepare|understand|execute|validate|start|done)$/i.test(text)) return '';
  return text.length > 140 ? `${text.slice(0, 137)}…` : text;
}

function addThinkingActivity(item, { key = '', type = 'progress', kind = 'process', label = '', detail = '', status = 'running', depth = 0 } = {}) {
  if (!item) return;
  const activityId = String(key || `${type}:${item.activities.length + 1}`);
  const safeLabel = thinkingActivityText(label, '正在处理…');
  const safeDetail = thinkingActivityText(detail);
  let activity = item.activityIndex.get(activityId);
  if (!activity) {
    activity = { id: activityId, type, kind, label: safeLabel, detail: safeDetail, status, depth };
    item.activityIndex.set(activityId, activity);
    item.activities.push(activity);
  } else {
    activity.type = type || activity.type;
    activity.kind = kind || activity.kind;
    activity.label = safeLabel || activity.label;
    activity.detail = safeDetail || activity.detail;
    activity.status = status || activity.status;
    activity.depth = depth;
  }
  // Keep a generous history for an expanded card, while preventing a noisy
  // stream (answer_delta and repeated progress events are coalesced by key).
  if (item.activities.length > 80) {
    const removed = item.activities.splice(0, item.activities.length - 80);
    removed.forEach((entry) => item.activityIndex.delete(entry.id));
  }
}

function recordThinkingActivity(item, type, event = {}, data = {}) {
  // These events are useful to the runtime inspector, but are bookkeeping
  // noise in the conversation. Keep the feed focused on user-relevant work.
  if (new Set(['checkpoint', 'permissions', 'notice', 'recovery', 'recovery_scheduled', 'resume', 'resume_scheduled', 'retry_scheduled', 'command_queued']).has(type)) return;
  const content = event.content || '';
  const toolName = data.tool_name || data.name || '';
  const server = data.server_id || data.server || '';
  const toolLabel = `${server ? `${server}.` : ''}${toolName || '工具'}`;
  const childLabel = data.child_title || data.child_id || data.node_id || '';
  const friendlyChildLabel = childLabel && !/^[a-z0-9_-]+$/i.test(String(childLabel)) ? childLabel : '';
  const status = String(data.status || '').toLowerCase();
  const isFailed = ['error', 'tool_error', 'tool_blocked'].includes(type) || status === 'failed';
  const isDone = ['tool_result', 'tool_reused', 'verification_result', 'output_check', 'answer', 'done'].includes(type) || ['completed', 'succeeded'].includes(status);
  const defaultStatus = isFailed ? 'failed' : isDone ? 'completed' : type === 'approval_required' || type === 'clarification' ? 'waiting' : 'running';
  const eventKey = type === 'plan_progress' || type === 'progress'
    ? `${type}:${data.node_id || 'node'}:${data.child_id || ''}:${status || 'running'}`
    : event.id ? `${type}:${event.id}` : `${type}:${data.call_id || toolLabel || childLabel || item.activities.length}`;
  let activity;
  switch (type) {
    case 'start':
      activity = { key: eventKey, kind: 'process', label: '开始处理任务', detail: content, status: 'running' };
      break;
    case 'intent': case 'goal_spec': case 'goal_spec_progress':
      activity = { key: eventKey, kind: 'goal', label: '正在确认任务目标', detail: content || item.goal, status: status === 'resolving' ? 'running' : 'completed' };
      break;
    case 'plan':
      activity = { key: eventKey, kind: 'plan', label: '已生成执行计划', detail: `${Array.isArray(data.plan?.nodes) ? data.plan.nodes.length : 0} 个执行节点`, status: 'completed' };
      break;
    case 'plan_progress': case 'progress':
      {
        const progressLabel = thinkingActivityText(content) || (status === 'failed' ? `执行未完成${friendlyChildLabel ? ` · ${friendlyChildLabel}` : ''}` : status === 'completed' || status === 'succeeded' ? `已完成${friendlyChildLabel ? ` · ${friendlyChildLabel}` : '当前步骤'}` : `正在执行${friendlyChildLabel ? ` · ${friendlyChildLabel}` : '当前步骤'}`);
        activity = { key: eventKey, kind: 'plan', label: progressLabel, detail: friendlyChildLabel && progressLabel !== friendlyChildLabel ? friendlyChildLabel : '', status: defaultStatus };
      }
      break;
    case 'expert_selection':
      activity = { key: eventKey, kind: 'expert', label: `已匹配专家团 · ${data.team_name || data.team_id || '专家团'}`, detail: data.reason || content, status: 'completed' };
      break;
    case 'agent':
      activity = { key: eventKey, kind: 'agent', label: `已选择智能体 · ${data.agent?.name || data.agent?.id || content || '智能体'}`, detail: content, status: 'completed' };
      break;
    case 'skill':
      activity = { key: eventKey, kind: 'skill', label: '已匹配 Skill', detail: (data.skills || []).map((skill) => skill.name || skill.id).filter(Boolean).join('、') || content, status: 'completed' };
      break;
    case 'model':
      activity = { key: eventKey, kind: 'model', label: `正在调用模型 · ${data.model_name || data.model_id || content || '当前模型'}`, detail: content, status: defaultStatus };
      break;
    case 'tool_call': case 'tool_reused':
      activity = { key: eventKey, kind: 'mcp', label: `正在调用 · ${toolLabel}`, detail: content, status: 'running', depth: 1 };
      break;
    case 'tool_result':
      activity = { key: eventKey, kind: 'mcp', label: `已返回 · ${toolLabel}`, detail: content, status: 'completed', depth: 1 };
      break;
    case 'tool_error': case 'tool_blocked':
      activity = { key: eventKey, kind: 'tool', label: `${type === 'tool_blocked' ? '已阻止' : '调用未完成'} · ${toolLabel}`, detail: content, status: 'failed', depth: 1 };
      break;
    case 'knowledge': case 'memory':
      activity = { key: eventKey, kind: 'knowledge', label: type === 'knowledge' ? '已检索项目资料' : '已应用上下文记忆', detail: content, status: 'completed' };
      break;
    case 'verification_started':
      activity = { key: eventKey, kind: 'verification', label: '正在检查结果', detail: content, status: 'running' };
      break;
    case 'verification_result': case 'output_check':
      activity = { key: eventKey, kind: 'verification', label: '结果检查完成', detail: content, status: 'completed' };
      break;
    case 'answer_delta':
      activity = { key: 'answer:stream', kind: 'output', label: '正在输出结果…', detail: `${item.answerChars} 字`, status: 'running' };
      break;
    case 'answer':
      {
        let label = '已生成最终答复';
        if (data.engine && data.engine !== 'builtin') {
          const engName = data.engine.toUpperCase();
          label = `${engName} 沙箱已生成答复`;
          if (data.session_id) {
            label += ` · 会话: ${data.session_id.slice(0, 8)}`;
          }
          if (data.resumed_session) {
            label += ' (上下文已延续)';
          }
        }
        activity = { key: eventKey, kind: 'output', label, detail: content, status: 'completed' };
      }
      break;
    case 'approval_required': case 'clarification':
      activity = { key: eventKey, kind: 'goal', label: type === 'approval_required' ? '等待你的确认' : '需要补充信息', detail: content || '补充后将继续当前任务', status: 'waiting' };
      break;
    case 'error':
      activity = { key: eventKey, kind: 'error', label: event.title || '任务执行失败', detail: content, status: 'failed' };
      break;
    case 'done':
      activity = { key: eventKey, kind: 'output', label: '任务已完成', detail: content || '结果已发布', status: 'completed' };
      break;
    default:
      activity = { key: eventKey, kind: thinkingIconForEvent(type), label: event.title || content || '正在处理…', detail: content, status: defaultStatus };
  }
  addThinkingActivity(item, activity);
}

function renderAgentThinkingCard(item, { full = true } = {}) {
  if (!item?.node?.isConnected) return;
  const node = item.node;
  node.className = `agent-thinking-card ${escapeHtml(item.status)}${item.historical ? ' historical' : ''}`;
  const title = item.historical && item.status === 'pending' ? '查看处理过程' : item.status === 'running' ? '正在处理' : item.status === 'waiting_approval' ? '等待你的确认' : item.status === 'waiting_input' ? '需要补充信息' : item.status === 'failed' ? '处理未完成' : item.status === 'cancelled' ? '任务已停止' : '处理完成';
  const currentLabel = item.current?.label || (item.status === 'running' ? '正在准备任务…' : agentThinkingStatusLabel(item.status));
  const currentDetail = thinkingActivityText(item.current?.detail) || '公开执行过程会在这里实时更新';
  const badge = item.historical && item.status === 'pending' ? '按需查看' : item.status === 'running' ? '实时' : item.status === 'completed' || item.status === 'succeeded' ? '已完成' : agentThinkingStatusLabel(item.status);
  const steps = [...item.steps.values()];
  const completed = steps.filter((step) => ['completed', 'succeeded'].includes(step.status)).length;
  const capabilityCount = [...item.roles.values()].filter((role) => ['skill', 'mcp', 'tool', 'model'].includes(role.kind)).length;
  const duration = item.finishedAt ? formatThinkingDuration(item.startedAt, item.finishedAt) : formatThinkingDuration(item.startedAt);
  const stats = `${item.activities.length || completed}/${item.activities.length || steps.length || 0} 条活动${capabilityCount ? ` · ${capabilityCount} 项能力` : ''}${duration ? ` · ${duration}` : ''}`;
  const summaryIcon = thinkingIconForEvent(item.current?.type || '', item.current?.kind || 'process');
  const currentIcon = thinkingIconForEvent(item.current?.type || '', item.current?.kind || 'process');
  const roles = [...item.roles.values()].filter((role, index, all) => all.findIndex((candidate) => `${candidate.kind}:${candidate.label}` === `${role.kind}:${role.label}`) === index);
  const rolesMarkup = roles.slice(0, 10).map(thinkingRoleMarkup).join('');
  const activitiesMarkup = item.activities.length ? item.activities.map(thinkingActivityMarkup).join('') : '<div class="agent-thinking-empty">活动记录会随任务推进显示</div>';
  const runtimeId = item.taskId && item.taskId !== '__pending__' ? item.taskId : '';
  const body = node.querySelector('.agent-thinking-body');
  const summaryTitle = node.querySelector('[data-thinking-title]');
  const summaryCopy = node.querySelector('[data-thinking-summary]');
  const summaryStatus = node.querySelector('[data-thinking-status]');
  const summaryIconNode = node.querySelector('[data-thinking-summary-icon]');
  if (summaryTitle) summaryTitle.textContent = title;
  if (summaryCopy) summaryCopy.textContent = `${currentLabel}${duration ? ` · ${duration}` : ''}`;
  if (summaryStatus) summaryStatus.textContent = badge;
  if (summaryIconNode) summaryIconNode.innerHTML = thinkingIconSvg(summaryIcon);
  if (!body) return;
  if (full) {
    const current = body.querySelector('[data-thinking-current]');
    if (current) current.innerHTML = `<span class="agent-thinking-current-icon ${escapeHtml(item.status)}">${thinkingIconSvg(currentIcon)}</span><span><strong>${escapeHtml(currentLabel)}</strong><small>${escapeHtml(currentDetail)}</small></span>`;
    const goal = body.querySelector('[data-thinking-goal]');
    if (goal) goal.innerHTML = item.goal ? `<span>目标</span><strong>${escapeHtml(item.goal)}</strong>` : '';
    const roleStrip = body.querySelector('[data-thinking-roles]');
    if (roleStrip) roleStrip.innerHTML = rolesMarkup ? `<div class="agent-thinking-role-strip">${rolesMarkup}</div>` : '';
    const activityList = body.querySelector('[data-thinking-activities]');
    if (activityList) activityList.innerHTML = activitiesMarkup;
    const statNode = body.querySelector('[data-thinking-stats]');
    if (statNode) statNode.textContent = stats;
    const runtimeButton = body.querySelector('[data-open-thinking-runtime]');
    if (runtimeButton) {
      runtimeButton.disabled = !runtimeId;
      runtimeButton.textContent = runtimeId ? '跳转到运行控制' : '任务建立后可查看运行控制';
    }
  }
}

function createAgentThinkingCard(taskId = '__pending__', { historical = false, open = !historical } = {}) {
  const key = String(taskId || '__pending__');
  const existing = agentThinkingCard(key);
  if (existing) {
    existing.historical = existing.historical && historical;
    if (open) existing.node.open = true;
    return existing;
  }
  const node = document.createElement('details');
  node.className = `agent-thinking-card ${historical ? 'historical' : 'running'}`;
  node.dataset.thinkingTask = key;
  node.open = open;
  node.innerHTML = `
    <summary class="agent-thinking-summary">
      <span class="agent-thinking-summary-icon" data-thinking-summary-icon>${thinkingIconSvg('process')}</span>
      <span class="agent-thinking-summary-copy"><strong data-thinking-title>${historical ? '查看智能体处理过程' : '智能体正在处理'}</strong><small data-thinking-summary>${historical ? '点击展开加载目标、计划和能力调用记录' : '正在确认目标与匹配能力…'}</small></span>
      <span class="agent-thinking-status" data-thinking-status>${historical ? '按需查看' : '实时'}</span>
      <span class="agent-thinking-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="agent-thinking-body">
      <div class="agent-thinking-current" data-thinking-current><span class="agent-thinking-current-icon">${thinkingIconSvg('process')}</span><span><strong>正在准备任务…</strong><small>公开执行过程会在这里实时更新</small></span></div>
      <div class="agent-thinking-goal" data-thinking-goal></div>
      <div class="agent-thinking-roles" data-thinking-roles></div>
      <div class="agent-thinking-activities" data-thinking-activities><div class="agent-thinking-empty">活动记录会随任务推进显示</div></div>
      <div class="agent-thinking-footer"><span data-thinking-stats>实时同步中</span><button class="text-button" type="button" data-open-thinking-runtime ${key === '__pending__' ? 'disabled' : ''}>${key === '__pending__' ? '任务建立后可查看运行控制' : '跳转到运行控制'}</button></div>
    </div>`;
  $('conversation').appendChild(node);
  const item = { taskId: key, node, historical, loaded: !historical, loading: false, status: historical ? 'pending' : 'running', startedAt: Date.now(), finishedAt: 0, goal: '', current: { type: 'start', kind: 'process', label: historical ? '点击展开查看执行过程' : '正在确认目标与匹配能力…', detail: historical ? '执行记录按需加载，不展示模型内部思考' : '目标、计划、Skill、MCP 与验收状态会实时同步' }, steps: new Map(), roles: new Map(), activities: [], activityIndex: new Map(), seenEvents: new Set(), eventCount: 0, answerChars: 0 };
  state.agentThinkingCards.set(key, item);
  node.querySelector('[data-open-thinking-runtime]').onclick = (event) => {
    event.preventDefault(); event.stopPropagation(); openRuntimeFromThinking(item.taskId);
  };
  node.addEventListener('toggle', () => {
    if (node.open && item.historical && !item.loaded && !item.loading) loadHistoricalThinking(item.taskId);
  });
  renderAgentThinkingCard(item);
  return item;
}

function bindAgentThinkingCard(pendingTaskId, taskId) {
  const pending = agentThinkingCard(pendingTaskId);
  if (!pending || !taskId || String(pendingTaskId) === String(taskId)) return pending;
  state.agentThinkingCards.delete(String(pendingTaskId));
  pending.taskId = String(taskId);
  pending.node.dataset.thinkingTask = String(taskId);
  pending.historical = false;
  pending.loaded = true;
  state.agentThinkingCards.set(String(taskId), pending);
  renderAgentThinkingCard(pending);
  return pending;
}

function setAgentThinkingCurrent(taskId, label, detail = '', type = 'progress', kind = '') {
  const item = agentThinkingCard(taskId);
  if (!item) return;
  item.current = { label: String(label || '正在处理…'), detail: String(detail || ''), type, kind: thinkingIconForEvent(type, kind) };
  renderAgentThinkingCard(item, { full: true });
}

function updateAgentThinkingPlan(item, plan = {}) {
  const nodes = Array.isArray(plan.nodes) ? plan.nodes : [];
  nodes.forEach((raw, index) => {
    const step = ensureThinkingStep(item, raw.id || `plan-${index + 1}`, raw.title || `执行节点 ${index + 1}`, 'plan', raw.status || 'pending');
    step.detail = raw.detail || step.detail || '';
    (Array.isArray(raw.children) ? raw.children : []).forEach((child) => {
      const childItem = ensureThinkingChild(step, child.id, child.title || child.id, child.kind || 'detail', child.status || 'pending');
      childItem.detail = child.detail || childItem.detail || '';
      if (child.kind) addThinkingRole(item, `${child.kind}:${child.id}`, child.kind === 'mcp' ? 'mcp' : child.kind === 'tool' ? 'tool' : child.kind, child.title || child.id);
    });
  });
}

function updateAgentThinkingEvent(taskId, event = {}) {
  const item = agentThinkingCard(taskId);
  if (!item || !event || privateTaskEventTypes.has(String(event.type || '').toLowerCase())) return;
  const type = String(event.type || '').toLowerCase();
  const id = Number(event.id || 0);
  if (id && item.seenEvents.has(id)) return;
  if (id) item.seenEvents.add(id);
  item.eventCount += 1;
  const data = event.data && typeof event.data === 'object' ? event.data : {};
  const status = thinkingStatusFromEvent(type);
  if (type === 'plan') {
    item.goal = String((data.plan || {}).goal || item.goal || '');
    updateAgentThinkingPlan(item, data.plan || {});
    setAgentThinkingCurrent(taskId, '执行计划已生成', '目标、节点与验收标准已固化', type, 'plan');
  } else if (type === 'intent' || type === 'goal_spec' || type === 'goal_spec_progress') {
    const goalSpec = data.goal_spec || {};
    item.goal = String(goalSpec.objective?.statement || data.objective || event.content || item.goal || '');
    const step = ensureThinkingStep(item, 'understand', '确认当前目标与约束', 'goal', type === 'goal_spec_progress' && data.status === 'resolving' ? 'running' : 'completed');
    step.detail = event.content || step.detail;
    setAgentThinkingCurrent(taskId, '正在确认目标', item.goal || event.content || '结合当前对话确认任务范围', type, 'goal');
  } else if (type === 'plan_progress') {
    const nodeId = data.node_id || 'execute';
    const step = ensureThinkingStep(item, nodeId, data.node_title || data.title || nodeId, 'plan', data.status || 'running');
    step.detail = event.content || step.detail;
    if (data.child_id) {
      const child = ensureThinkingChild(step, data.child_id, data.child_title || data.child_id, data.child_kind || 'detail', data.status || 'running');
      child.detail = event.content || child.detail;
      if (data.child_kind) addThinkingRole(item, `${data.child_kind}:${data.child_id}`, data.child_kind === 'mcp' ? 'mcp' : data.child_kind === 'tool' ? 'tool' : data.child_kind, data.child_title || data.child_id);
    }
    setAgentThinkingCurrent(taskId, event.content || '正在执行计划…', data.child_title || data.node_title || nodeId, type, 'plan');
  } else if (type === 'expert_selection') {
    const team = data.team_name || data.team_id || '专家团';
    addThinkingRole(item, `expert-team:${data.team_id || team}`, 'expert', team, data.selection_mode === 'automatic' ? '自动匹配' : '已指定');
    if (data.supervisor) addThinkingRole(item, `expert:${data.supervisor.agent_id || data.supervisor.agent_name}`, 'expert', data.supervisor.agent_name || data.supervisor.agent_id, '主管');
    (Array.isArray(data.members) ? data.members : []).forEach((member) => addThinkingRole(item, `expert:${member.agent_id || member.agent_name}`, 'expert', member.agent_name || member.agent_id || '专家', member.role || '成员'));
    setAgentThinkingCurrent(taskId, '已选择参与专家', event.content || `${team} 将协作处理当前任务`, type, 'expert');
  } else if (type === 'agent') {
    const agent = data.agent || {};
    addThinkingRole(item, `agent:${agent.id || event.content}`, 'agent', agent.name || event.content || '智能体');
    setAgentThinkingCurrent(taskId, '已选择智能体', agent.name || event.content || '正在执行', type, 'agent');
  } else if (type === 'skill') {
    const skills = Array.isArray(data.skills) ? data.skills : [];
    skills.forEach((skill) => { addThinkingRole(item, `skill:${skill.id || skill.name}`, 'skill', skill.name || skill.id || 'Skill'); const step = ensureThinkingStep(item, 'understand', '目标与能力匹配', 'goal', 'running'); const child = ensureThinkingChild(step, `skill:${skill.id || skill.name}`, skill.name || skill.id || 'Skill', 'skill', 'completed'); child.detail = '已匹配'; });
    setAgentThinkingCurrent(taskId, '已匹配 Skill', event.content || '正在准备执行能力', type, 'skill');
  } else if (['model', 'tool_call', 'tool_result', 'tool_error', 'tool_blocked'].includes(type)) {
    const server = data.server_id || data.server || '';
    const tool = data.tool_name || data.name || '';
    const capabilityKind = type === 'model' ? 'model' : type === 'tool_blocked' ? 'tool' : 'mcp';
    const label = type === 'model' ? (data.model_name || data.model_id || event.content || '模型') : `${server ? `${server}.` : ''}${tool || '工具调用'}`;
    const key = `${capabilityKind}:${data.call_id || server + ':' + tool || event.content}`;
    addThinkingRole(item, key, capabilityKind, label, type === 'tool_result' ? '已返回' : type === 'tool_error' || type === 'tool_blocked' ? '调用未完成' : '执行中');
    const parentId = data.node_id || 'execute';
    const step = ensureThinkingStep(item, parentId, data.node_title || (parentId === 'execute' ? '执行任务' : parentId), 'process', type === 'tool_error' || type === 'tool_blocked' ? 'failed' : type === 'tool_result' ? 'completed' : 'running');
    const child = ensureThinkingChild(step, data.child_id || key, label, capabilityKind, type === 'tool_error' || type === 'tool_blocked' ? 'failed' : type === 'tool_result' ? 'completed' : 'running');
    child.detail = event.content || child.detail;
    setAgentThinkingCurrent(taskId, type === 'model' ? '正在调用模型…' : type === 'tool_result' ? '工具已返回，正在整理结果…' : type === 'tool_error' || type === 'tool_blocked' ? '工具调用未完成' : '正在调用工具…', label, type, capabilityKind);
  } else if (type === 'knowledge' || type === 'memory') {
    addThinkingRole(item, `${type}:${event.id || event.content}`, 'knowledge', type === 'knowledge' ? '项目知识库' : '平台记忆', '已应用');
    const step = ensureThinkingStep(item, 'prepare', '整理上下文与授权能力', 'knowledge', 'running');
    const child = ensureThinkingChild(step, `${type}:${event.id || 'context'}`, type === 'knowledge' ? '检索知识库' : '应用平台记忆', 'knowledge', 'completed');
    child.detail = event.content || child.detail;
    step.status = 'completed';
    setAgentThinkingCurrent(taskId, type === 'knowledge' ? '已检索相关资料' : '已应用平台记忆', event.content || '', type, 'knowledge');
  } else if (['verification_started', 'verification_result', 'output_check'].includes(type)) {
    const step = ensureThinkingStep(item, 'validate', '结果验收', 'verification', type === 'verification_result' || type === 'output_check' ? 'completed' : 'running');
    step.detail = event.content || step.detail;
    addThinkingRole(item, 'verification:final', 'verification', '结果验收', type === 'verification_result' || type === 'output_check' ? '已检查' : '进行中');
    setAgentThinkingCurrent(taskId, type === 'verification_started' ? '正在验收生成结果…' : '结果验收已更新', event.content || '核对结果与当前目标', type, 'verification');
  } else if (type === 'answer_delta') {
    item.answerChars += String(event.content || '').length;
    setAgentThinkingCurrent(taskId, '正在输出结果…', `${item.answerChars} 字 · 通过验收后发布`, type, 'output');
  } else if (type === 'answer') {
    setAgentThinkingCurrent(taskId, '最终答复已生成', '正在完成交付', type, 'output');
  } else if (type === 'approval_required' || type === 'clarification') {
    item.status = status;
    setAgentThinkingCurrent(taskId, type === 'approval_required' ? '等待你的确认' : '需要补充信息', event.content || '补充后将继续当前任务', type, 'goal');
  } else if (type === 'error') {
    item.status = 'failed';
    setAgentThinkingCurrent(taskId, event.title || '任务执行失败', event.content || '请检查模型、参数或工具配置后重试', type, 'error');
  } else if (type === 'done') {
    setAgentThinkingCurrent(taskId, '任务已完成', event.content || '结果已发布', type, 'output');
  } else {
    setAgentThinkingCurrent(taskId, event.title || event.content || '正在执行任务…', event.content || '', type, thinkingIconForEvent(type));
  }
  // The conversation view is an activity feed, not a second runtime panel.
  // Record only public, typed events; private reasoning is filtered above.
  recordThinkingActivity(item, type, event, data);
  if (type !== 'error' && type !== 'approval_required' && type !== 'clarification' && type !== 'done') item.status = 'running';
  renderAgentThinkingCard(item);
  if (['done', 'error', 'cancelled'].includes(type)) finishAgentThinkingCard(taskId, type === 'done' ? 'completed' : type);
}

function finishAgentThinkingCard(taskId, status = 'completed') {
  const item = agentThinkingCard(taskId);
  if (!item) return;
  item.status = status === 'succeeded' ? 'completed' : status === 'submission_failed' ? 'failed' : status;
  if (status === 'submission_failed') {
    item.current = { type: 'error', kind: 'error', label: '发送失败', detail: '所选模型不可用，任务尚未开始执行' };
  }
  item.finishedAt = item.finishedAt || Date.now();
  // Historical cards stay open when the user explicitly expanded them; live
  // cards collapse after delivery so the conversation returns to a concise
  // result view.
  if (item.status === 'completed' || item.status === 'cancelled') item.node.open = item.historical ? item.node.open : false;
  else item.node.open = true;
  renderAgentThinkingCard(item);
}

async function loadHistoricalThinking(taskId) {
  const item = agentThinkingCard(taskId);
  if (!item || item.loading || item.loaded || !taskId || taskId === '__pending__') return;
  item.loading = true;
  item.current = { type: 'start', kind: 'process', label: '正在加载执行记录…', detail: '读取目标、计划、Skill/MCP 与验收节点' };
  renderAgentThinkingCard(item);
  try {
    const task = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    item.task = task;
    item.loaded = true;
    if (runtimeIsActive(task.status)) {
      item.status = 'running';
      item.node.open = true;
    }
    if (task.expert_selection) updateAgentThinkingEvent(taskId, { type: 'expert_selection', data: task.expert_selection, content: task.expert_selection.reason || '' });
    (task.events || []).forEach((event) => updateAgentThinkingEvent(taskId, event));
    if (['completed', 'succeeded', 'failed', 'cancelled'].includes(task.status)) finishAgentThinkingCard(taskId, task.status);
  } catch (err) {
    item.status = 'failed';
    setAgentThinkingCurrent(taskId, '执行记录读取失败', err.message || '请打开运行记录查看完整信息', 'error', 'error');
    renderAgentThinkingCard(item);
  } finally {
    item.loading = false;
  }
}

function openRuntimeFromThinking(taskId) {
  const id = String(taskId || '');
  if (!id || id === '__pending__') return notify('任务建立后才能查看运行控制', 'error');
  const reveal = () => {
    const target = $('timelineSection') || $('runtimeInspectorSection');
    if (!target) return;
    if ($('runtimeInspectorSection')) $('runtimeInspectorSection').open = true;
    target.open = true;
    target.classList.remove('runtime-jump-highlight');
    // Force the highlight animation to restart when the user clicks again.
    void target.offsetWidth;
    target.classList.add('runtime-jump-highlight');
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    window.setTimeout(() => target.classList.remove('runtime-jump-highlight'), 1800);
    notify('已跳转到执行过程', 'success');
  };
  if (state.currentTask?.id === id) {
    switchTab('chat');
    ['taskOverviewSection', 'runtimeInspectorSection', 'timelineSection'].forEach((sectionId) => { if ($(sectionId)) $(sectionId).open = true; });
    loadTaskRuntime(id, { silent: true }).finally(reveal);
    return;
  }
  openTask(id).then(reveal).catch((err) => notify(`打开运行控制失败：${err.message || err}`, 'error'));
}

async function stopActiveTask() {
  const taskId = state.currentTask?.id;
  if (!taskId) {
    notify('任务正在提交，请稍候再停止', 'error');
    return;
  }
  state.taskUiCancelRequested = true;
  setSendButtonState('cancel_requested');
  setTaskUiStatus(taskId, '正在停止任务…', 'verifying');
  const accepted = await sendTaskRuntimeCommand('cancel', { reason: '用户点击停止' }, null);
  if (!accepted) {
    state.taskUiCancelRequested = false;
    if (runtimeIsActive(currentRuntimeStatus())) setSendButtonState('running');
  }
}

function setBusy(button, busy, label = '保存中…') {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? label : button.dataset.label;
}

function switchTab(tab) {
  document.querySelectorAll('.nav').forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === tab));
  document.querySelectorAll('.tab').forEach((el) => el.classList.remove('active'));
  $(`tab-${tab}`).classList.add('active');
  if (tab !== 'loops') {
    clearTimeout(state.loopPollTimer);
    state.loopPollTimer = null;
  }
  if (tab === 'workspaces') {
    loadWorkspacesOnly({ preserveSelection: true }).catch((err) => notify(`项目刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'skills') {
    loadSkillsOnly().catch((err) => notify(`技能刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'mcp') {
    loadMcpOnly().catch((err) => notify(`工具服务刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'marketplace') {
    loadMarketplaceOnly().catch((err) => notify(`市场刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'experts') {
    loadExpertWorkspace({ preserveSelection: true }).catch((err) => notify(`专家团刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'memory') {
    loadMemoriesOnly({ preserveSelection: true }).catch((err) => notify(`记忆刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'knowledge') {
    loadKnowledgeBasesOnly({ preserveSelection: true }).catch((err) => notify(`知识库刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'diagnostics') {
    loadDiagnosticsOnly().catch((err) => notify(`自检失败：${err.message || err}`, 'error'));
  } else if (tab === 'artifacts') {
    loadArtifactsOnly({ preserveSelection: true }).catch((err) => notify(`产物刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'loops') {
    loadLoopsOnly().catch((err) => notify(`自动化刷新失败：${err.message || err}`, 'error'));
  } else if (tab === 'users') {
    loadAdminUsers().catch((err) => notify(err.message, 'error'));
  } else if (tab === 'engines') {
    loadExecutionEnginesOnly({ preserveSelection: true }).catch((err) => notify(`执行引擎刷新失败：${err.message || err}`, 'error'));
  }
}

async function loadSkillsOnly() {
  state.skills = await api('/api/skills');
  renderSkills();
  return state.skills;
}

async function loadMcpOnly() {
  state.mcp = await api('/api/mcp');
  renderMcp();
  return state.mcp;
}

function ensureSelectedWorkspace() {
  const enabled = state.workspaces.filter((item) => item.enabled);
  const preferred = state.workspaceId || readPreference('workspace') || 'default';
  const selected = enabled.find((item) => item.id === preferred)
    || enabled.find((item) => item.id === 'default')
    || state.workspaces.find((item) => item.id === 'default')
    || enabled[0]
    || state.workspaces[0]
    || null;
  state.selectedWorkspace = selected;
  state.workspaceId = selected?.id || 'default';
  writePreference('workspace', state.workspaceId);
}

async function loadWorkspacesOnly({ preserveSelection = false } = {}) {
  const previous = preserveSelection ? state.selectedWorkspace?.id || state.workspaceId : state.workspaceId;
  state.workspaces = await api(`/api/workspaces?${workspaceQuery({ include_disabled: 'true' })}`);
  state.workspaceId = previous || readPreference('workspace') || 'default';
  ensureSelectedWorkspace();
  renderWorkspaceSelect();
  renderWorkspaces();
  if (state.selectedWorkspace) selectWorkspaceEditor(state.selectedWorkspace.id, { activate: false });
}

function renderWorkspaceSelect() {
  const select = $('workspaceSelect');
  if (!select) return;
  const enabled = state.workspaces.filter((item) => item.enabled);
  select.innerHTML = enabled.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
  if (enabled.some((item) => item.id === currentWorkspaceId())) select.value = currentWorkspaceId();
}

async function loadAll() {
  const [skills, mcp, agents, tasks, models, executionEngines, loops, workspaces, capabilities, marketplace] = await Promise.all([
    api('/api/skills'),
    api('/api/mcp'),
    api(`/api/agents?workspace_id=${encodeURIComponent(currentWorkspaceId())}`),
    api(`/api/tasks?${new URLSearchParams(platformScopeValues()).toString()}`),
    api('/api/models'),
    api('/api/execution-engines'),
    api(`/api/loops?${new URLSearchParams(platformScopeValues()).toString()}`),
    api(`/api/workspaces?${workspaceQuery()}`),
    api('/api/capabilities'),
    api('/api/marketplace'),
  ]);
  state.skills = skills;
  state.mcp = mcp;
  state.agents = agents;
  state.tasks = tasks;
  state.models = models;
  state.executionEngines = executionEngines;
  state.loops = loops;
  state.workspaces = workspaces;
  state.capabilities = capabilities;
  state.marketplace = marketplace;
  ensureSelectedWorkspace();
  renderWorkspaceSelect();
  renderWorkspaces();
  renderAgentsSelect();
  renderTaskModelSelect();
  renderSkills();
  renderMcp();
  renderAgents();
  renderTasks();
  renderModels();
  renderExecutionEngines();
  renderExecutionEngineSelect();
  renderLoops();
  renderCapabilities();
  renderMarketplace();
  const [memoryLoad, knowledgeLoad, artifactLoad, expertLoad] = await Promise.allSettled([
    loadMemoriesOnly({ preserveSelection: true }),
    loadKnowledgeBasesOnly({ preserveSelection: true }),
    loadArtifactsOnly({ preserveSelection: true }),
    loadExpertWorkspace({ preserveSelection: true }),
  ]);
  if (memoryLoad.status === 'rejected') {
    console.warn('记忆模块初始化失败', memoryLoad.reason);
    $('memoryEffectiveMeta').textContent = '记忆服务暂不可用，可稍后重新计算';
    $('memoryEffectiveContext').textContent = '平台其他功能仍可正常使用。';
  }
  if (knowledgeLoad.status === 'rejected') {
    console.warn('知识库模块初始化失败', knowledgeLoad.reason);
    state.knowledgeBases = [];
    state.knowledgeDocuments = [];
    state.knowledgeSearchResults = [];
    state.selectedKnowledgeBase = null;
    renderKnowledgeBases();
    renderKnowledgeDocuments();
    renderKnowledgeSearchResults();
  }
  if (artifactLoad.status === 'rejected') {
    console.warn('产物工作区初始化失败', artifactLoad.reason);
    state.artifacts = [];
    state.selectedArtifact = null;
    renderArtifactWorkspace();
    resetArtifactPreview('产物服务暂不可用，请稍后刷新。');
  }
  if (expertLoad.status === 'rejected') {
    console.warn('专家团模块初始化失败', expertLoad.reason);
    state.expertTemplates = [];
    state.expertInstallations = [];
    state.expertTeams = [];
    state.expertTeamRuns = [];
    $('expertTemplateList').innerHTML = '<div class="meta empty">专家模板服务暂不可用，请稍后刷新。</div>';
    $('expertInstallationList').innerHTML = '<div class="meta empty">暂时无法读取已安装专家。</div>';
    $('expertTeamList').innerHTML = '<div class="meta empty">专家团服务暂不可用，请稍后刷新。</div>';
    resetExpertRunLive('暂时无法读取团队运行。');
  }
  await refreshExecutionMode();
}

async function refreshExecutionMode() {
  const notice = $('runtimeModeNotice');
  if (!notice) return;
  try {
    const response = await fetch('/api/readiness');
    const status = await response.json();
    notice.textContent = status.mode === 'local'
      ? '当前为本地开发模式，不适合多人共享部署。'
      : status.ready ? '任务执行服务已就绪。' : '任务执行服务暂未就绪，新任务可能需要等待。';
  } catch (error) {
    notice.textContent = '暂时无法检查任务执行服务，请刷新重试。';
  }
}

function renderAgentsSelect() {
  const select = $('agentSelect');
  const previous = select.value || readPreference('agent') || 'general-agent';
  select.innerHTML = state.agents.map((a) => `<option value="${escapeHtml(a.id)}">${escapeHtml(a.name)}</option>`).join('');
  if (state.agents.some((a) => a.id === previous)) select.value = previous;
  else if (state.agents.some((a) => a.id === 'general-agent')) select.value = 'general-agent';
}

function enabledWorkbenchTeams() {
  return state.expertTeams.filter((team) => team.enabled);
}

function renderWorkbenchTeamOptions() {
  const select = $('expertTeamSelect');
  if (!select) return;
  const teams = enabledWorkbenchTeams();
  const previous = select.value || readPreference('expert-team') || '';
  select.innerHTML = `<option value="">自动匹配专家团${teams.length ? '' : '（暂无可用团队）'}</option>${teams.map((team) => `<option value="${escapeHtml(team.id)}">指定：${escapeHtml(team.name)}</option>`).join('')}`;
  if (teams.some((team) => team.id === previous)) select.value = previous;
  else select.value = '';
}

function renderWorkbenchMode() {
  const expert = state.workbenchMode === 'expert';
  document.querySelectorAll('[data-workbench-mode]').forEach((button) => {
    const active = button.dataset.workbenchMode === state.workbenchMode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  $('agentSelect')?.classList.toggle('hidden', expert);
  $('expertTeamControl')?.classList.toggle('hidden', !expert);
  const selectedTeam = enabledWorkbenchTeams().find((team) => team.id === $('expertTeamSelect')?.value);
  if ($('workbenchModeDescription')) {
    $('workbenchModeDescription').textContent = expert
      ? '自动匹配已配置的专家团；团队内成员独立并行分析，再由主管统一汇总和验收。'
      : '描述目标或上传资料，任务助手会自动选择合适的技能和工具。';
  }
  if ($('composerModeHint')) {
    $('composerModeHint').textContent = expert
      ? '专家协作 · 根据目标匹配已启用团队'
      : '单智能体处理 · 自动匹配技能和工具';
  }
  if ($('messageInput')) {
    $('messageInput').placeholder = expert
      ? '描述需要多位专家协作分析的目标，或上传相关资料…'
      : '输入任务，例如：根据我上传的资料生成一份项目分析报告…';
  }
  if ($('expertTeamHint')) {
    $('expertTeamHint').textContent = selectedTeam
      ? `固定使用“${selectedTeam.name}”，由团队主管汇总`
      : `根据任务目标自动匹配 · 当前 ${enabledWorkbenchTeams().length} 个可用团队`;
  }
}

function setWorkbenchMode(mode, { persist = true } = {}) {
  state.workbenchMode = mode === 'expert' ? 'expert' : 'agent';
  if (persist) writePreference('workbench-mode', state.workbenchMode);
  renderWorkbenchMode();
}

function renderTaskModelSelect() {
  const select = $('taskModelSelect');
  const enabled = state.models.filter((m) => m.enabled);
  const ready = enabled.filter((m) => (m.readiness?.state || 'ready') === 'ready');
  const explicit = readPreference('model-explicit') === '1';
  const workspacePreferred = state.selectedWorkspace?.default_model_id;
  const remembered = select.value || readPreference('model');
  const preferred = explicit
    ? remembered || workspacePreferred || ready.find((m) => m.id !== 'deterministic')?.id || 'deterministic'
    : workspacePreferred && ready.some((m) => m.id === workspacePreferred)
      ? workspacePreferred
      : ready.find((m) => m.id !== 'deterministic')?.id || workspacePreferred || remembered || 'deterministic';
  select.innerHTML = enabled.map((m) => {
    const readyState = m.readiness?.state || 'ready';
    const label = m.readiness?.label || (m.enabled ? '可用' : '停用');
    return `<option value="${escapeHtml(m.id)}" ${readyState !== 'ready' ? 'disabled' : ''}>模型：${escapeHtml(m.name)} · ${escapeHtml(label)}</option>`;
  }).join('');
  if (ready.some((m) => m.id === preferred)) select.value = preferred;
  else if (ready.length) select.value = ready[0].id;
  renderWorkbenchModelStatus();
  renderExecutionEngineSelect();
}

function currentWorkbenchModel() {
  return state.models.find((m) => m.id === $('taskModelSelect')?.value) || null;
}

function renderWorkbenchModelStatus() {
  const pill = $('modelStatusPill');
  if (!pill) return;
  const model = currentWorkbenchModel();
  const stateValue = model?.readiness?.state || (model?.enabled ? 'ready' : 'off');
  pill.className = `model-status-pill ${escapeHtml(modelReadinessClass(model))}`;
  pill.textContent = model ? modelReadinessLabel(model) : '未选择模型';
  pill.title = model?.readiness?.detail || (model ? '打开模型设置' : '请先配置模型');
}

function addMessage(role, content, eventId = null) {
  const div = document.createElement('div');
  div.className = `message ${role}`;
  if (eventId != null) div.dataset.eventId = String(eventId);
  renderMessageContent(div, content);
  $('conversation').appendChild(div);
  $('conversation').scrollTop = $('conversation').scrollHeight;
  return div;
}

function renderMessageContent(element, content) {
  if (element.classList.contains('user')) {
    element.textContent = content;
    return;
  }
  element.innerHTML = element.classList.contains('agent')
    ? renderAssistantContent(content)
    : renderMarkdown(content);
}

function stripInternalAnswerMarkers(content) {
  return String(content ?? '')
    .replace(/^\s*(?:[-*+]\s*)?(?:本次)?验收代号\s*[:：][^\n]*(?:\n|$)/gim, '')
    .replace(/(?:本次)?验收代号\s*[:：]\s*[\u4e00-\u9fffA-Za-z0-9_-]+/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// Some older/custom agents still return a fixed “任务理解 / 执行过程 / 结果”
// checklist. Keep those responses readable without hiding information: show the
// useful result first and move the process notes into a collapsed disclosure.
function assistantSectionKind(label) {
  const value = String(label || '').replace(/[*_]/g, '').trim();
  if (/^(任务理解|目标理解|理解)$/.test(value)) return 'understand';
  if (/^(执行过程|执行步骤|处理过程|过程)$/.test(value)) return 'process';
  if (/^(结果|最终结果|结论|答案)$/.test(value)) return 'result';
  if (/^(后续计划|后续建议|建议|下一步|可沉淀(?:为)?\s*Skill(?:\s*的建议)?)$/.test(value)) return 'next';
  return '';
}

function renderAssistantContent(content) {
  const source = stripInternalAnswerMarkers(content).replace(/\r\n?/g, '\n');
  const lines = source.split('\n');
  const sectionPattern = /^\s*(?:[-*+]\s+)?(?:\*\*)?([^：:]{1,28})(?:\*\*)?\s*[:：]\s*(.*)$/;
  const sections = [];
  const intro = [];
  let current = null;

  lines.forEach((line) => {
    const match = line.match(sectionPattern);
    const kind = match ? assistantSectionKind(match[1]) : '';
    if (kind) {
      current = { kind, label: String(match[1]).replace(/[*_]/g, '').trim(), body: match[2] || '' };
      sections.push(current);
      return;
    }
    if (current && line.trim()) {
      current.body = `${current.body}${current.body ? '\n' : ''}${line}`;
    } else if (current && !line.trim()) {
      current.body = `${current.body}\n`;
    } else {
      intro.push(line);
    }
  });

  // Only reshape a recognizable multi-part template. Normal Markdown remains
  // untouched, including user-requested headings and lists.
  if (sections.length < 2) return renderMarkdown(source);

  const resultSections = sections.filter((item) => item.kind === 'result');
  const leadSections = resultSections.length ? resultSections : sections.slice(0, 1);
  const detailSections = sections.filter((item) => !leadSections.includes(item));
  const introMarkup = intro.join('\n').trim() ? `<div class="assistant-lead">${renderMarkdown(intro.join('\n'))}</div>` : '';
  const resultMarkup = `<div class="assistant-result"><div class="assistant-result-label"><span>结果</span><small>${resultSections.length ? '已整理' : '答复'}</small></div><div class="assistant-result-body">${leadSections.map((item) => renderMarkdown(item.body)).join('')}</div></div>`;
  const detailMarkup = detailSections.length ? `
    <details class="assistant-details">
      <summary><span>查看处理详情</span><small>${detailSections.length} 项</small></summary>
      <div class="assistant-detail-list">${detailSections.map((item) => `<div class="assistant-detail-row"><span class="assistant-detail-label">${escapeHtml(item.label)}</span><div>${renderMarkdown(item.body)}</div></div>`).join('')}</div>
    </details>` : '';
  return `<div class="assistant-response">${introMarkup}${resultMarkup}${detailMarkup}</div>`;
}

function renderMarkdown(content) {
  const lines = escapeHtml(String(content ?? '').replace(/\r\n?/g, '\n')).split('\n');
  const output = [];
  let paragraph = [];
  let listType = '';
  let code = null;
  const codeLanguageLabels = {
    json: 'JSON',
    python: 'Python',
    py: 'Python',
    bash: 'Shell',
    sh: 'Shell',
    shell: 'Shell',
    zsh: 'Shell',
    javascript: 'JavaScript',
    js: 'JavaScript',
    typescript: 'TypeScript',
    ts: 'TypeScript',
    sql: 'SQL',
    yaml: 'YAML',
    yml: 'YAML',
    html: 'HTML',
    css: 'CSS',
    markdown: 'Markdown',
    md: 'Markdown',
  };
  const renderCodeBlock = (block) => {
    const language = String(block.language || '').toLowerCase();
    const label = codeLanguageLabels[language] || (language ? language.toUpperCase() : '代码');
    const languageClass = block.language ? ` class="language-${block.language}"` : '';
    return `<div class="code-block" data-code-language="${escapeHtml(block.language || '')}"><div class="code-block-toolbar"><span>${escapeHtml(label)}</span><button type="button" class="code-copy-button" data-copy-code aria-label="复制${escapeHtml(label)}代码">复制</button></div><pre><code${languageClass}>${block.lines.join('\n')}</code></pre></div>`;
  };

  const inline = (value) => value
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\[([^\]]+)\]\((\/api\/artifacts\/[^)\s]+|https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(?<!\*)\*([^*]+)\*(?!\*)/g, '<em>$1</em>');
  const closeParagraph = () => {
    if (paragraph.length) output.push(`<p>${paragraph.map(inline).join('<br>')}</p>`);
    paragraph = [];
  };
  const closeList = () => {
    if (listType) output.push(`</${listType}>`);
    listType = '';
  };
  const openList = (type) => {
    closeParagraph();
    if (listType !== type) {
      closeList();
      output.push(`<${type}>`);
      listType = type;
    }
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const fence = line.match(/^```([a-zA-Z0-9_-]*)\s*$/);
    if (fence) {
      closeParagraph(); closeList();
      if (code) {
        output.push(renderCodeBlock(code));
        code = null;
      } else {
        code = { language: fence[1], lines: [] };
      }
      continue;
    }
    if (code) { code.lines.push(line); continue; }
    if (!line.trim()) { closeParagraph(); closeList(); continue; }

    const next = lines[index + 1] || '';
    if (line.includes('|') && /^\s*\|?\s*:?-{3,}/.test(next)) {
      closeParagraph(); closeList();
      const headers = line.replace(/^\||\|$/g, '').split('|').map((cell) => inline(cell.trim()));
      output.push('<div class="md-table-wrap"><table><thead><tr>' + headers.map((cell) => `<th>${cell}</th>`).join('') + '</tr></thead><tbody>');
      index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const cells = lines[index].replace(/^\||\|$/g, '').split('|').map((cell) => inline(cell.trim()));
        output.push('<tr>' + cells.map((cell) => `<td>${cell}</td>`).join('') + '</tr>');
        index += 1;
      }
      output.push('</tbody></table></div>');
      index -= 1;
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      closeParagraph(); closeList();
      const level = heading[1].length;
      output.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      closeParagraph(); closeList(); output.push('<hr>'); continue;
    }
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    if (unordered) { openList('ul'); output.push(`<li>${inline(unordered[1])}</li>`); continue; }
    const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ordered) { openList('ol'); output.push(`<li>${inline(ordered[1])}</li>`); continue; }
    const quote = line.match(/^&gt;\s?(.*)$/);
    if (quote) { closeParagraph(); closeList(); output.push(`<blockquote>${inline(quote[1])}</blockquote>`); continue; }
    closeList();
    paragraph.push(line);
  }
  if (code) output.push(renderCodeBlock(code));
  closeParagraph(); closeList();
  return output.join('');
}

async function sendTask() {
  if (state.taskUiRunning) {
    await stopActiveTask();
    return;
  }
  const message = $('messageInput').value.trim();
  if (!message) return;
  const nonContextUploads = state.uploads.filter((item) => item.context_status && item.context_status.extractable === false);
  if (nonContextUploads.length) {
    const names = nonContextUploads.map((item) => `- ${item.name || item.id}：${uploadContextStateLabel(item)}`).join('\n');
    if (!confirm(`以下附件不会自动进入模型上下文：\n${names}\n\n仍然继续发送吗？`)) return;
  }
  const expertMode = state.workbenchMode === 'expert';
  if (expertMode && !enabledWorkbenchTeams().length) {
    notify('还没有可用的专家团，请先在“专家团”中创建并启用团队', 'error');
    return;
  }
  const button = $('sendBtn');
  state.taskUiRunning = true;
  state.taskUiCancelRequested = false;
  setSendButtonState('running');
  stopTaskStream();
  clearTaskUiStatus();
  addMessage('user', message);
  createAgentThinkingCard('__pending__', { historical: false, open: true });
  setTaskUiStatus('__pending__', '正在提交任务…', 'thinking');
  $('timeline').innerHTML = '';
  state.currentExpertSelection = null;
  $('artifacts').innerHTML = '暂无产物';
  $('artifacts').classList.add('empty');
  const selectedModel = state.models.find((m) => m.id === $('taskModelSelect').value);
  if (selectedModel && (selectedModel.readiness?.state || 'ready') !== 'ready') {
    // Keep the message in the composer so the user can correct the model and
    // retry without retyping it.  The conversation still records the failed
    // attempt and its human-readable reason.
    finishSubmissionFailure(
      `所选模型暂不可用（${selectedModel.readiness?.label || '需要配置'}）：${selectedModel.readiness?.detail || '请到模型设置检查配置。'}`,
    );
    switchTab('models');
    selectModel(selectedModel.id);
    return;
  }
  $('messageInput').value = '';
  try {
    const submissionBody = JSON.stringify({
        message,
        agent_id: $('agentSelect').value,
        model_id: $('taskModelSelect').value,
        conversation_id: state.conversationId,
        workspace: currentWorkspaceId(),
        organization_id: 'local-org',
        user_id: 'local-user',
        executor_type: expertMode ? 'team' : 'agent',
        executor_id: expertMode ? ($('expertTeamSelect').value || null) : $('agentSelect').value,
        attachment_ids: state.uploads.map((x) => x.id),
        execution_engine: $('executionEngineSelect')?.value || 'builtin',
      });
    if (state.pendingSubmission?.body !== submissionBody) {
      state.pendingSubmission = {body: submissionBody, key: createConversationId()};
    }
    const task = await api('/api/tasks', {
      method: 'POST',
      headers: {'Idempotency-Key': state.pendingSubmission.key},
      body: submissionBody,
    });
    state.pendingSubmission = null;
    state.uploads = [];
    renderUploads();
    state.currentTask = task;
    state.taskUiTaskId = task.id;
    bindAgentThinkingCard('__pending__', task.id);
    if (state.taskUiStatusNode) {
      state.taskUiStatusNode.dataset.taskStatus = String(task.id);
      state.taskUiTaskId = task.id;
    }
    setTaskUiStatus(task.id, '正在理解任务并制定执行计划…', 'thinking');
    renderTaskMeta(task);
    if (task.expert_selection) {
      updateAgentThinkingEvent(task.id, {
        type: 'expert_selection',
        title: '已选择参与专家',
        content: task.expert_selection.reason || '',
        data: task.expert_selection,
      });
      renderExpertSelectionEvent({
        type: 'expert_selection',
        title: '已选择参与专家',
        content: task.expert_selection.reason || '',
        data: task.expert_selection,
      });
    }
    watchTaskRuntime(task.id);
    startTaskStream(task.id);
  } catch (err) {
    // The API can reject before a task id exists (for example when the
    // selected model became unavailable after the page was loaded).  Keep the
    // temporary “正在提交” status from trapping the send button in running
    // mode, and distinguish this from a failure inside an existing task.
    if (!$('messageInput').value) $('messageInput').value = message;
    finishSubmissionFailure(err.message || err);
  }
}

function renderTaskMeta(task) {
  const overview = $('taskOverviewSection');
  const overviewStatus = $('taskOverviewStatus');
  $('taskMeta').classList.remove('empty');
  const model = state.models.find((m) => m.id === task.model_id);
  const expert = task.executor_type === 'team';
  const needsClarification = taskNeedsClarification(task);
  const executor = expert
    ? state.expertTeams.find((team) => team.id === task.executor_id)?.name || task.executor_id || '自动匹配'
    : state.agents.find((agent) => agent.id === task.agent_id)?.name || task.agent_id;
  const missing = taskMissingInputs(task);
  const status = needsClarification ? '等待补充信息' : runtimeStatusLabel(task.status);
  if (overviewStatus) overviewStatus.textContent = `${status} · ${task.title || task.id}`;
  if (overview) overview.open = runtimeIsActive(task.status) || ['failed', 'cancelled', 'waiting_input', 'waiting_approval'].includes(task.status) || needsClarification;
  $('taskMeta').innerHTML = `<strong>${escapeHtml(task.title || task.id)}</strong>\n状态：${escapeHtml(status)}${needsClarification && missing.length ? `\n还差：${escapeHtml(missing.join('、'))}` : ''}\nID：${escapeHtml(task.id)}\n会话：${escapeHtml(task.conversation_id || state.conversationId)}\n模式：${expert ? '专家协作' : '普通任务'}\n${expert ? '专家团' : '智能体'}：${escapeHtml(executor)}\n模型：${escapeHtml(model?.name || task.model_id || '跟随智能体')}`;
}

function taskMissingInputs(task) {
  const values = task?.result?.missing_information || task?.result?.missing_inputs || [];
  return runtimeArray(values).map((item) => {
    if (item && typeof item === 'object') return friendlyInputLabel(item.label || item.key || item.name || '');
    return friendlyInputLabel(item);
  }).filter(Boolean);
}

function friendlyInputLabel(value) {
  const key = String(value || '').trim();
  const normalized = key.toLowerCase().replace(/[_./-]+/g, ' ').replace(/\s+/g, ' ').trim();
  const labels = {
    city: '城市或地区', location: '城市或地区', date: '日期', time: '时间',
    topic: '主题', audience: '受众', source: '资料来源', format: '输出格式',
    'product description': '产品描述', 'product details': '产品描述',
    description: '描述', 'programming language': '编程语言',
    'selected language': '编程语言', 'preferred language': '编程语言',
    language: '编程语言', 'tech stack': '技术栈',
  };
  return labels[normalized] || key;
}

function taskNeedsClarification(task, runtime = null) {
  return task?.result?.needs_clarification === true
    || runtime?.active_goal?.status === 'needs_input'
    || runtime?.trace_summary?.goal?.status === 'needs_input';
}

const runtimeStatusLabels = {
  pending: '等待中', queued: '已排队', running: '执行中', processing: '处理中',
  completed: '已完成', succeeded: '已完成', failed: '失败', cancelled: '已取消',
  cancel_requested: '取消中', paused: '已暂停', interrupted: '已中断',
  waiting: '等待中', waiting_input: '等待补充信息', waiting_approval: '等待审批', restored: '已恢复', rejected: '已拒绝',
};

const runtimeCommandLabels = {
  cancel: '取消任务', retry: '重试任务', resume: '恢复任务',
  message: '追加指令', restore_checkpoint: '恢复检查点',
};

function runtimeArray(value) {
  return Array.isArray(value) ? value : [];
}

function runtimeStatusLabel(status) {
  return runtimeStatusLabels[status] || status || '未知';
}

function runtimeStatusClass(status) {
  const value = String(status || 'unknown').toLowerCase();
  return /^[a-z0-9_-]+$/.test(value) ? value : 'unknown';
}

function runtimeTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function runtimeActiveRun(runtime = state.taskRuntime) {
  const active = runtime?.active_run;
  if (active && typeof active === 'object') return active;
  if (active != null) return runtimeArray(runtime?.runs).find((run) => String(run.id) === String(active)) || null;
  return null;
}

function currentRuntimeStatus(runtime = state.taskRuntime) {
  return runtimeActiveRun(runtime)?.status || state.currentTask?.status || 'pending';
}

function runtimeIsActive(status) {
  return ['pending', 'queued', 'running', 'processing', 'cancel_requested', 'waiting', 'waiting_approval'].includes(status);
}

function taskUiGeneratingStatus(status) {
  return ['pending', 'queued', 'running', 'processing', 'cancel_requested', 'waiting'].includes(String(status || ''));
}

function setRuntimeCount(id, count) {
  $(id).textContent = String(count);
}

function runtimeItem(title, status, detail = '', meta = '', options = {}) {
  const classes = ['runtime-item'];
  if (options.active) classes.push('current');
  if (options.child) classes.push('child-node');
  return `
    <div class="${classes.join(' ')}">
      <span class="runtime-dot ${escapeHtml(runtimeStatusClass(status))}"></span>
      <div class="runtime-item-copy"><strong>${escapeHtml(title)}${options.active ? '<span class="runtime-current-tag">当前</span>' : ''}</strong>${detail ? `<small>${escapeHtml(detail)}</small>` : ''}${options.capability ? `<span class="runtime-capability ${escapeHtml(runtimeStatusClass(options.capability.type))}">${escapeHtml(options.capability.label)}</span>` : ''}</div>
      <span class="runtime-item-meta">${escapeHtml(meta || runtimeStatusLabel(status))}</span>
    </div>
  `;
}

const verificationStateLabels = {
  not_started: '尚未验收', pending: '正在验收', verifying: '正在验收',
  verification_missing: '缺少验收记录', legacy_unverified: '历史任务未验收',
  passed: '验收通过', failed: '验收未通过', inconclusive: '验收未得出结论',
};

function verificationStateLabel(stateValue) {
  return verificationStateLabels[stateValue] || stateValue || '尚未验收';
}

function verificationStateClass(stateValue) {
  const value = String(stateValue || 'not_started').toLowerCase();
  if (value === 'passed') return 'passed';
  if (value === 'failed') return 'failed';
  if (['pending', 'verifying'].includes(value)) return 'pending';
  if (['verification_missing', 'legacy_unverified', 'inconclusive'].includes(value)) return 'warning';
  return 'idle';
}

function publicVerificationReport(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const report = value.public_report || value.report || value;
  return report && typeof report === 'object' && !Array.isArray(report) ? report : null;
}

function verificationSummaryMarkup(reportValue, stateValue = '', { compact = false } = {}) {
  const report = publicVerificationReport(reportValue) || {};
  const rules = runtimeArray(report.rules);
  const passedRules = rules.filter((item) => item?.status === 'passed').length;
  const failedRules = rules.filter((item) => item?.status === 'failed').length;
  const semantic = report.semantic && typeof report.semantic === 'object' ? report.semantic : {};
  const semanticLabels = { passed: '语义复核通过', failed: '语义复核未通过', skipped: '未执行语义复核', error: '语义复核异常' };
  const coverageLabels = { rules_only: '规则校验', rules_and_semantic: '规则 + 语义复核' };
  const repairs = runtimeArray(report.repair_instructions).filter((item) => typeof item === 'string' && item.trim()).slice(0, 3);
  const inferredState = stateValue || (report.passed === true ? 'passed' : report.passed === false ? 'failed' : 'not_started');
  const reason = report.public_reason || '';
  return `
    <div class="verification-summary ${escapeHtml(verificationStateClass(inferredState))} ${compact ? 'compact' : ''}">
      <div class="verification-summary-head">
        <strong>${escapeHtml(verificationStateLabel(inferredState))}</strong>
        ${report.coverage ? `<span>${escapeHtml(coverageLabels[report.coverage] || report.coverage)}</span>` : ''}
      </div>
      ${reason ? `<p>${escapeHtml(reason)}</p>` : ''}
      ${(rules.length || semantic.status) ? `<div class="verification-facts">
        ${rules.length ? `<span>规则 ${passedRules}/${rules.length} 通过${failedRules ? ` · ${failedRules} 项未通过` : ''}</span>` : ''}
        ${semantic.status ? `<span>${escapeHtml(semanticLabels[semantic.status] || semantic.status)}</span>` : ''}
      </div>` : ''}
      ${repairs.length ? `<div class="verification-repairs"><strong>建议修复</strong>${repairs.map((item) => `<span>${escapeHtml(item)}</span>`).join('')}</div>` : ''}
    </div>`;
}

function renderRuntimeAssurance(runtime) {
  const box = $('runtimeAssurance');
  if (!box) return;
  const goal = runtime?.active_goal && typeof runtime.active_goal === 'object' ? runtime.active_goal : null;
  const verification = runtime?.verification && typeof runtime.verification === 'object' ? runtime.verification : null;
  const trace = runtime?.trace_summary && typeof runtime.trace_summary === 'object' ? runtime.trace_summary : null;
  if (!goal && !verification && !trace) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  const summary = goal?.summary && typeof goal.summary === 'object' ? goal.summary : {};
  const objective = summary.objective?.statement || summary.objective || goal?.objective || '目标合同已建立';
  const deliverables = runtimeArray(summary.deliverables);
  const skills = runtimeArray(summary.capabilities?.skills);
  const tools = runtimeArray(summary.capabilities?.tools);
  const goalStatus = goal?.status || summary.status || '';
  const goalStatusLabels = { draft: '草拟中', needs_input: '待补充信息', confirmed: '已确认', superseded: '已更新' };
  const needsInput = taskNeedsClarification(state.currentTask, runtime) || goalStatus === 'needs_input';
  const missingInputs = runtimeArray(summary.missing_inputs).map((item) => {
    if (item && typeof item === 'object') return friendlyInputLabel(item.label || item.key || item.name || '');
    return friendlyInputLabel(item);
  }).filter(Boolean);
  const verificationState = verification?.state || verification?.status || 'not_started';
  const nodeCounts = trace?.node_status_counts && typeof trace.node_status_counts === 'object' ? trace.node_status_counts : {};
  const capabilities = runtimeArray(trace?.capabilities);
  const artifacts = trace?.artifacts && typeof trace.artifacts === 'object' ? trace.artifacts : {};
  const knowledge = trace?.knowledge && typeof trace.knowledge === 'object' ? trace.knowledge : {};
  const artifactItems = runtimeArray(artifacts.items).filter((item) => item && item.download_url).slice(0, 4);
  const current = trace?.current_node && typeof trace.current_node === 'object' ? trace.current_node : null;
  const capabilityPreview = capabilities.slice(0, 5);
  const nodeFacts = Object.entries(nodeCounts).filter(([, count]) => Number(count) > 0).map(([key, count]) => `${runtimeStatusLabel(key)} ${count}`);
  const knowledgeDocs = runtimeArray(knowledge.documents).slice(0, 3);
  box.innerHTML = `
    ${goal ? `<div class="runtime-contract-card">
      <div class="runtime-contract-head"><strong>${needsInput ? '需要补充信息' : '任务要求'}</strong><span>${escapeHtml(goalStatusLabels[goalStatus] || goalStatus || '已建立')}</span></div>
      <p>${escapeHtml(objective)}</p>
      ${needsInput && missingInputs.length ? `<small>还差：${escapeHtml(missingInputs.join('、'))}</small>` : `<small>${deliverables.length} 项交付物 · ${skills.length} 个技能 · ${tools.length} 个工具</small>`}
    </div>` : ''}
    ${verification && !needsInput ? verificationSummaryMarkup(verification.public_report, verificationState, { compact: true }) : ''}`;
  if (trace && !needsInput) {
    box.innerHTML += `<div class="runtime-trace-card">
      <div class="runtime-trace-head"><strong>执行证据</strong><span>${escapeHtml(runtimeStatusLabel(trace.active_run_status || trace.task_status || 'unknown'))}</span></div>
      <div class="runtime-trace-grid">
        <div><span>当前节点</span><strong>${escapeHtml(current?.title || current?.id || '暂无活动节点')}</strong></div>
        <div><span>运行尝试</span><strong>${Number(trace.attempts || 0)}</strong></div>
        <div><span>产物交付</span><strong>${Number(artifacts.published || 0)}/${Number(artifacts.total || 0)}</strong></div>
        <div><span>验收状态</span><strong>${escapeHtml(verificationStateLabel(trace.verification_state || verificationState))}</strong></div>
        <div><span>知识引用</span><strong>${Number(knowledge.match_count || 0)}</strong></div>
      </div>
      ${nodeFacts.length ? `<div class="runtime-trace-tags">${nodeFacts.map((item) => `<span>${escapeHtml(item)}</span>`).join('')}</div>` : ''}
      ${capabilityPreview.length ? `<div class="runtime-trace-capabilities">${capabilityPreview.map((item) => `<span class="${escapeHtml(runtimeStatusClass(item.type))}">${escapeHtml(runtimeNodeKindLabel(item.type))} · ${escapeHtml(item.id || item.label || '')}</span>`).join('')}${capabilities.length > capabilityPreview.length ? `<small>+${capabilities.length - capabilityPreview.length}</small>` : ''}</div>` : '<p>本次运行尚未记录 Skill/MCP/模型等子能力。</p>'}
      ${knowledgeDocs.length ? `<div class="runtime-trace-knowledge">${knowledgeDocs.map((item) => `<span>${escapeHtml(item.document_name || item.document_id || '知识文档')} · ${Number(item.match_count || 0)} 片段</span>`).join('')}</div>` : ''}
      ${artifactItems.length ? `<div class="runtime-trace-artifacts">${artifactItems.map((item) => {
        const downloadUrl = safeArtifactDownloadUrl(item.download_url);
        return `<a href="${escapeHtml(downloadUrl)}" download><span>${escapeHtml(item.name || item.id || '文件')}</span><small>${escapeHtml(String(item.kind || '').toUpperCase() || 'FILE')}</small></a>`;
      }).join('')}</div>` : ''}
      ${Array.isArray(artifacts.formats) && artifacts.formats.length ? `<p>文件格式：${escapeHtml(artifacts.formats.join('、'))}</p>` : ''}
    </div>`;
  }
  box.classList.remove('hidden');
}

function normalizeRuntimeNode(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const wrapped = value.node && typeof value.node === 'object' && !Array.isArray(value.node) ? value.node : value;
  const childSource = value.children ?? wrapped.children;
  return {
    ...wrapped,
    children: runtimeArray(childSource).map(normalizeRuntimeNode).filter(Boolean),
  };
}

function buildRuntimeNodeTree(runtime) {
  const projection = runtime?.node_tree;
  let projectedRoots = [];
  if (Array.isArray(projection)) projectedRoots = projection;
  else if (projection && typeof projection === 'object') {
    if (Array.isArray(projection.roots)) projectedRoots = projection.roots;
    else if (Array.isArray(projection.nodes)) projectedRoots = projection.nodes;
    else projectedRoots = [projection];
  }
  if (projectedRoots.length) return projectedRoots.map(normalizeRuntimeNode).filter(Boolean);

  const flat = runtimeArray(runtime?.nodes).map(normalizeRuntimeNode).filter(Boolean);
  const byKey = new Map();
  flat.forEach((node, index) => {
    const id = node.id || node.node_id || node.node_key || `node-${index}`;
    byKey.set(`${node.run_id || ''}:${id}`, { ...node, children: [] });
  });
  const roots = [];
  byKey.forEach((node) => {
    const parentId = node.parent_node_id || node.parent_id;
    const parent = parentId ? byKey.get(`${node.run_id || ''}:${parentId}`) : null;
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  });
  return roots;
}

function runtimeNodeCount(nodes) {
  return nodes.reduce((count, node) => count + 1 + runtimeNodeCount(runtimeArray(node.children)), 0);
}

function runtimeNodeCopy(node, index = 0) {
  const name = node.title || node.name || node.node_id || node.node_key || node.id || `节点 ${index + 1}`;
  const kind = node.kind || node.type || '执行节点';
  const attemptText = node.attempt || node.run_number ? `第 ${node.attempt || node.run_number} 次 · ` : '';
  const summary = node.error_summary || node.status_message || node.output_summary || '';
  const detail = summary || `${attemptText}${kind}`;
  const timeText = node.finished_at || node.started_at ? runtimeTime(node.finished_at || node.started_at) : '';
  const capability = node.capability && typeof node.capability === 'object'
    ? { type: node.capability.type || kind, label: `${runtimeNodeKindLabel(node.capability.type || kind)} · ${node.capability.id || node.capability.label || name}` }
    : null;
  return { name, detail, meta: timeText || runtimeStatusLabel(node.status), status: node.status || 'pending', capability };
}

function runtimeNodeKindLabel(kind) {
  return { phase: '阶段', skill: 'Skill', mcp: 'MCP', tool: '工具', model: '模型', agent: '专家', knowledge: '知识库', memory: '上下文' }[String(kind || '').toLowerCase()] || '节点';
}

function runtimeNodeContains(node, nodeId) {
  if (!nodeId) return false;
  if (String(node.id || node.node_id || '') === String(nodeId)) return true;
  return runtimeArray(node.children).some((child) => runtimeNodeContains(child, nodeId));
}

function renderRuntimeNodeTree(nodes, currentNodeId = '') {
  return nodes.map((node, index) => {
    const copy = runtimeNodeCopy(node, index);
    const children = runtimeArray(node.children);
    return `<div class="runtime-tree-root">
      ${runtimeItem(copy.name, copy.status, copy.detail, copy.meta, { active: String(node.id || '') === String(currentNodeId), capability: copy.capability })}
      ${children.length ? `<div class="runtime-tree-children">${children.map((child, childIndex) => {
        const childCopy = runtimeNodeCopy(child, childIndex);
        const nestedCount = runtimeNodeCount(runtimeArray(child.children));
        const detail = `${childCopy.detail}${nestedCount ? ` · 另含 ${nestedCount} 个明细` : ''}`;
        return runtimeItem(childCopy.name, childCopy.status, detail, childCopy.meta, { active: String(child.id || '') === String(currentNodeId), child: true, capability: childCopy.capability });
      }).join('')}</div>` : ''}
    </div>`;
  }).join('');
}

function runtimeCapabilityCallDetail(call) {
  const parts = [
    call.status_message,
    call.output_summary,
    call.error_summary,
  ].filter((item) => String(item || '').trim());
  return parts[0] || `${runtimeNodeKindLabel(call.type)} 调用记录`;
}

function renderRuntimeCapabilityCalls(runtime) {
  const trace = runtime?.trace_summary && typeof runtime.trace_summary === 'object' ? runtime.trace_summary : {};
  const calls = runtimeArray(trace.capability_calls);
  setRuntimeCount('runtimeCapabilityCallCount', calls.length);
  const box = $('runtimeCapabilityCalls');
  if (!box) return;
  box.innerHTML = calls.length ? calls.map((call, index) => {
    const title = call.label || call.title || call.id || `调用 ${index + 1}`;
    const kind = runtimeNodeKindLabel(call.type);
    const detail = `${kind} · ${call.id || '未记录标识'} · ${runtimeCapabilityCallDetail(call)}`;
    const meta = call.finished_at || call.started_at ? runtimeTime(call.finished_at || call.started_at) : runtimeStatusLabel(call.status);
    return runtimeItem(title, call.status || 'pending', detail, meta, {
      active: String(call.node_id || '') === String(runtime?.current_node?.id || ''),
      capability: { type: call.type || 'tool', label: `${kind} · ${call.id || title}` },
    });
  }).join('') : '<div class="runtime-empty">暂无 Skill/MCP/工具调用记录</div>';
}

function resetTaskRuntime() {
  if (state.runtimeTimer) clearTimeout(state.runtimeTimer);
  state.runtimeTimer = null;
  state.runtimeTaskId = null;
  state.taskRuntime = null;
  $('taskRuntime').classList.add('empty');
  $('taskRuntime').classList.remove('compact-task');
  ['runtimeRunsSection', 'runtimeNodesSection', 'runtimeCapabilitySection'].forEach((id) => { if ($(id)) $(id).open = false; });
  $('runtimeSummary').innerHTML = '<div><strong>尚未选择任务</strong><span>创建或打开任务后，可在这里控制运行并恢复现场。</span></div><span id="runtimeLiveStatus" class="runtime-live-status idle">未运行</span>';
  renderRuntimeAssurance(null);
  ['cancelTaskBtn', 'retryTaskBtn', 'resumeTaskBtn', 'runtimeMessageBtn'].forEach((id) => { $(id).disabled = true; });
  $('runtimeMessage').disabled = true;
  $('runtimeMessage').value = '';
  [['runtimeRuns', '暂无运行记录'], ['runtimeNodes', '暂无节点记录'], ['runtimeCapabilityCalls', '暂无 Skill/MCP/工具调用记录'], ['runtimeCommands', '暂无排队指令'], ['runtimeCheckpoints', '暂无可恢复检查点']].forEach(([id, label]) => {
    $(id).innerHTML = `<div class="runtime-empty">${label}</div>`;
  });
  ['runtimeRunCount', 'runtimeNodeCount', 'runtimeCapabilityCallCount', 'runtimeCommandCount', 'runtimeCheckpointCount'].forEach((id) => setRuntimeCount(id, 0));
  if ($('runtimeInspectorStatus')) $('runtimeInspectorStatus').textContent = '未运行';
}

function renderTaskRuntime(runtime) {
  if (!state.currentTask) return resetTaskRuntime();
  state.taskRuntime = runtime || {};
  const runs = runtimeArray(runtime?.runs);
  const nodeTree = buildRuntimeNodeTree(runtime);
  const nodeCount = runtimeNodeCount(nodeTree);
  const checkpoints = runtimeArray(runtime?.checkpoints);
  const commands = runtimeArray(runtime?.commands);
  const active = runtimeActiveRun(runtime);
  const currentNode = runtime?.current_node && typeof runtime.current_node === 'object' ? runtime.current_node : null;
  const currentNodeId = currentNode?.id || active?.current_node_id || '';
  const rawStatus = active?.status || state.currentTask.status || 'pending';
  const status = taskNeedsClarification(state.currentTask, runtime) && ['completed', 'succeeded'].includes(rawStatus)
    ? 'waiting_input'
    : rawStatus;
  const attempt = active?.attempt ?? active?.run_number ?? active?.number;
  const summaryTitle = active ? `${attempt ? `第 ${attempt} 次运行 · ` : ''}${runtimeStatusLabel(status)}` : `任务${runtimeStatusLabel(status)}`;
  const currentLabel = currentNode ? ` · 当前：${currentNode.title || currentNode.node_key || currentNode.id}` : '';
  const summaryDetail = `${runs.length} 次尝试 · ${nodeCount} 个节点 · ${checkpoints.length} 个检查点${currentLabel}`;
  $('taskRuntime').classList.remove('empty');
  const compactClarification = taskNeedsClarification(state.currentTask, runtime)
    && nodeCount <= 1
    && !runtimeArray(runtime?.capability_calls).length
    && !runtimeArray(runtime?.trace_summary?.capabilities).length
    && !runtimeArray(runtime?.trace_summary?.artifacts?.items).length;
  $('taskRuntime').classList.toggle('compact-task', compactClarification);
  $('runtimeSummary').innerHTML = `<div><strong>${escapeHtml(summaryTitle)}</strong><span>${escapeHtml(summaryDetail)}</span></div><span id="runtimeLiveStatus" class="runtime-live-status ${escapeHtml(runtimeStatusClass(status))}">${escapeHtml(runtimeStatusLabel(status))}</span>`;
  if ($('runtimeInspectorStatus')) $('runtimeInspectorStatus').textContent = `${runtimeStatusLabel(status)} · ${nodeCount} 个节点`;
  if (runtimeIsActive(status) || ['failed', 'cancelled', 'waiting_input', 'waiting_approval'].includes(status)) {
    const runtimeSection = $('runtimeInspectorSection');
    if (runtimeSection) runtimeSection.open = true;
    if (runtimeIsActive(status) || status === 'failed') {
      ['runtimeNodesSection', 'runtimeCapabilitySection'].forEach((id) => { if ($(id)) $(id).open = true; });
    }
  }
  renderRuntimeAssurance(runtime);

  $('cancelTaskBtn').disabled = !runtimeIsActive(status);
  $('retryTaskBtn').disabled = runtimeIsActive(status) || (!runs.length && !['failed', 'cancelled', 'interrupted', 'completed', 'succeeded'].includes(status));
  $('resumeTaskBtn').disabled = runtimeIsActive(status) || !checkpoints.length;
  $('runtimeMessage').disabled = !runtimeIsActive(status);
  $('runtimeMessageBtn').disabled = !runtimeIsActive(status);

  setRuntimeCount('runtimeRunCount', runs.length);
  $('runtimeRuns').innerHTML = runs.length ? runs.map((run, index) => {
    const number = run.attempt ?? run.run_number ?? run.number ?? index + 1;
    const times = [run.started_at ? `开始 ${runtimeTime(run.started_at)}` : '', run.finished_at ? `结束 ${runtimeTime(run.finished_at)}` : ''].filter(Boolean).join(' · ');
    return runtimeItem(`第 ${number} 次运行`, run.status, times, runtimeStatusLabel(run.status));
  }).join('') : '<div class="runtime-empty">暂无运行记录</div>';

  setRuntimeCount('runtimeNodeCount', nodeCount);
  const currentCopy = currentNode ? runtimeNodeCopy(currentNode) : null;
  const currentMarkup = currentCopy ? `<div class="runtime-current-activity"><span>正在执行</span><strong>${escapeHtml(currentCopy.name)}</strong><small>${escapeHtml(currentCopy.detail)}</small></div>` : '';
  $('runtimeNodes').innerHTML = nodeTree.length ? `${currentMarkup}${renderRuntimeNodeTree(nodeTree, currentNodeId)}` : '<div class="runtime-empty">暂无节点记录</div>';
  renderRuntimeCapabilityCalls(runtime);

  setRuntimeCount('runtimeCommandCount', commands.length);
  $('runtimeCommands').innerHTML = commands.length ? commands.map((command) => {
    const type = command.type || command.command || 'command';
    const safeMessage = type === 'message' ? (command.payload?.message || command.payload?.instruction || command.message || '') : '';
    const detail = safeMessage ? `“${String(safeMessage).slice(0, 80)}${String(safeMessage).length > 80 ? '…' : ''}”` : runtimeTime(command.created_at || command.queued_at);
    return runtimeItem(runtimeCommandLabels[type] || type, command.status || 'queued', detail, runtimeStatusLabel(command.status || 'queued'));
  }).join('') : '<div class="runtime-empty">暂无排队指令</div>';

  setRuntimeCount('runtimeCheckpointCount', checkpoints.length);
  $('runtimeCheckpoints').innerHTML = checkpoints.length ? checkpoints.map((checkpoint, index) => {
    const id = checkpoint.id || checkpoint.checkpoint_id;
    const title = checkpoint.label || checkpoint.name || `检查点 ${index + 1}`;
    const location = checkpoint.node_title || checkpoint.node_id || checkpoint.run_id || '任务现场';
    const disabled = checkpoint.restorable === false || !id || runtimeIsActive(status);
    const titleText = runtimeIsActive(status) ? '请先取消当前运行，再恢复检查点' : '从此检查点创建新的运行尝试';
    return `<div class="runtime-item runtime-checkpoint"><span class="runtime-dot checkpoint"></span><div class="runtime-item-copy"><strong>${escapeHtml(title)}</strong><small>${escapeHtml(location)}${checkpoint.created_at ? ` · ${escapeHtml(runtimeTime(checkpoint.created_at))}` : ''}</small></div><button class="runtime-restore secondary" type="button" data-checkpoint-id="${escapeHtml(id || '')}" title="${escapeHtml(titleText)}" ${disabled ? 'disabled' : ''}>恢复</button></div>`;
  }).join('') : '<div class="runtime-empty">暂无可恢复检查点</div>';
  document.querySelectorAll('[data-checkpoint-id]').forEach((button) => {
    button.onclick = () => restoreTaskCheckpoint(button.dataset.checkpointId, button);
  });
}

function renderRuntimeUnavailable(message) {
  if (!state.currentTask) return;
  $('taskRuntime').classList.remove('empty');
  $('taskRuntime').classList.remove('compact-task');
  $('runtimeSummary').innerHTML = `<div><strong>运行信息暂不可用</strong><span>${escapeHtml(message || '请稍后刷新任务')}</span></div><span id="runtimeLiveStatus" class="runtime-live-status unknown">未连接</span>`;
  if ($('runtimeInspectorStatus')) $('runtimeInspectorStatus').textContent = '读取失败 · 点击追踪';
  if ($('runtimeInspectorSection')) $('runtimeInspectorSection').open = true;
  renderRuntimeAssurance(null);
  ['cancelTaskBtn', 'retryTaskBtn', 'resumeTaskBtn', 'runtimeMessageBtn'].forEach((id) => { $(id).disabled = true; });
  $('runtimeMessage').disabled = true;
  [['runtimeRuns', '暂无运行记录'], ['runtimeNodes', '暂无节点记录'], ['runtimeCapabilityCalls', '暂无 Skill/MCP/工具调用记录'], ['runtimeCommands', '暂无排队指令'], ['runtimeCheckpoints', '暂无可恢复检查点']].forEach(([id, label]) => {
    $(id).innerHTML = `<div class="runtime-empty">${label}</div>`;
  });
  ['runtimeRunCount', 'runtimeNodeCount', 'runtimeCapabilityCallCount', 'runtimeCommandCount', 'runtimeCheckpointCount'].forEach((id) => setRuntimeCount(id, 0));
}

function renderRuntimeLoading() {
  $('taskRuntime').classList.remove('empty');
  $('runtimeSummary').innerHTML = '<div><strong>正在读取运行现场</strong><span>同步运行尝试、节点、指令和检查点…</span></div><span id="runtimeLiveStatus" class="runtime-live-status running">同步中</span>';
  if ($('runtimeInspectorStatus')) $('runtimeInspectorStatus').textContent = '同步中';
  if ($('runtimeInspectorSection')) $('runtimeInspectorSection').open = true;
  renderRuntimeAssurance(null);
  ['cancelTaskBtn', 'retryTaskBtn', 'resumeTaskBtn', 'runtimeMessageBtn'].forEach((id) => { $(id).disabled = true; });
  $('runtimeMessage').disabled = true;
  $('runtimeMessage').value = '';
  [['runtimeRuns', '正在读取运行记录'], ['runtimeNodes', '正在读取节点状态'], ['runtimeCapabilityCalls', '正在读取调用明细'], ['runtimeCommands', '正在读取指令队列'], ['runtimeCheckpoints', '正在读取检查点']].forEach(([id, label]) => {
    $(id).innerHTML = `<div class="runtime-empty">${label}</div>`;
  });
  ['runtimeRunCount', 'runtimeNodeCount', 'runtimeCapabilityCallCount', 'runtimeCommandCount', 'runtimeCheckpointCount'].forEach((id) => setRuntimeCount(id, 0));
}

function scheduleTaskRuntimeRefresh(taskId, delay = 1400) {
  if (!taskId || state.runtimeTaskId !== taskId || state.runtimeTimer) return;
  state.runtimeTimer = setTimeout(() => {
    state.runtimeTimer = null;
    loadTaskRuntime(taskId, { silent: true });
  }, delay);
}

async function loadTaskRuntime(taskId, { silent = false } = {}) {
  if (!taskId || state.currentTask?.id !== taskId) return;
  state.runtimeTaskId = taskId;
  try {
    const runtime = await api(`/api/tasks/${encodeURIComponent(taskId)}/runtime`);
    if (state.currentTask?.id !== taskId) return;
    renderTaskRuntime(runtime);
    if (runtimeIsActive(currentRuntimeStatus(runtime))) scheduleTaskRuntimeRefresh(taskId);
  } catch (err) {
    if (state.currentTask?.id !== taskId) return;
    renderRuntimeUnavailable(err.status === 404 ? '该任务尚未建立可恢复运行记录' : (err.message || '读取失败'));
    if (!silent) notify(`运行信息加载失败：${err.message || err}`, 'error');
    if (runtimeIsActive(state.currentTask?.status)) scheduleTaskRuntimeRefresh(taskId, 2500);
  }
}

function watchTaskRuntime(taskId) {
  if (state.runtimeTimer) clearTimeout(state.runtimeTimer);
  state.runtimeTimer = null;
  state.runtimeTaskId = taskId;
  state.taskRuntime = null;
  renderRuntimeLoading();
  return loadTaskRuntime(taskId, { silent: true });
}

function runtimeCommandError(type, err) {
  if (err.status === 409) return '任务当前状态不允许此操作，请刷新后重试';
  if (err.status === 404) return type === 'restore_checkpoint' ? '检查点不存在或已失效' : '任务不存在或运行记录尚未建立';
  if (err.status === 422 || err.status === 400) return err.message || '提交内容不完整，请检查后重试';
  return err.message || '平台暂时无法处理该操作，请稍后重试';
}

async function sendTaskRuntimeCommand(type, payload = {}, button = null) {
  const taskId = state.currentTask?.id;
  if (!taskId) return notify('请先创建或打开一个任务', 'error');
  if (button) setBusy(button, true, type === 'message' ? '排队中…' : '处理中…');
  try {
    try {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/commands`, {
        method: 'POST', body: JSON.stringify({ type, payload }),
      });
    } catch (err) {
      if (![404, 405].includes(err.status)) throw err;
      let fallbackPath = '';
      if (['cancel', 'retry', 'resume'].includes(type)) fallbackPath = `/api/tasks/${encodeURIComponent(taskId)}/${type}`;
      if (type === 'restore_checkpoint') fallbackPath = `/api/tasks/${encodeURIComponent(taskId)}/checkpoints/${encodeURIComponent(payload.checkpoint_id)}/restore`;
      if (!fallbackPath) throw err;
      await api(fallbackPath, { method: 'POST', body: JSON.stringify(payload) });
    }
    notify({ cancel: '已提交取消请求', retry: '已创建新的运行尝试', resume: '已提交恢复请求', message: '追加指令已加入队列', restore_checkpoint: '已提交检查点恢复请求' }[type] || '操作已提交');
    if (['retry', 'resume', 'restore_checkpoint'].includes(type)) startTaskStream(taskId);
    await refreshCurrentTask(taskId);
    await loadTaskRuntime(taskId, { silent: true });
    return true;
  } catch (err) {
    notify(runtimeCommandError(type, err), 'error');
    return false;
  } finally {
    if (button) setBusy(button, false);
    if (state.taskRuntime && state.currentTask?.id === taskId) renderTaskRuntime(state.taskRuntime);
  }
}

async function submitRuntimeMessage() {
  const input = $('runtimeMessage');
  const message = input.value.trim();
  if (!message) return notify('请先填写要追加的指令', 'error');
  const accepted = await sendTaskRuntimeCommand('message', { message }, $('runtimeMessageBtn'));
  if (accepted) input.value = '';
}

async function restoreTaskCheckpoint(checkpointId, button) {
  if (!checkpointId) return;
  if (!confirm('恢复后将从该检查点创建新的运行尝试，确定继续吗？')) return;
  await sendTaskRuntimeCommand('restore_checkpoint', { checkpoint_id: checkpointId }, button);
}

const taskTerminalStatuses = new Set(['completed', 'failed', 'cancelled']);
const privateTaskEventTypes = new Set(['analysis', 'reasoning', 'thought', 'thinking']);
const streamRetryDelays = [500, 1000, 2000, 4000, 8000];

function stopTaskStream({ clearCursor = true } = {}) {
  state.streamGeneration += 1;
  if (state.eventSource) state.eventSource.close();
  if (state.streamRetryTimer) clearTimeout(state.streamRetryTimer);
  state.eventSource = null;
  state.streamRetryTimer = null;
  state.streamRetryCount = 0;
  state.streamTaskId = null;
  if (clearCursor) {
    state.streamCursor = 0;
    state.seenEventIds = new Set();
  }
}

function taskEventId(event, payload) {
  const value = Number(payload?.id ?? event.lastEventId);
  return Number.isSafeInteger(value) && value > 0 ? value : 0;
}

function updateTaskUiFromEvent(taskId, payload = {}) {
  if (!taskId || state.currentTask?.id !== taskId || !state.taskUiRunning) return;
  const type = String(payload.type || '').toLowerCase();
  const statusMap = {
    start: ['正在理解任务…', 'thinking'], intent: ['正在理解你的目标…', 'thinking'],
    plan: ['正在制定执行计划…', 'thinking'], plan_progress: ['正在执行计划…', 'thinking'],
    skill: ['正在匹配技能…', 'thinking'], model: ['正在调用模型…', 'thinking'],
    knowledge: ['正在检索相关资料…', 'thinking'], tool_call: ['正在调用工具…', 'thinking'],
    tool_result: ['工具已返回，正在整理结果…', 'thinking'], progress: ['正在执行任务…', 'thinking'],
    plan_check: ['正在校验调用参数…', 'thinking'], output_check: ['正在检查输出质量…', 'verifying'],
    verification_started: ['正在验收生成结果…', 'verifying'], verification_result: ['正在发布验收通过的结果…', 'verifying'],
    answer_delta: ['正在输出结果…', 'outputting'], answer: ['正在完成最终答复…', 'outputting'],
    approval_required: ['等待你的确认…', 'verifying'], clarification: ['等待你补充信息…', 'verifying'],
    error: ['任务执行失败', 'error'], cancelled: ['任务已停止', 'done'], done: ['已完成', 'done'],
  };
  const next = statusMap[type];
  if (next) setTaskUiStatus(taskId, next[0], next[1]);
  if (['approval_required', 'clarification'].includes(type)) {
    state.taskUiRunning = false;
    state.taskUiCancelRequested = false;
    setSendButtonState('idle');
  }
  if (['done', 'error', 'cancelled'].includes(type)) finishTaskUi(taskId, type === 'done' ? 'completed' : type);
}

function handleTaskStreamEvent(taskId, event) {
  let payload;
  try {
    payload = JSON.parse(event.data);
  } catch (_) {
    return;
  }
  const eventId = taskEventId(event, payload);
  if (eventId && (eventId <= state.streamCursor || state.seenEventIds.has(eventId))) return;
  if (eventId) {
    state.streamCursor = eventId;
    state.seenEventIds.add(eventId);
  }
  updateAgentThinkingEvent(taskId, payload);
  updateTaskUiFromEvent(taskId, payload);
  if (privateTaskEventTypes.has(String(payload.type || '').toLowerCase())) return;
  scheduleTaskRuntimeRefresh(taskId, 280);
  if (!['answer_delta', 'answer_reset', 'approval_required'].includes(payload.type)) appendEvent(payload);
  if (payload.type === 'start' && state.currentTask?.id === taskId) {
    state.currentTask.status = 'running';
    renderTaskMeta(state.currentTask);
  }
  if (payload.type === 'answer_reset') resetStreamedAnswer(taskId);
  if (payload.type === 'answer_delta') appendAnswerDelta(taskId, payload.content || '');
  if (payload.type === 'clarification') publishClarification(taskId, payload);
  if (payload.type === 'verification_started') markDraftVerificationStarted(taskId);
  if (payload.type === 'verification_result') markDraftVerificationResult(taskId, payload);
  if (payload.type === 'answer') publishStreamedAnswer(taskId, payload);
  if (payload.type === 'error') {
    markDraftFailed(taskId, payload);
    publishTaskError(taskId, payload);
  }
  if (payload.type === 'approval_required') renderApproval(taskId, payload);
  if (payload.type === 'install') {
    loadSkillsOnly().catch((err) => notify(`技能列表刷新失败：${err.message || err}`, 'error'));
  }
  if (['done', 'error', 'cancelled'].includes(payload.type)) {
    refreshCurrentTask(taskId);
    loadTasksOnly();
  }
}

function connectTaskStream(taskId, generation) {
  if (generation !== state.streamGeneration || state.streamTaskId !== taskId || state.currentTask?.id !== taskId) return;
  const cursor = state.streamCursor > 0 ? `?cursor=${state.streamCursor}` : '';
  const source = new EventSource(`/api/tasks/${encodeURIComponent(taskId)}/events/stream${cursor}`);
  state.eventSource = source;
  source.addEventListener('task_event', (event) => {
    if (generation !== state.streamGeneration || state.eventSource !== source || state.currentTask?.id !== taskId) return;
    state.streamRetryCount = 0;
    handleTaskStreamEvent(taskId, event);
  });
  source.addEventListener('task_status', (event) => {
    if (generation !== state.streamGeneration || state.eventSource !== source) return;
    let payload = {};
    try { payload = JSON.parse(event.data); } catch (_) {}
    if (state.currentTask?.id === taskId && payload.status) {
      state.currentTask.status = payload.status;
      renderTaskMeta(state.currentTask);
    }
    if (payload.status && ['completed', 'failed', 'cancelled', 'waiting_approval'].includes(payload.status)) {
      finishTaskUi(taskId, payload.status);
    }
    source.close();
    if (state.eventSource === source) state.eventSource = null;
    if (payload.terminal || payload.status === 'waiting_approval') {
      refreshCurrentTask(taskId);
      loadTasksOnly();
    }
  });
  source.onerror = () => {
    if (generation !== state.streamGeneration || state.eventSource !== source) return;
    source.close();
    state.eventSource = null;
    if (state.currentTask?.id !== taskId || taskTerminalStatuses.has(state.currentTask?.status)) return;
    if (state.streamRetryCount >= streamRetryDelays.length) {
      refreshCurrentTask(taskId);
      return;
    }
    const delay = streamRetryDelays[state.streamRetryCount++];
    state.streamRetryTimer = setTimeout(() => {
      state.streamRetryTimer = null;
      connectTaskStream(taskId, generation);
    }, delay);
  };
}

function startTaskStream(taskId, { seenEventIds = null } = {}) {
  const sameTask = state.streamTaskId === taskId;
  const previousCursor = sameTask ? state.streamCursor : 0;
  const previousSeen = sameTask
    ? state.seenEventIds
    : new Set((seenEventIds || []).map(Number).filter((id) => Number.isSafeInteger(id) && id > 0));
  stopTaskStream({ clearCursor: false });
  state.streamTaskId = taskId;
  state.streamCursor = previousCursor;
  state.seenEventIds = previousSeen;
  const generation = state.streamGeneration;
  connectTaskStream(taskId, generation);
}

window.addEventListener('beforeunload', () => stopTaskStream());

function resetStreamedAnswer(taskId) {
  const streamed = $('conversation').querySelector(`[data-stream-task="${taskId}"]`);
  if (streamed) streamed.remove();
}

function appendAnswerDelta(taskId, content) {
  let message = $('conversation').querySelector(`[data-stream-task="${taskId}"]`);
  if (!message) {
    message = document.createElement('div');
    message.className = 'message agent streaming draft-answer';
    message.dataset.streamTask = taskId;
    message.dataset.deliveryState = 'draft_unverified';
    message.dataset.rawContent = '';
    $('conversation').appendChild(message);
  }
  message.dataset.rawContent = (message.dataset.rawContent || '') + content;
  message.dataset.deliveryState = 'draft_unverified';
  message.classList.add('streaming', 'draft-answer');
  message.classList.remove('verifying', 'draft-failed');
  const count = message.dataset.rawContent.length;
  message.innerHTML = `<div class="stream-meta"><span></span><strong>草稿 · 待验收</strong><small>实时生成 · ${count} 字</small></div><div class="stream-body">${renderMarkdown(stripInternalAnswerMarkers(message.dataset.rawContent))}</div>`;
  $('conversation').scrollTop = $('conversation').scrollHeight;
}

function markDraftVerificationStarted(taskId) {
  const streamed = $('conversation').querySelector(`[data-stream-task="${taskId}"]`);
  if (!streamed) return;
  streamed.dataset.deliveryState = 'verifying';
  streamed.classList.remove('streaming', 'draft-failed');
  streamed.classList.add('draft-answer', 'verifying');
  const meta = streamed.querySelector('.stream-meta');
  if (meta) meta.innerHTML = '<span></span><strong>正在验收</strong><small>通过后才会正式发布</small>';
}

function markDraftVerificationResult(taskId, payload) {
  const streamed = $('conversation').querySelector(`[data-stream-task="${taskId}"]`);
  if (!streamed) return;
  const report = publicVerificationReport(payload?.data?.report || payload?.data?.public_report);
  const passed = report?.passed === true || payload?.data?.delivery_state === 'verified';
  streamed.dataset.deliveryState = passed ? 'verified_pending_publish' : 'rejected';
  streamed.classList.remove('streaming', 'verifying');
  streamed.classList.toggle('draft-failed', !passed);
  const meta = streamed.querySelector('.stream-meta');
  if (meta) {
    meta.innerHTML = passed
      ? '<span></span><strong>验收通过</strong><small>等待正式发布</small>'
      : '<span></span><strong>验收未通过</strong><small>此草稿不会发布</small>';
  }
}

function markDraftFailed(taskId) {
  const streamed = $('conversation').querySelector(`[data-stream-task="${taskId}"]`);
  if (!streamed) return;
  streamed.dataset.deliveryState = 'error_unpublished';
  streamed.classList.remove('streaming', 'verifying');
  streamed.classList.add('draft-answer', 'draft-failed');
  const meta = streamed.querySelector('.stream-meta');
  if (meta) meta.innerHTML = '<span></span><strong>任务失败</strong><small>草稿未发布</small>';
}

function publishClarification(taskId, payload = {}) {
  const id = String(payload.id || '');
  const taskKey = String(taskId || '');
  const content = localizeMissingInformationText(payload.content || payload.title || '请补充完成任务所需的信息。');
  const existing = [...$('conversation').querySelectorAll('[data-clarification-task]')]
    .find((item) => item.dataset.clarificationTask === taskKey);
  if (existing) {
    if (id) existing.dataset.eventId = id;
    return;
  }
  // The backend may keep an answer event for auditability even when the task
  // ended in needs-input. Replace that answer in the conversation so the
  // clarification prompt appears only once.
  [...$('conversation').querySelectorAll('[data-task-message]')]
    .filter((item) => item.dataset.taskMessage === taskKey)
    .forEach((item) => item.remove());
  [...$('conversation').querySelectorAll('[data-stream-task]')]
    .filter((item) => item.dataset.streamTask === taskKey)
    .forEach((item) => item.remove());
  const message = document.createElement('div');
  message.className = 'message agent clarification-message';
  message.dataset.clarificationTask = taskKey;
  if (id) message.dataset.eventId = id;
  message.innerHTML = `<div class="clarification-meta"><span>?</span><strong>需要补充信息</strong><small>补充后会继续当前任务</small></div><div class="clarification-body">${renderMarkdown(content)}</div>`;
  $('conversation').appendChild(message);
  $('conversation').scrollTop = $('conversation').scrollHeight;
}

function localizeMissingInformationText(value) {
  return String(value || '')
    .replace(/product\s+description/gi, '产品描述')
    .replace(/programming\s+language/gi, '编程语言')
    .replace(/selected\s+language/gi, '编程语言')
    .replace(/preferred\s+language/gi, '编程语言');
}

function looksLikeClarificationResponse(value) {
  const text = String(value || '').trim();
  return /^(?:在安排专家协作前，)?(?:还需要你补充|还差一个信息|请补充)/.test(text)
    && /(产品描述|编程语言|product\s+description|programming\s+language|selected\s+language|preferred\s+language|必要信息|信息)/i.test(text);
}

function publishTaskError(taskId, payload = {}) {
  const taskKey = String(taskId || '');
  const existing = [...$('conversation').querySelectorAll('[data-task-error]')]
    .find((item) => item.dataset.taskError === taskKey);
  if (existing) {
    if (payload.content) renderMessageContent(existing.querySelector('.error-body') || existing, payload.content);
    return;
  }
  const message = document.createElement('div');
  message.className = 'message agent task-error-message';
  message.dataset.taskError = taskKey;
  if (payload.id) message.dataset.eventId = String(payload.id);
  const pptxSetup = payload.data?.error_code === 'artifact_pptx_unavailable' || payload.error_code === 'artifact_pptx_unavailable';
  message.innerHTML = `<div class="error-meta"><span>!</span><strong>${escapeHtml(payload.title || '任务未完成')}</strong><small>可以检查配置后重试</small></div><div class="error-body">${renderMarkdown(payload.content || '任务执行未完成，请检查模型、参数或工具配置后重试。')}</div>${pptxSetup ? '<button class="text-button capability-action" data-open-pptx-config type="button">打开完整 PPTX 配置向导</button>' : ''}`;
  $('conversation').appendChild(message);
  if ($('taskOverviewSection')) $('taskOverviewSection').open = true;
  if ($('runtimeInspectorSection')) $('runtimeInspectorSection').open = true;
  if ($('timelineSection')) $('timelineSection').open = true;
  if ($('timelineStatus')) $('timelineStatus').textContent = '执行失败 · 点击追踪';
  $('conversation').scrollTop = $('conversation').scrollHeight;
}

function publishStreamedAnswer(taskId, payload) {
  // A clarification is the user-facing response for a needs-input run. Some
  // older runtimes also emit an answer event containing the same text; keep
  // the conversation from showing that prompt twice.
  if ([...$('conversation').querySelectorAll('[data-clarification-task]')]
    .some((item) => item.dataset.clarificationTask === String(taskId || ''))) return;
  const streamed = $('conversation').querySelector(`[data-stream-task="${taskId}"]`);
  if (streamed) {
    renderMessageContent(streamed, payload.content || payload.title);
    streamed.classList.remove('streaming', 'draft-answer', 'verifying', 'draft-failed');
    streamed.classList.add('verified-answer');
    delete streamed.dataset.streamTask;
    delete streamed.dataset.rawContent;
    delete streamed.dataset.deliveryState;
    streamed.dataset.eventId = String(payload.id);
  } else if (!$('conversation').querySelector(`[data-event-id="${payload.id}"]`)) {
    const message = addMessage('agent', payload.content || payload.title, payload.id);
    message.dataset.taskMessage = String(taskId || '');
  }
}

const taskEventLabels = {
  start: '开始执行', intent: '目标理解', plan: '执行计划', plan_progress: '计划进度',
  skill: '技能匹配', model: '模型调用', progress: '执行进度', plan_check: '调用校验',
  tool_call: '调用工具', tool_result: '工具结果', tool_error: '工具失败',
  tool_blocked: '工具阻止',
  output_check: '结果验收', answer: '最终答复', done: '执行完成', error: '执行失败',
  approval_required: '等待确认', install: '能力安装', checkpoint: '检查点',
  knowledge: '知识引用',
  expert_selection: '专家选择', team_queued: '专家团排队', team_parallel_start: '专家并行',
  team_aggregating: '主管汇总', team_completed: '协作完成', team_partial_failed: '部分失败',
  team_member_retry: '成员重试', policy_decision: '权限决策',
  verification_started: '正在验收', verification_result: '验收结果', candidate_verified: '候选已验收',
  clarification: '需要补充信息',
};

function taskEventLabel(type) {
  return taskEventLabels[type] || '执行事件';
}

function expertSelectionMarkup(event) {
  const data = event?.data && typeof event.data === 'object' ? event.data : {};
  const members = Array.isArray(data.members) ? data.members : [];
  const automatic = data.selection_mode === 'automatic';
  const matchedTerms = Array.isArray(data.matched_terms) ? data.matched_terms.filter(Boolean).slice(0, 5) : [];
  const matchText = automatic && matchedTerms.length ? ` · 命中：${matchedTerms.join('、')}` : '';
  const supervisor = data.supervisor?.agent_name || data.supervisor?.agent_id || '';
  const participantMarkup = members.map((member) => {
    const name = member.agent_name || member.name || member.agent_id || '专家';
    const role = member.role || '独立分析';
    return `<div class="expert-participant"><span class="expert-participant-avatar">${escapeHtml(String(name).slice(0, 1))}</span><span><strong>${escapeHtml(name)}</strong><small>${escapeHtml(role)}</small></span></div>`;
  }).join('');
  return `
    <div id="expertSelectionSummary" class="expert-selection-card">
      <div class="expert-selection-head">
        <div class="expert-selection-title">
          <span class="expert-selection-mark"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="8" r="2.7"/><circle cx="17" cy="9" r="2.3"/><path d="M3.8 18.5c.5-2.9 1.9-4.5 4.2-4.5s3.7 1.6 4.2 4.5M13.3 18c.4-2.3 1.6-3.7 3.7-3.7s3.3 1.4 3.7 3.7"/></svg></span>
          <span><strong>${escapeHtml(data.team_name || data.team_id || '专家团')}</strong><small>${supervisor ? `主管：${escapeHtml(supervisor)}` : '成员独立分析，主管统一汇总'}${escapeHtml(matchText)}</small></span>
        </div>
        <span class="expert-selection-mode">${automatic ? '自动匹配' : '手动指定'}</span>
      </div>
      <div class="expert-selection-reason">${escapeHtml(data.reason || event?.content || '已根据当前任务选择参与专家。')}</div>
      ${participantMarkup ? `<div class="expert-participants">${participantMarkup}</div>` : ''}
    </div>`;
}

function renderExpertSelectionEvent(event) {
  state.currentExpertSelection = event;
  const current = $('expertSelectionSummary');
  if (current) {
    current.outerHTML = expertSelectionMarkup(event);
  } else if ($('executionPlan')) {
    $('executionPlan').insertAdjacentHTML('afterbegin', expertSelectionMarkup(event));
  } else {
    $('timeline').insertAdjacentHTML('beforeend', expertSelectionMarkup(event));
  }
  if (state.workbenchMode === 'expert' && $('expertTeamSelect')) {
    const automatic = event.data?.selection_mode === 'automatic';
    const teamId = event.data?.team_id || '';
    $('expertTeamSelect').value = automatic ? '' : (enabledWorkbenchTeams().some((team) => team.id === teamId) ? teamId : '');
    renderWorkbenchMode();
  }
}

function renderVerificationResultEvent(event) {
  const data = event?.data && typeof event.data === 'object' ? event.data : {};
  const report = publicVerificationReport(data.report || data.public_report);
  const stateValue = data.delivery_state === 'verified' || report?.passed === true ? 'passed' : report?.passed === false ? 'failed' : (data.state || 'inconclusive');
  const verificationId = String(data.verification_id || 'latest');
  const existing = [...$('timeline').querySelectorAll('[data-verification-summary]')]
    .find((item) => item.dataset.verificationSummary === verificationId);
  const div = existing || document.createElement('div');
  div.className = 'event verification-event';
  div.dataset.verificationSummary = verificationId;
  div.innerHTML = `
    <div class="event-title">
      <span>最终验收报告</span>
      <span class="badge verification_result ${escapeHtml(verificationStateClass(stateValue))}">${escapeHtml(verificationStateLabel(stateValue))}</span>
    </div>
    ${verificationSummaryMarkup(report, stateValue)}
  `;
  if (!existing) $('timeline').appendChild(div);

  if (report && Array.isArray(report.rules)) {
    const criteria = $('acceptanceCriteria');
    if (criteria) {
      criteria.innerHTML = report.rules.map((item) => renderAcceptanceCriterion({
        id: item.id,
        title: item.title,
        status: item.status,
        detail: item.public_reason,
      })).join('');
    }
    const summary = $('acceptanceSummary');
    if (summary) {
      const failed = report.rules.filter((item) => item.status === 'failed').length;
      summary.textContent = report.passed ? `全部 ${report.rules.length} 项通过` : `${failed} 项未通过`;
      summary.className = report.passed ? 'passed' : 'failed';
    }
  }
  updateAcceptanceDeliveryPanel({ event, report });
  $('timeline').scrollTop = $('timeline').scrollHeight;
}

function renderKnowledgeEvent(event) {
  const data = event?.data && typeof event.data === 'object' ? event.data : {};
  const matches = runtimeArray(data.matches);
  const docs = new Map();
  matches.forEach((item) => {
    const key = item.document_id || item.document_name || item.chunk_id || 'unknown';
    const current = docs.get(key) || { name: item.document_name || item.document_id || '知识文档', count: 0, ordinals: [] };
    current.count += 1;
    if (item.ordinal !== undefined && item.ordinal !== null) current.ordinals.push(Number(item.ordinal) + 1);
    docs.set(key, current);
  });
  const docItems = [...docs.values()].slice(0, 6);
  const div = document.createElement('div');
  div.className = 'event knowledge-event';
  div.innerHTML = `
    <div class="event-title">
      <span>${escapeHtml(event.title || '已检索知识库')}</span>
      <span class="badge knowledge">知识引用</span>
    </div>
    ${event.content ? `<div class="event-content">${escapeHtml(event.content)}</div>` : ''}
    ${docItems.length ? `<div class="knowledge-event-list">${docItems.map((item) => `<span><strong>${escapeHtml(item.name)}</strong><small>${Number(item.count || 0)} 个片段${item.ordinals.length ? ` · #${item.ordinals.slice(0, 4).join(' #')}` : ''}</small></span>`).join('')}</div>` : ''}
  `;
  $('timeline').appendChild(div);
  $('timeline').scrollTop = $('timeline').scrollHeight;
}

function appendEvent(event) {
  if (['analysis', 'reasoning', 'thought', 'thinking'].includes(event.type)) return;
  if (['answer_delta', 'answer_reset'].includes(event.type)) return;
  if (['checkpoint', 'permissions', 'agent', 'memory', 'goal_spec_progress', 'goal_spec'].includes(event.type)) return;
  if (event.type === 'policy_decision' && event.data?.outcome === 'allow') return;
  if ($('timelineStatus')) $('timelineStatus').textContent = `${taskEventLabel(event.type)} · ${event.title || '执行记录'}`;
  if ($('timelineSection') && !['answer', 'done'].includes(event.type)) $('timelineSection').open = true;
  if (event.type === 'error' && $('timelineSection')) $('timelineSection').open = true;
  if (event.type === 'expert_selection') {
    renderExpertSelectionEvent(event);
    return;
  }
  if (event.type === 'plan') {
    renderExecutionPlan(event);
    return;
  }
  if (event.type === 'plan_progress') {
    updateExecutionProgress(event);
    return;
  }
  if (event.type === 'verification_result') {
    renderVerificationResultEvent(event);
    return;
  }
  if (event.type === 'clarification') {
    publishClarification(event.task_id || state.currentTask?.id, event);
  }
  if (event.type === 'error') {
    publishTaskError(event.task_id || state.currentTask?.id, event);
  }
  if (event.type === 'knowledge') {
    renderKnowledgeEvent(event);
    return;
  }
  if ($('executionPlan') && ['intent', 'skill', 'model', 'knowledge', 'plan_check', 'tool_call', 'tool_result', 'tool_error', 'tool_blocked', 'output_check', 'progress'].includes(event.type)) {
    appendPlanDetail(event);
    if (!['tool_blocked'].includes(event.type)) return;
  }
  if ($('executionPlan') && ['answer', 'done'].includes(event.type)) {
    return;
  }
  const div = document.createElement('div');
  div.className = `event ${escapeHtml(event.type)}-event`;
  const title = event.type === 'verification_started' ? '正在验收' : (event.title || taskEventLabel(event.type));
  const toolSummary = event.type === 'tool_blocked' && event.data?.server_id && event.data?.tool_name
    ? `<div class="event-facts"><span>工具</span><strong>${escapeHtml(`${event.data.server_id}.${event.data.tool_name}`)}</strong><span>原因</span><strong>${escapeHtml(event.data.reason || '未通过校验')}</strong></div>`
    : '';
  div.innerHTML = `
    <div class="event-title">
      <span>${escapeHtml(title)}</span>
      <span class="badge ${escapeHtml(event.type)}">${escapeHtml(taskEventLabel(event.type))}</span>
    </div>
    ${event.content ? `<div class="event-content">${escapeHtml(event.content)}</div>` : ''}
    ${toolSummary}
  `;
  $('timeline').appendChild(div);
  $('timeline').scrollTop = $('timeline').scrollHeight;
}

const planStatusLabels = {
  pending: '等待中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
};

function renderExecutionPlan(event) {
  const plan = event.data?.plan || {};
  const nodes = Array.isArray(plan.nodes) ? plan.nodes : [];
  const confirmation = plan.goal_confirmation || {};
  const criteria = Array.isArray(plan.acceptance_criteria) ? plan.acceptance_criteria : [];
  $('timeline').innerHTML = `
    <div id="executionPlan" class="execution-tree" data-tool-node-id="${escapeHtml(plan.tool_node_id || 'execute')}">
      ${state.currentExpertSelection ? expertSelectionMarkup(state.currentExpertSelection) : ''}
      <div class="execution-goal">
        <div><span>目标</span>${escapeHtml(plan.goal || event.content || '')}</div>
        ${confirmation.label ? `<div class="goal-confirmation ${escapeHtml(confirmation.status || '')}"><b>✓</b><strong>${escapeHtml(confirmation.label)}</strong><small>${escapeHtml(confirmation.message || '')}</small></div>` : ''}
      </div>
      <div class="execution-nodes">
        ${nodes.map((node, index) => renderPlanNode(node, index)).join('')}
      </div>
      ${criteria.length ? `<div class="acceptance-panel"><div class="acceptance-title"><span>最终验收标准</span><small id="acceptanceSummary">等待执行完成后逐项检查</small></div><div id="acceptanceCriteria">${criteria.map(renderAcceptanceCriterion).join('')}</div></div>` : ''}
    </div>
  `;
  $('executionPlan').querySelectorAll('.plan-node-head').forEach((head) => {
    head.onclick = () => {
      const node = head.closest('.plan-node');
      if (node.classList.contains('has-children')) node.classList.toggle('expanded');
    };
  });
}

function renderPlanNode(node, index) {
  const children = Array.isArray(node.children) ? node.children : [];
  const status = node.status || 'pending';
  return `
    <div class="plan-node ${escapeHtml(status)} ${children.length ? 'has-children' : ''}" data-node-id="${escapeHtml(node.id)}">
      <button class="plan-node-head" type="button">
        <span class="plan-node-index">${index + 1}</span>
        <span class="plan-node-copy"><strong>${escapeHtml(node.title)}</strong><small class="plan-node-message">${planStatusLabels[status]}</small></span>
        <span class="plan-status">${planStatusLabels[status]}</span>
        ${children.length ? '<span class="plan-chevron">⌄</span>' : ''}
      </button>
      ${children.length ? `<div class="plan-node-children">${children.map(renderPlanChild).join('')}</div>` : ''}
      <div class="plan-node-details"></div>
    </div>
  `;
}

function renderAcceptanceCriterion(item) {
  const status = item.status || 'pending';
  const symbol = status === 'passed' ? '✓' : status === 'failed' ? '!' : '·';
  return `<div class="acceptance-item ${escapeHtml(status)}" data-criterion-id="${escapeHtml(item.id || '')}"><span>${symbol}</span><div><strong>${escapeHtml(item.title || '')}</strong>${item.detail ? `<small>${escapeHtml(item.detail)}</small>` : ''}</div></div>`;
}

function acceptanceArtifactItems(event = null) {
  const eventArtifacts = runtimeArray(event?.data?.artifacts);
  const taskArtifacts = runtimeArray(state.currentTask?.artifacts);
  const byId = new Map();
  [...taskArtifacts, ...eventArtifacts].forEach((item) => {
    if (!item || typeof item !== 'object') return;
    const key = item.id || item.download_url || item.name;
    if (key) byId.set(String(key), item);
  });
  return [...byId.values()];
}

function renderAcceptanceDeliveryArtifacts(artifacts) {
  return artifacts.map((item) => {
    const downloadUrl = safeArtifactDownloadUrl(item.download_url);
    const previewUrl = safeArtifactPreviewUrl(item.preview_url);
    const kind = String(item.kind || item.format || 'file').toUpperCase();
    return `
      <div class="acceptance-artifact">
        <span><strong>${escapeHtml(item.name || item.id || '生成文件')}</strong><small>${escapeHtml(kind)}${item.delivery_status ? ` · ${escapeHtml(item.delivery_status)}` : ''}</small></span>
        <span class="acceptance-artifact-actions">
          ${previewUrl ? `<a href="${escapeHtml(previewUrl)}" target="_blank" rel="noopener noreferrer">预览</a>` : ''}
          ${downloadUrl ? `<a href="${escapeHtml(downloadUrl)}" target="_blank" rel="noopener noreferrer" download>下载</a>` : '<em>等待发布</em>'}
        </span>
      </div>
    `;
  }).join('');
}

function updateAcceptanceDeliveryPanel({ event = null, report = null } = {}) {
  const tree = $('executionPlan');
  const panel = tree?.querySelector('.acceptance-panel');
  if (!panel) return;
  let delivery = $('acceptanceDelivery');
  if (!delivery) {
    panel.insertAdjacentHTML('beforeend', '<div id="acceptanceDelivery" class="acceptance-delivery"></div>');
    delivery = $('acceptanceDelivery');
  }
  const data = event?.data && typeof event.data === 'object' ? event.data : {};
  const artifacts = acceptanceArtifactItems(event);
  const expectedFormat = data.expected_format || '';
  const reportedCount = Number(data.artifact_count || artifacts.length || 0);
  const summary = $('acceptanceSummary');
  const existingPassed = delivery.classList.contains('passed') || summary?.classList.contains('passed');
  const existingFailed = delivery.classList.contains('failed') || summary?.classList.contains('failed');
  const reportPassed = report?.passed === true || data.passed === true || (!event && existingPassed);
  const reportFailed = report?.passed === false || data.passed === false || (!event && existingFailed);
  const statusText = reportPassed ? '交付校验通过' : reportFailed ? '交付校验未通过' : '等待交付校验';
  const countText = reportedCount ? `${reportedCount} 个产物` : '暂无产物';
  delivery.className = `acceptance-delivery ${reportPassed ? 'passed' : reportFailed ? 'failed' : 'pending'}`;
  delivery.innerHTML = `
    <div class="acceptance-delivery-head">
      <span>交付物</span>
      <strong>${escapeHtml(statusText)} · ${escapeHtml(countText)}${expectedFormat ? ` · ${escapeHtml(String(expectedFormat).toUpperCase())}` : ''}</strong>
    </div>
    ${artifacts.length ? `<div class="acceptance-artifacts">${renderAcceptanceDeliveryArtifacts(artifacts)}</div>` : `<p>${reportedCount ? '产物已生成，任务完成刷新后会显示下载入口。' : '当前任务还没有生成可下载产物。'}</p>`}
  `;
}

function renderPlanChild(child) {
  const status = child.status || 'pending';
  const kind = { agent: '专家', skill: '技能', mcp: '工具', tool: '工具', model: '模型', detail: '明细' }[child.kind] || '明细';
  return `
    <div class="plan-child ${escapeHtml(status)}" data-child-id="${escapeHtml(child.id)}">
      <span class="plan-dot"></span>
      <span class="plan-child-copy"><strong>${escapeHtml(child.title)}</strong><small>${escapeHtml(kind)}</small></span>
      <span class="plan-child-status">${planStatusLabels[status]}</span>
    </div>
  `;
}

function updateExecutionProgress(event) {
  const data = event.data || {};
  const tree = $('executionPlan');
  if (!tree) return;
  const node = [...tree.querySelectorAll('.plan-node')].find((item) => item.dataset.nodeId === data.node_id);
  if (!node) return;
  const status = data.status || 'running';
  if (data.child_id) {
    let children = node.querySelector('.plan-node-children');
    if (!children) {
      children = document.createElement('div');
      children.className = 'plan-node-children';
      node.appendChild(children);
      node.classList.add('has-children');
      const head = node.querySelector('.plan-node-head');
      if (!head.querySelector('.plan-chevron')) head.insertAdjacentHTML('beforeend', '<span class="plan-chevron">⌄</span>');
    }
    let child = [...children.querySelectorAll('.plan-child')].find((item) => item.dataset.childId === data.child_id);
    if (!child) {
      children.insertAdjacentHTML('beforeend', renderPlanChild({ id: data.child_id, title: data.child_title || data.child_id, kind: data.child_kind, status }));
      child = [...children.querySelectorAll('.plan-child')].find((item) => item.dataset.childId === data.child_id);
    }
    setPlanElementStatus(child, status, '.plan-child-status');
    node.classList.add('expanded');
    if (status === 'failed') setPlanElementStatus(node, 'failed', '.plan-status');
    else if (!node.classList.contains('completed')) setPlanElementStatus(node, 'running', '.plan-status');
  } else {
    setPlanElementStatus(node, status, '.plan-status');
    if (status === 'running') node.classList.add('expanded');
  }
  const message = node.querySelector('.plan-node-message');
  if (message) message.textContent = event.content || planStatusLabels[status];
  tree.querySelectorAll('.plan-node').forEach((item) => {
    if (item !== node && item.classList.contains('running')) item.classList.remove('expanded');
  });
  $('timeline').scrollTop = $('timeline').scrollHeight;
}

function appendPlanDetail(event) {
  const tree = $('executionPlan');
  if (!tree) return;
  if (event.type === 'output_check' && Array.isArray(event.data?.criteria)) {
    const criteria = $('acceptanceCriteria');
    if (criteria) criteria.innerHTML = event.data.criteria.map(renderAcceptanceCriterion).join('');
    const summary = $('acceptanceSummary');
    if (summary) {
      summary.textContent = event.data.passed ? `全部 ${event.data.criteria.length} 项通过` : `${event.data.criteria.filter((item) => item.status === 'failed').length} 项未通过`;
      summary.className = event.data.passed ? 'passed' : 'failed';
    }
    updateAcceptanceDeliveryPanel({ event, report: event.data?.report });
  }
  const mapping = {
    intent: 'understand', skill: 'understand', model: 'execute', progress: 'execute',
    plan_check: tree.dataset.toolNodeId || 'execute', tool_call: tree.dataset.toolNodeId || 'execute',
    tool_result: tree.dataset.toolNodeId || 'execute', tool_error: tree.dataset.toolNodeId || 'execute',
    tool_blocked: tree.dataset.toolNodeId || 'execute', output_check: 'validate',
  };
  const nodeId = mapping[event.type] || 'execute';
  const node = [...tree.querySelectorAll('.plan-node')].find((item) => item.dataset.nodeId === nodeId);
  if (!node) return;
  const details = node.querySelector('.plan-node-details');
  if (!details) return;
  const detail = document.createElement('div');
  detail.className = `plan-detail ${escapeHtml(event.type)}`;
  const label = { plan_check: '调用前校验', tool_call: '调用参数', tool_result: '工具结果', tool_error: '工具失败', tool_blocked: '工具阻止', output_check: '最终验收', model: '模型', skill: '技能', intent: '目标解析', progress: '进度' }[event.type] || taskEventLabel(event.type);
  let content = event.content || '';
  let meta = '';
  if (event.type === 'tool_call') {
    const args = event.data?.arguments || {};
    content = Object.entries(args).map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value)}`).join(' · ') || '无额外参数';
    meta = `${event.data?.server_id || ''}.${event.data?.tool_name || ''}`;
  } else if (event.type === 'plan_check') {
    const tool = event.data?.tool || '';
    const status = event.data?.passed === false ? '未通过' : '通过';
    meta = [status, tool].filter(Boolean).join(' · ');
    if (event.data?.reason && !content.includes(event.data.reason)) content = `${content || '调用前校验结果'} · ${event.data.reason}`;
  } else if (event.type === 'tool_result') {
    meta = event.data?.duration_ms ? `耗时 ${event.data.duration_ms} ms` : '';
    if (event.data?.artifact?.name) content = `${content} · ${event.data.artifact.name}`;
  } else if (event.type === 'tool_blocked') {
    meta = `${event.data?.server_id || ''}.${event.data?.tool_name || ''}`;
    const reason = event.data?.reason ? `原因：${event.data.reason}` : '';
    const source = event.data?.source ? `来源：${event.data.source}` : '';
    content = [content, reason, source].filter(Boolean).join(' · ');
  }
  detail.innerHTML = `<span class="plan-detail-kind">${escapeHtml(label)}</span><div><strong>${escapeHtml(event.title || label)}</strong>${content ? `<small>${escapeHtml(content)}</small>` : ''}</div>${meta ? `<em>${escapeHtml(meta)}</em>` : ''}`;
  details.appendChild(detail);
  node.classList.add('has-children', 'expanded');
  const head = node.querySelector('.plan-node-head');
  if (!head.querySelector('.plan-chevron')) head.insertAdjacentHTML('beforeend', '<span class="plan-chevron">⌄</span>');
}

function setPlanElementStatus(element, status, labelSelector) {
  element.classList.remove('pending', 'running', 'completed', 'failed');
  element.classList.add(status);
  const label = element.querySelector(labelSelector);
  if (label) label.textContent = planStatusLabels[status] || status;
}

function renderApproval(taskId, event = {}) {
  const isInstall = event.data?.action === 'install_recommended_skill';
  const recommendations = event.data?.recommendations || [];
  const div = document.createElement('div');
  div.className = `event ${isInstall ? 'skill-recommendation' : ''}`;
  div.innerHTML = `
    <div class="event-title"><span>${isInstall ? '发现可补充的 Skill' : '人工审批'}</span><span class="badge approval_required">${isInstall ? '能力补充' : '等待确认'}</span></div>
    <div class="event-content">${escapeHtml(event.content || (isInstall ? '当前能力不足，是否安装推荐 Skill？' : '是否批准执行敏感操作？'))}</div>
    ${recommendations.map((item) => {
      const installed = state.skills.some((skill) => skill.id === item.id);
      return `<div class="recommendation-item ${installed ? 'installed' : ''}"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.description || '')}</span><small>${escapeHtml(item.source_label || '内置目录')} · ${installed ? '已安装' : '可安装'}</small></div>`;
    }).join('')}
    <div class="approval-actions">
      <button id="approveYes">${isInstall ? '确认安装' : '批准'}</button>
      <button id="approveNo" class="secondary">${isInstall ? '暂不安装' : '拒绝'}</button>
      ${isInstall && recommendations[0] ? `<button id="viewRecommendation" class="secondary">去市场查看</button>` : ''}
    </div>
  `;
  $('timeline').appendChild(div);
  $('approveYes').onclick = () => approveTask(taskId, true);
  $('approveNo').onclick = () => approveTask(taskId, false);
  if (isInstall && recommendations[0]) {
    $('viewRecommendation').onclick = () => {
      state.marketplaceFocusId = recommendations[0].id;
      switchTab('marketplace');
      renderMarketplace();
    };
  }
}

async function approveTask(taskId, approved) {
  await api(`/api/tasks/${taskId}/approve`, {
    method: 'POST',
    body: JSON.stringify({ approved, note: approved ? '前端批准' : '前端拒绝' }),
  });
  startTaskStream(taskId);
}

async function refreshCurrentTask(taskId) {
  try {
    const task = await api(`/api/tasks/${taskId}`);
    state.currentTask = task;
    const thinking = agentThinkingCard(taskId);
    if (thinking) {
      thinking.loaded = true;
      (task.events || []).forEach((event) => updateAgentThinkingEvent(taskId, event));
    }
    if (runtimeIsActive(task.status)) {
      state.taskUiRunning = taskUiGeneratingStatus(task.status);
      state.taskUiTaskId = taskId;
      setSendButtonState(state.taskUiRunning ? (state.taskUiCancelRequested ? 'cancel_requested' : 'running') : 'idle');
      if (!state.taskUiStatusNode) setTaskUiStatus(taskId, task.status === 'waiting_approval' ? '等待你的确认…' : task.status === 'waiting_input' ? '等待你补充信息…' : task.status === 'queued' ? '任务已排队，正在启动…' : '正在执行任务…', taskUiGeneratingStatus(task.status) ? 'thinking' : 'verifying');
    } else if (['completed', 'failed', 'cancelled'].includes(task.status)) {
      state.taskUiTaskId = taskId;
      finishTaskUi(taskId, task.status);
    }
    renderTaskMeta(task);
    renderArtifacts(task.artifacts || []);
    const clarification = [...(task.events || [])].reverse().find((event) => event.type === 'clarification');
    if (clarification) publishClarification(taskId, clarification);
    const error = [...(task.events || [])].reverse().find((event) => event.type === 'error');
    if (error) publishTaskError(taskId, error);
    const answer = [...(task.events || [])].reverse().find((event) => event.type === 'answer');
    if (answer && !clarification && !$('conversation').querySelector(`[data-event-id="${answer.id}"]`)) {
      const message = addMessage('agent', answer.content || answer.title, answer.id);
      message.dataset.taskMessage = String(taskId || '');
    }
    await loadTaskRuntime(taskId, { silent: true });
  } catch (err) {
    console.error(err);
  }
}

function renderArtifacts(artifacts) {
  const box = $('artifacts');
  if (!artifacts.length) {
    box.innerHTML = '暂无产物';
    box.classList.add('empty');
    return;
  }
  box.classList.remove('empty');
  box.innerHTML = artifacts.map((a) => {
    const downloadUrl = safeArtifactDownloadUrl(a.download_url);
    return `
      <div class="artifact">
        <span>${escapeHtml(a.name)} <span class="small">${escapeHtml(a.kind)}</span></span>
        <span class="artifact-actions"><button type="button" class="text-button" data-preview-artifact="${escapeHtml(a.id || '')}" ${a.id ? '' : 'disabled'}>预览</button>${downloadUrl ? `<a href="${escapeHtml(downloadUrl)}" target="_blank" rel="noopener noreferrer">下载</a>` : ''}</span>
      </div>
    `;
  }).join('');
  box.querySelectorAll('[data-preview-artifact]').forEach((button) => {
    button.onclick = () => openArtifactPreview(button.dataset.previewArtifact).catch((err) => notify(`产物预览失败：${err.message || err}`, 'error'));
  });
  updateAcceptanceDeliveryPanel();
}

function renderSkills() {
  const keyword = String($('skillSearch')?.value || '').trim().toLowerCase();
  const showDisabled = !!$('skillShowDisabled')?.checked;
  const visible = state.skills.filter((s) => {
    if (!showDisabled && !s.enabled) return false;
    if (!keyword) return true;
    return [s.name, s.id, s.description, s.category].some((value) => String(value || '').toLowerCase().includes(keyword));
  });
  if ($('skillCount')) $('skillCount').textContent = `${visible.length}/${state.skills.length}`;
  $('skillList').innerHTML = visible.map((s) => `
    <div class="card skill-card ${state.selectedSkill?.id === s.id ? 'active' : ''} ${s.enabled ? '' : 'disabled'}" data-skill="${escapeHtml(s.id)}">
      <div class="card-title"><span>${escapeHtml(s.name)}</span><span class="status">${s.enabled ? 'enabled' : 'disabled'}</span></div>
      <div class="card-desc">${escapeHtml(s.description)}</div>
      <div class="small">${escapeHtml(s.id)} · ${escapeHtml(s.category)} · ${escapeHtml(s.version)} · ${s.file_count || 0} files</div>
    </div>
  `).join('') || `<div class="meta empty">${keyword ? '没有匹配的技能。' : (showDisabled ? '还没有技能。' : '没有启用的技能。')}</div>`;
  document.querySelectorAll('[data-skill]').forEach((el) => el.onclick = () => selectSkill(el.dataset.skill));
}

function scrollSkillCardIntoView(id) {
  if (!id) return;
  const card = document.querySelector(`[data-skill="${CSS.escape(id)}"]`);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

async function selectSkill(id) {
  const skill = await api(`/api/skills/${id}`);
  state.selectedSkill = skill;
  $('skillEditorTitle').textContent = skill.name;
  $('skillId').value = skill.id;
  $('skillId').disabled = true;
  $('skillName').value = skill.name;
  $('skillCategory').value = skill.category;
  $('skillVersion').value = skill.version;
  $('skillDescription').value = skill.description;
  $('skillMcps').value = (skill.required_mcps || []).join(',');
  $('skillEnabled').checked = !!skill.enabled;
  $('skillContent').value = skill.content;
  $('skillPackageStatus').textContent = (skill.package_missing || []).length
    ? `缺少 ${skill.package_missing.length} 个被 SKILL.md 引用的文件：${skill.package_missing.join('、')}`
    : `包结构完整 · ${skill.file_count || 0} 个文件`;
  $('skillPackageStatus').className = `small ${(skill.package_missing || []).length ? 'package-warning' : 'package-ok'}`;
  $('deleteSkillBtn').classList.toggle('hidden', skill.category === 'builtin');
  $('exportSkillBtn').disabled = false;
  await loadSkillFiles(id);
  renderSkills();
  scrollSkillCardIntoView(id);
}

function newSkill() {
  state.selectedSkill = null;
  $('skillEditorTitle').textContent = '新建技能';
  $('skillId').disabled = false;
  $('skillId').value = 'custom_skill_' + Math.floor(Math.random() * 1000);
  $('skillName').value = '自定义技能';
  $('skillCategory').value = 'custom';
  $('skillVersion').value = '0.1.0';
  $('skillDescription').value = '描述这个技能适合处理哪些任务。';
  $('skillMcps').value = 'report';
  $('skillEnabled').checked = true;
  $('skillContent').value = `---\nid: ${$('skillId').value}\nname: 自定义技能\ndescription: 描述这个技能适合处理哪些任务。\ncategory: custom\nversion: 0.1.0\nrequired_mcps: report\n---\n\n# 自定义技能\n\n## 使用条件\n\n说明什么场景触发。\n\n## 执行流程\n\n1. 第一步。\n2. 第二步。\n\n## 输出格式\n\n说明最终结果怎么输出。\n`;
  $('deleteSkillBtn').classList.add('hidden');
  $('exportSkillBtn').disabled = true;
  $('skillPackageStatus').textContent = '保存 Skill 后可维护包文件';
  $('skillPackageStatus').className = 'small';
  state.skillFiles = [];
  state.selectedSkillFile = null;
  renderSkillFiles();
}

async function loadSkillFiles(skillId) {
  state.skillFiles = await api(`/api/skills/${encodeURIComponent(skillId)}/files`);
  state.selectedSkillFile = null;
  renderSkillFiles();
  const first = state.skillFiles.find((item) => item.path === 'SKILL.md') || state.skillFiles[0];
  if (first) await selectSkillFile(first.path);
}

function renderSkillFiles() {
  $('skillFileList').innerHTML = state.skillFiles.length ? state.skillFiles.map((file) => `
    <button type="button" class="skill-file ${state.selectedSkillFile?.path === file.path ? 'active' : ''}" data-skill-file="${escapeHtml(file.path)}">
      <span>${escapeHtml(file.path)}</span><small>${file.is_binary ? 'binary' : `${Math.ceil(file.size / 1024) || 1} KB`}</small>
    </button>
  `).join('') : '<div class="meta empty">当前 Skill 只有主配置，尚无附属文件</div>';
  document.querySelectorAll('[data-skill-file]').forEach((el) => el.onclick = () => selectSkillFile(el.dataset.skillFile));
  if (!state.selectedSkillFile) {
    $('skillFilePath').value = '';
    $('skillFileContent').value = '';
    $('skillFileContent').disabled = true;
    $('saveSkillFileBtn').disabled = true;
    $('deleteSkillFileBtn').classList.add('hidden');
    $('skillFileMeta').textContent = '尚未选择文件';
  }
}

async function selectSkillFile(path) {
  if (!state.selectedSkill) return;
  const file = await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}/files/${path.split('/').map(encodeURIComponent).join('/')}`);
  state.selectedSkillFile = file;
  $('skillFilePath').value = file.path;
  $('skillFilePath').disabled = true;
  $('skillFileContent').value = file.content || '';
  $('skillFileContent').disabled = !!file.is_binary;
  $('saveSkillFileBtn').disabled = !!file.is_binary;
  $('deleteSkillFileBtn').classList.toggle('hidden', file.path === 'SKILL.md');
  $('skillFileMeta').textContent = `${file.content_type} · ${file.size} bytes${file.is_binary ? ' · 二进制文件仅支持查看元数据' : ''}`;
  renderSkillFiles();
}

function newSkillFile() {
  if (!state.selectedSkill) return notify('请先保存或选择一个 Skill', 'error');
  state.selectedSkillFile = { path: '', content: '', is_binary: false, is_new: true };
  $('skillFilePath').disabled = false;
  $('skillFilePath').value = 'references/rules.md';
  $('skillFileContent').disabled = false;
  $('skillFileContent').value = '# Rules\n\n在这里维护规则、脚本说明或参考资料。\n';
  $('saveSkillFileBtn').disabled = false;
  $('deleteSkillFileBtn').classList.add('hidden');
  $('skillFileMeta').textContent = '新文件';
  renderSkillFiles();
}

async function saveSkillFile() {
  if (!state.selectedSkill) return;
  const path = $('skillFilePath').value.trim();
  if (!path) return notify('请填写文件路径', 'error');
  const encodedPath = path.split('/').map(encodeURIComponent).join('/');
  await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}/files/${encodedPath}`, { method: 'PUT', body: JSON.stringify({ content: $('skillFileContent').value }) });
  if (path === 'SKILL.md') {
    state.selectedSkill = await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}`);
    $('skillContent').value = state.selectedSkill.content;
  }
  await loadSkillFiles(state.selectedSkill.id);
  await selectSkillFile(path);
  state.skills = await api('/api/skills');
  renderSkills();
  notify(`文件“${path}”已保存`);
}

async function deleteSkillFile() {
  const file = state.selectedSkillFile;
  if (!state.selectedSkill || !file || file.path === 'SKILL.md') return;
  if (!confirm(`确定删除文件“${file.path}”吗？`)) return;
  const encodedPath = file.path.split('/').map(encodeURIComponent).join('/');
  await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}/files/${encodedPath}`, { method: 'DELETE' });
  await loadSkillFiles(state.selectedSkill.id);
  state.skills = await api('/api/skills');
  renderSkills();
  notify(`文件“${file.path}”已删除`);
}

async function uploadSkillFile(file) {
  if (!state.selectedSkill || !file) return notify('请先选择一个 Skill', 'error');
  let path = $('skillFilePath').value.trim();
  if (!path || path === 'SKILL.md') path = file.name;
  const form = new FormData();
  form.append('file', file);
  await api(`/api/skills/${encodeURIComponent(state.selectedSkill.id)}/files/upload?path=${encodeURIComponent(path)}`, { method: 'POST', body: form });
  await loadSkillFiles(state.selectedSkill.id);
  await selectSkillFile(path);
  state.skills = await api('/api/skills');
  renderSkills();
  notify(`文件“${path}”已上传`);
}

function exportSelectedSkill() {
  if (!state.selectedSkill) return notify('请先选择一个 Skill', 'error');
  window.location.href = `/api/skills/${encodeURIComponent(state.selectedSkill.id)}/export`;
}

async function deleteSkill() {
  const skill = state.selectedSkill;
  if (!skill || skill.category === 'builtin') return;
  if (!confirm(`确定卸载技能“${skill.name}”吗？`)) return;
  await api(`/api/skills/${skill.id}`, { method: 'DELETE' });
  state.skills = await api('/api/skills');
  newSkill(); renderSkills(); notify(`技能“${skill.name}”已卸载`);
}

async function saveSkill() {
  const button = $('saveSkillBtn');
  setBusy(button, true);
  try {
  const payload = {
    id: $('skillId').value.trim(),
    name: $('skillName').value.trim(),
    description: $('skillDescription').value.trim(),
    category: $('skillCategory').value.trim() || 'custom',
    version: $('skillVersion').value.trim() || '0.1.0',
    content: $('skillContent').value,
    enabled: $('skillEnabled').checked,
    required_mcps: $('skillMcps').value.split(',').map((x) => x.trim()).filter(Boolean),
  };
  if (state.selectedSkill) {
    await api(`/api/skills/${state.selectedSkill.id}`, { method: 'PUT', body: JSON.stringify(payload) });
  } else {
    await api('/api/skills', { method: 'POST', body: JSON.stringify(payload) });
  }
  state.skills = await api('/api/skills');
  renderSkills();
  await selectSkill(payload.id);
  notify(`技能“${payload.name}”已保存`);
  } catch (err) {
    notify(`技能保存失败：${err.message || err}`, 'error');
  } finally {
    setBusy(button, false);
  }
}

function renderMcp() {
  $('mcpList').innerHTML = state.mcp.map((m) => `
    <div class="card ${state.selectedMcp?.id === m.id ? 'active' : ''}" data-mcp="${escapeHtml(m.id)}">
      <div class="card-title"><span>${escapeHtml(m.name)}</span><span class="status">${escapeHtml(m.kind)}</span></div>
      <div class="card-desc">${escapeHtml(m.description)}</div>
      <div class="small">${escapeHtml(m.id)} · ${m.tools?.length || 0} tools</div>
    </div>
  `).join('');
  document.querySelectorAll('[data-mcp]').forEach((el) => el.onclick = () => selectMcp(el.dataset.mcp));
}

function scrollMcpCardIntoView(id) {
  if (!id) return;
  const card = document.querySelector(`[data-mcp="${CSS.escape(id)}"]`);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

async function selectMcp(id) {
  const mcp = await api(`/api/mcp/${id}`);
  state.selectedMcp = mcp;
  $('mcpTitle').textContent = mcp.name;
  $('mcpDetails').textContent = `ID：${mcp.id}\n类型：${mcp.kind}\n状态：${mcp.enabled ? '启用' : '停用'}\n描述：${mcp.description}`;
  $('mcpId').value = mcp.id; $('mcpId').disabled = true; $('mcpName').value = mcp.name; $('mcpKind').value = mcp.kind;
  $('mcpDescription').value = mcp.description; $('mcpConfig').value = formatJson(mcp.config || {}); $('mcpEnabled').checked = !!mcp.enabled;
  $('deleteMcpBtn').classList.toggle('hidden', mcp.kind === 'builtin');
  $('toolList').innerHTML = (mcp.tools || []).map((t) => `
    <div class="tool-item" data-tool="${escapeHtml(t.name)}">
      <strong>${escapeHtml(t.name)}</strong>
      <span>${escapeHtml(t.description)}</span>
      <details><summary>schema</summary><pre>${escapeHtml(formatJson(t.input_schema || {}))}</pre></details>
    </div>
  `).join('');
  document.querySelectorAll('[data-tool]').forEach((el) => el.onclick = () => {
    $('testServerId').value = mcp.id;
    $('testToolName').value = el.dataset.tool;
    const tool = (mcp.tools || []).find((t) => t.name === el.dataset.tool);
    $('toolArgs').value = exampleArgs(mcp.id, tool?.name);
  });
  renderMcp();
  scrollMcpCardIntoView(id);
}

function newMcp() {
  state.selectedMcp = null; $('mcpTitle').textContent = '添加工具服务'; $('mcpDetails').textContent = '连接本地、远程或 HTTP 工具服务。';
  $('mcpId').disabled = false; $('mcpId').value = 'my-tools'; $('mcpName').value = '我的工具服务'; $('mcpKind').value = 'mcp_stdio'; $('mcpDescription').value = '';
  $('mcpConfig').value = formatJson({ command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem', '/workspace'], env: {} }); $('mcpEnabled').checked = true; $('toolList').innerHTML = '';
  $('deleteMcpBtn').classList.add('hidden');
}

async function deleteMcp() {
  const server = state.selectedMcp;
  if (!server || server.kind === 'builtin') return;
  if (!confirm(`确定卸载工具服务“${server.name}”吗？`)) return;
  await api(`/api/mcp/${server.id}`, { method: 'DELETE' });
  state.mcp = await api('/api/mcp');
  newMcp(); renderMcp(); notify(`工具服务“${server.name}”已卸载`);
}

async function saveMcp() {
  const button = $('saveMcpBtn');
  setBusy(button, true);
  try {
  const payload = { id: $('mcpId').value.trim(), name: $('mcpName').value.trim(), kind: $('mcpKind').value, description: $('mcpDescription').value.trim(), enabled: $('mcpEnabled').checked, config: JSON.parse($('mcpConfig').value || '{}'), tools: state.selectedMcp?.tools || [] };
  if (!payload.id || !payload.name) throw new Error('请填写 ID 和名称');
  if (state.selectedMcp) await api(`/api/mcp/${state.selectedMcp.id}`, { method: 'PUT', body: JSON.stringify(payload) });
  else await api('/api/mcp', { method: 'POST', body: JSON.stringify(payload) });
  state.mcp = await api('/api/mcp'); await selectMcp(payload.id);
  notify(`工具服务“${payload.name}”已保存`);
  return payload;
  } catch (err) {
    notify(`工具服务保存失败：${err.message || err}`, 'error');
    throw err;
  } finally {
    setBusy(button, false);
  }
}

async function discoverMcp() {
  try { await saveMcp(); const tools = await api(`/api/mcp/${$('mcpId').value.trim()}/discover`, { method: 'POST' }); $('toolResult').textContent = formatJson(tools); state.mcp = await api('/api/mcp'); await selectMcp($('mcpId').value.trim()); }
  catch (err) { $('toolResult').textContent = String(err.message || err); }
}

function exampleArgs(serverId, toolName) {
  const server = state.mcp.find((item) => item.id === serverId);
  const tool = (server?.tools || []).find((item) => item.name === toolName);
  const schema = tool?.input_schema || {};
  const properties = schema.properties || {};
  const required = new Set(schema.required || []);
  const valueFor = (definition = {}) => {
    if (definition.default !== undefined) return definition.default;
    if (Array.isArray(definition.enum) && definition.enum.length) return definition.enum[0];
    if (definition.type === 'array') return [];
    if (definition.type === 'object') return {};
    if (definition.type === 'integer' || definition.type === 'number') return 0;
    if (definition.type === 'boolean') return false;
    return '';
  };
  return formatJson(Object.fromEntries(
    Object.entries(properties)
      .filter(([name, definition]) => required.has(name) || definition.default !== undefined)
      .map(([name, definition]) => [name, valueFor(definition)]),
  ));
}

async function invokeTool() {
  try {
    const serverId = $('testServerId').value.trim();
    const toolName = $('testToolName').value.trim();
    const args = JSON.parse($('toolArgs').value || '{}');
    const result = await api(`/api/mcp/${serverId}/tools/${toolName}/invoke`, {
      method: 'POST',
      body: JSON.stringify({ arguments: args }),
    });
    $('toolResult').textContent = formatJson(result);
  } catch (err) {
    $('toolResult').textContent = String(err.message || err);
  }
}

function renderAgents() {
  const keyword = String($('agentSearch')?.value || '').trim().toLowerCase();
  const showDisabled = !!$('agentShowDisabled')?.checked;
  const visible = state.agents.filter((a) => {
    if (!showDisabled && a.enabled === false) return false;
    if (!keyword) return true;
    return [a.name, a.id, a.description].some((value) => String(value || '').toLowerCase().includes(keyword));
  });
  $('agentCount').textContent = `${visible.length}/${state.agents.length}`;
  $('agentList').innerHTML = visible.map((a) => `
    <div class="agent-card ${state.selectedAgent?.id === a.id ? 'active' : ''}" data-agent="${escapeHtml(a.id)}">
      <div class="agent-card-head">
        <div class="agent-avatar ${agentAvatarTone(a)}">${agentIconSvg(a)}</div>
        <div class="agent-card-copy"><div class="card-title">${escapeHtml(a.name)}</div><div class="card-desc">${escapeHtml(a.description)}</div></div>
        <span class="status">${escapeHtml(a.model)}</span>
      </div>
      <div class="kv">
        <strong>ID</strong><span>${escapeHtml(a.id)}</span>
        <strong>技能</strong><span>${escapeHtml((a.skills || []).join(', '))}</span>
        <strong>工具</strong><span>${escapeHtml((a.mcp_servers || []).join(', '))}</span>
        <strong>权限</strong><span>${escapeHtml(formatJson(a.permissions || {}))}</span>
      </div>
    </div>
  `).join('') || `<div class="meta empty">${keyword ? '没有匹配的智能体。' : '还没有智能体。'}</div>`;
  document.querySelectorAll('[data-agent]').forEach((el) => el.onclick = () => selectAgent(el.dataset.agent));
}

function newAgent() { state.selectedAgent = null; $('agentEditorTitle').textContent = '新建智能体'; $('agentId').disabled = false; $('agentId').value = 'custom-agent'; $('agentName').value = '我的智能体'; $('agentDescription').value = ''; $('agentPrompt').value = '请理解用户目标，优先使用已配置的技能和工具完成任务。'; $('agentSkills').value = 'general_task,report_generation'; $('agentMcps').value = 'report'; $('agentPermissions').value = '{}'; $('agentModel').value = 'deterministic'; renderAgents(); }
function selectAgent(id) { const a = state.agents.find((x) => x.id === id); if (!a) return; state.selectedAgent = a; $('agentEditorTitle').textContent = a.name; $('agentId').disabled = true; $('agentId').value = a.id; $('agentName').value = a.name; $('agentDescription').value = a.description; $('agentPrompt').value = a.system_prompt; $('agentSkills').value = (a.skills || []).join(','); $('agentMcps').value = (a.mcp_servers || []).join(','); $('agentPermissions').value = formatJson(a.permissions || {}); $('agentModel').value = a.model; renderAgents(); }
async function saveAgent() { const list = (id) => $(id).value.split(',').map((x) => x.trim()).filter(Boolean); const payload = { id: $('agentId').value.trim(), name: $('agentName').value.trim(), description: $('agentDescription').value.trim(), model: $('agentModel').value, system_prompt: $('agentPrompt').value, skills: list('agentSkills'), mcp_servers: list('agentMcps'), permissions: JSON.parse($('agentPermissions').value || '{}') }; if (state.selectedAgent) await api(`/api/agents/${state.selectedAgent.id}`, { method: 'PUT', body: JSON.stringify(payload) }); else await api('/api/agents', { method: 'POST', body: JSON.stringify(payload) }); state.agents = await api(`/api/agents?workspace_id=${encodeURIComponent(currentWorkspaceId())}`); renderAgentsSelect(); selectAgent(payload.id); }

function renderWorkspaceModelOptions() {
  const agentSelect = $('workspaceDefaultAgent');
  const modelSelect = $('workspaceDefaultModel');
  if (agentSelect) agentSelect.innerHTML = state.agents.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
  if (modelSelect) modelSelect.innerHTML = state.models.filter((item) => item.enabled).map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
}

function renderWorkspaces() {
  renderWorkspaceModelOptions();
  const keyword = String($('workspaceSearch')?.value || '').trim().toLowerCase();
  const showDisabled = !!$('workspaceShowDisabled')?.checked;
  const visible = state.workspaces.filter((item) => {
    if (!showDisabled && !item.enabled) return false;
    if (!keyword) return true;
    return [item.name, item.id, item.description].some((value) => String(value || '').toLowerCase().includes(keyword));
  });
  $('workspaceCount').textContent = `${visible.length}/${state.workspaces.length}`;
  $('workspaceList').innerHTML = visible.map((item) => `
    <div class="card workspace-card ${state.selectedWorkspace?.id === item.id ? 'active' : ''} ${item.enabled ? '' : 'disabled'}" data-workspace="${escapeHtml(item.id)}">
      <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.enabled ? '已启用' : '已停用'}</span></div>
      <div class="card-desc">${escapeHtml(item.description || '暂无描述')}</div>
      <div class="memory-card-meta"><span>${escapeHtml(item.id)}</span><span>智能体 ${escapeHtml(item.default_agent_id || 'general-agent')}</span><span>模型 ${escapeHtml(item.default_model_id || 'deterministic')}</span></div>
    </div>
  `).join('') || `<div class="meta empty">${keyword ? '没有匹配的项目。' : '还没有项目。'}</div>`;
  document.querySelectorAll('[data-workspace]').forEach((element) => {
    element.onclick = () => selectWorkspaceEditor(element.dataset.workspace);
  });
}

function newWorkspace() {
  $('workspaceMembersPanel').classList.add('hidden');
  state.selectedWorkspace = null;
  renderWorkspaces();
  $('workspaceEditorTitle').textContent = '新建项目';
  $('workspaceEditorMeta').textContent = '保存后可在左侧切换当前项目';
  $('workspaceId').disabled = false;
  $('workspaceId').value = 'project-' + Date.now().toString(36).slice(-6);
  $('workspaceName').value = '新项目';
  $('workspaceDescription').value = '';
  $('workspaceDefaultAgent').value = $('agentSelect')?.value || 'general-agent';
  $('workspaceDefaultModel').value = $('taskModelSelect')?.value || 'deterministic';
  $('workspaceSettings').value = '{}';
  $('workspaceEnabled').checked = true;
  $('deleteWorkspaceBtn').classList.add('hidden');
}

function selectWorkspaceEditor(id, { activate = true } = {}) {
  const item = state.workspaces.find((workspace) => workspace.id === id);
  if (!item) return;
  state.selectedWorkspace = item;
  if (activate && item.enabled) {
    state.workspaceId = item.id;
    writePreference('workspace', item.id);
    renderWorkspaceSelect();
  }
  renderWorkspaces();
  $('workspaceEditorTitle').textContent = item.name;
  $('workspaceEditorMeta').textContent = `${item.id} · 更新于 ${runtimeTime(item.updated_at)}`;
  $('workspaceId').disabled = true;
  $('workspaceId').value = item.id;
  $('workspaceName').value = item.name || '';
  $('workspaceDescription').value = item.description || '';
  $('workspaceDefaultAgent').value = item.default_agent_id || 'general-agent';
  $('workspaceDefaultModel').value = item.default_model_id || 'deterministic';
  $('workspaceSettings').value = formatJson(item.settings || {});
  $('workspaceEnabled').checked = !!item.enabled;
  $('deleteWorkspaceBtn').classList.toggle('hidden', item.id === 'default' || !item.enabled);
  const canManage = state.currentUser && (state.currentUser.role === 'admin' || state.currentUser.user_id === item.owner_user_id);
  $('workspaceMembersPanel').classList.toggle('hidden', !canManage);
  if (canManage) loadWorkspaceMembers(item.id).catch((error) => { $('workspaceMembersError').textContent = error.message; });
}

async function loadWorkspaceMembers(workspaceId) {
  $('workspaceMembersError').textContent = '';
  const members = await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/members`);
  if (state.selectedWorkspace?.id !== workspaceId) return;
  const list = $('workspaceMembersList');
  list.replaceChildren();
  for (const member of members) {
    const row = document.createElement('div');
    row.className = 'toolbar';
    const label = document.createElement('span');
    label.textContent = `${member.username} · ${member.role === 'viewer' ? '只读' : '成员'}`;
    row.appendChild(label);
    for (const [text, method, role] of [['切换权限', 'PUT', member.role === 'viewer' ? 'member' : 'viewer'], ['移除', 'DELETE', null]]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'secondary';
      button.textContent = text;
      button.onclick = async () => {
        button.disabled = true;
        try {
          await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(member.user_id)}`, {method, ...(role ? {body: JSON.stringify({role})} : {})});
          await loadWorkspaceMembers(workspaceId);
        } catch (error) { $('workspaceMembersError').textContent = error.message; }
        finally { button.disabled = false; }
      };
      row.appendChild(button);
    }
    list.appendChild(row);
  }
}

async function addWorkspaceMember() {
  const workspaceId = state.selectedWorkspace?.id;
  const username = $('workspaceMemberUsername').value.trim();
  if (!workspaceId || !username) return;
  $('addWorkspaceMemberBtn').disabled = true;
  try {
    await api(`/api/workspaces/${encodeURIComponent(workspaceId)}/members/by-username/${encodeURIComponent(username)}`, {method:'PUT', body:JSON.stringify({role:$('workspaceMemberRole').value})});
    $('workspaceMemberUsername').value = '';
    await loadWorkspaceMembers(workspaceId);
  } catch (error) { $('workspaceMembersError').textContent = error.message; }
  finally { $('addWorkspaceMemberBtn').disabled = false; }
}

async function saveWorkspace() {
  const id = $('workspaceId').value.trim();
  const payload = {
    id,
    name: $('workspaceName').value.trim(),
    description: $('workspaceDescription').value.trim(),
    organization_id: 'local-org',
    user_id: 'local-user',
    default_agent_id: $('workspaceDefaultAgent').value || 'general-agent',
    default_model_id: $('workspaceDefaultModel').value || 'deterministic',
    settings: JSON.parse($('workspaceSettings').value || '{}'),
    enabled: $('workspaceEnabled').checked,
  };
  if (!payload.id || !payload.name) return notify('请填写项目 ID 和名称', 'error');
  const button = $('saveWorkspaceBtn'); setBusy(button, true);
  try {
    const saved = state.workspaces.some((item) => item.id === id)
      ? await api(`/api/workspaces/${encodeURIComponent(id)}?${workspaceQuery()}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: payload.name,
          description: payload.description,
          default_agent_id: payload.default_agent_id,
          default_model_id: payload.default_model_id,
          settings: payload.settings,
          enabled: payload.enabled,
        }),
      })
      : await api('/api/workspaces', { method: 'POST', body: JSON.stringify(payload) });
    await loadWorkspacesOnly({ preserveSelection: false });
    state.workspaceId = saved.id;
    writePreference('workspace', saved.id);
    ensureSelectedWorkspace();
    renderWorkspaceSelect();
    selectWorkspaceEditor(saved.id, { activate: true });
    await reloadWorkspaceScopedData();
    notify(`项目“${saved.name}”已保存并切换`);
  } catch (err) { notify(`项目保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteWorkspace() {
  const item = state.selectedWorkspace;
  if (!item || item.id === 'default') return;
  if (!confirm(`确定停用项目“${item.name}”吗？已有任务、知识库和产物不会删除，但该项目不会再出现在切换列表中。`)) return;
  const button = $('deleteWorkspaceBtn'); setBusy(button, true, '停用中…');
  try {
    await api(`/api/workspaces/${encodeURIComponent(item.id)}?${workspaceQuery()}`, { method: 'DELETE' });
    state.workspaceId = 'default';
    writePreference('workspace', 'default');
    await loadWorkspacesOnly({ preserveSelection: false });
    await reloadWorkspaceScopedData();
    notify('项目已停用，当前已切回默认项目');
  } catch (err) { notify(`项目停用失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function switchWorkspace(id) {
  if (!id || id === currentWorkspaceId()) return;
  state.workspaceId = id;
  writePreference('workspace', id);
  ensureSelectedWorkspace();
  selectWorkspaceEditor(id, { activate: false });
  const workspace = state.selectedWorkspace;
  if (workspace?.default_agent_id && state.agents.some((item) => item.id === workspace.default_agent_id)) {
    $('agentSelect').value = workspace.default_agent_id;
    writePreference('agent', workspace.default_agent_id);
  }
  if (workspace?.default_model_id && state.models.some((item) => item.id === workspace.default_model_id && item.enabled)) {
    $('taskModelSelect').value = workspace.default_model_id;
    writePreference('model', workspace.default_model_id);
    writePreference('model-explicit', '0');
  }
  state.currentTask = null;
  state.taskRuntime = null;
  state.runtimeTaskId = null;
  resetTaskRuntime();
  $('timeline').innerHTML = '';
  $('taskMeta').className = 'meta empty';
  $('taskMeta').textContent = '尚未创建任务';
  await reloadWorkspaceScopedData();
  notify(`已切换到项目“${workspace?.name || id}”`);
}

async function reloadWorkspaceScopedData() {
  const [tasks, loops] = await Promise.all([
    api(`/api/tasks?${new URLSearchParams(platformScopeValues()).toString()}`),
    api(`/api/loops?${new URLSearchParams(platformScopeValues()).toString()}`),
  ]);
  state.tasks = tasks;
  state.loops = loops;
  renderTasks();
  renderLoops();
  await Promise.allSettled([
    loadMemoriesOnly({ preserveSelection: false }),
    loadKnowledgeBasesOnly({ preserveSelection: false }),
    loadArtifactsOnly({ preserveSelection: false }),
    loadExpertWorkspace({ preserveSelection: true }),
  ]);
}

const expertVisibilityLabels = { private: '本机使用者', workspace: '当前工作区', organization: '当前实例', public: '实例内公共' };
const expertRunStatusLabels = {
  queued: '已排队', running: '成员执行中', aggregating: '主管汇总中', partial_failed: '部分失败',
  waiting_approval: '等待审批', completed: '已完成', failed: '失败', cancelled: '已取消',
};

function expertScopeValues() {
  return platformScopeValues();
}

function expertQuery({ includeDisabled = false } = {}) {
  const query = new URLSearchParams(expertScopeValues());
  if (includeDisabled) query.set('include_disabled', 'true');
  return query.toString();
}

function parseExpertJson(id, label, expected = 'object') {
  const raw = $(id).value.trim();
  let value;
  try { value = JSON.parse(raw || (expected === 'array' ? '[]' : '{}')); }
  catch (_) { throw new Error(`${label}不是有效的 JSON`); }
  if (expected === 'array' && !Array.isArray(value)) throw new Error(`${label}必须是 JSON 数组`);
  if (expected === 'object' && (!value || Array.isArray(value) || typeof value !== 'object')) throw new Error(`${label}必须是 JSON 对象`);
  return value;
}

function validateExpertId(value, label) {
  if (!value) throw new Error(`请填写${label}`);
  if (!/^[A-Za-z0-9_-]{2,80}$/.test(value)) throw new Error(`${label}只能包含字母、数字、短横线和下划线，长度 2–80`);
}

function expertOwns(item, ownerKey = 'owner_user_id') {
  const scope = expertScopeValues();
  return item
    && (item.organization_id || 'local-org') === scope.organization_id
    && (item.workspace_id || 'default') === scope.workspace_id
    && (item[ownerKey] || 'local-user') === scope.user_id;
}

async function loadExpertWorkspace({ preserveSelection = false } = {}) {
  const templateId = preserveSelection ? state.selectedExpertTemplate?.id : '';
  const teamId = preserveSelection ? state.selectedExpertTeam?.id : '';
  const [templates, installations, teams] = await Promise.all([
    api(`/api/expert-templates?${expertQuery({ includeDisabled: true })}`),
    api(`/api/expert-installations?${expertQuery()}`),
    api(`/api/expert-teams?${expertQuery({ includeDisabled: true })}`),
  ]);
  state.expertTemplates = Array.isArray(templates) ? templates : [];
  state.expertInstallations = Array.isArray(installations) ? installations : [];
  state.expertTeams = Array.isArray(teams) ? teams : [];
  state.selectedExpertTemplate = templateId ? state.expertTemplates.find((item) => item.id === templateId) || null : null;
  state.selectedExpertTeam = teamId ? state.expertTeams.find((item) => item.id === teamId) || null : null;
  renderExpertTemplates();
  renderExpertInstallations();
  renderExpertTeams();
  renderExpertRunModelOptions();
  if (state.selectedExpertTemplate) selectExpertTemplate(state.selectedExpertTemplate.id, { reload: false });
  else newExpertTemplate({ preserveLists: true });
  if (state.selectedExpertTeam) await selectExpertTeam(state.selectedExpertTeam.id, { reload: false, loadRuns: true });
  else newExpertTeam({ preserveLists: true });
  renderWorkbenchTeamOptions();
  renderWorkbenchMode();
}

function renderExpertTemplates() {
  $('expertTemplateCount').textContent = `${state.expertTemplates.length} 个模板`;
  $('expertTemplateList').innerHTML = state.expertTemplates.map((item) => {
    const manifest = item.manifest && typeof item.manifest === 'object' ? item.manifest : {};
    const capabilityCount = (Array.isArray(manifest.skills) ? manifest.skills.length : 0) + (Array.isArray(manifest.mcp_servers) ? manifest.mcp_servers.length : 0);
    return `
      <button class="card expert-template-card ${state.selectedExpertTemplate?.id === item.id ? 'active' : ''} ${item.enabled ? '' : 'disabled'}" data-expert-template="${escapeHtml(item.id)}" type="button">
        <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.enabled ? `v${escapeHtml(item.version)}` : '已停用'}</span></div>
        <div class="card-desc">${escapeHtml(item.description || '未填写模板说明')}</div>
        <div class="expert-card-meta"><span>${escapeHtml(expertVisibilityLabels[item.visibility] || item.visibility)}</span><span>${capabilityCount} 项能力</span><span>${escapeHtml(item.id)}</span></div>
      </button>`;
  }).join('') || '<div class="meta empty">还没有专家模板。新建模板后即可安装为专家。</div>';
  $('expertTemplateList').querySelectorAll('[data-expert-template]').forEach((element) => {
    element.onclick = () => selectExpertTemplate(element.dataset.expertTemplate).catch((err) => notify(`模板读取失败：${err.message || err}`, 'error'));
  });
}

function newExpertTemplate({ preserveLists = false } = {}) {
  state.selectedExpertTemplate = null;
  if (!preserveLists) renderExpertTemplates();
  $('expertTemplateEditorTitle').textContent = '新建专家模板';
  $('expertTemplateEditorMeta').textContent = '配置可复用的角色、能力和权限边界';
  $('expertTemplateId').disabled = false;
  $('expertTemplateId').value = '';
  $('expertTemplateName').value = '';
  $('expertTemplateVersion').value = '0.1.0';
  $('expertTemplateVisibility').value = 'organization';
  $('expertTemplateDescription').value = '';
  $('expertTemplateManifest').value = formatJson({
    name: '专业分析专家',
    description: '',
    model: 'deterministic',
    system_prompt: '围绕指定分工独立完成分析，输出可核验的结论、依据和风险提示。',
    skills: [],
    mcp_servers: [],
    permissions: { read_only: true },
  });
  $('expertTemplatePermissions').value = '{}';
  $('expertTemplateEnabled').checked = true;
  $('expertInstallAgentId').value = '';
  $('expertInstallVisibility').value = 'private';
  $('expertInstallPermissions').value = '{}';
  $('deleteExpertTemplateBtn').classList.add('hidden');
  $('saveExpertTemplateBtn').disabled = false;
  $('installExpertTemplateBtn').disabled = true;
}

async function selectExpertTemplate(id, { reload = true } = {}) {
  const item = reload
    ? await api(`/api/expert-templates/${encodeURIComponent(id)}?${expertQuery()}`)
    : state.expertTemplates.find((template) => template.id === id);
  if (!item) return;
  state.selectedExpertTemplate = item;
  const index = state.expertTemplates.findIndex((template) => template.id === item.id);
  if (index >= 0) state.expertTemplates[index] = item;
  renderExpertTemplates();
  const owned = expertOwns(item);
  $('expertTemplateEditorTitle').textContent = item.name;
  $('expertTemplateEditorMeta').textContent = `${item.id} · ${expertVisibilityLabels[item.visibility] || item.visibility}${owned ? '' : ' · 只读模板'}`;
  $('expertTemplateId').disabled = true;
  $('expertTemplateId').value = item.id;
  $('expertTemplateName').value = item.name || '';
  $('expertTemplateVersion').value = item.version || '0.1.0';
  $('expertTemplateVisibility').value = item.visibility || 'organization';
  $('expertTemplateDescription').value = item.description || '';
  $('expertTemplateManifest').value = formatJson(item.manifest || {});
  $('expertTemplatePermissions').value = formatJson(item.permissions || {});
  $('expertTemplateEnabled').checked = !!item.enabled;
  $('deleteExpertTemplateBtn').classList.toggle('hidden', !owned);
  $('saveExpertTemplateBtn').disabled = !owned;
  $('installExpertTemplateBtn').disabled = !item.enabled;
}

function expertTemplatePayload() {
  const scope = expertScopeValues();
  const id = $('expertTemplateId').value.trim();
  const name = $('expertTemplateName').value.trim();
  validateExpertId(id, '模板 ID');
  if (!name) throw new Error('请填写模板名称');
  return {
    id, name,
    description: $('expertTemplateDescription').value.trim(),
    version: $('expertTemplateVersion').value.trim() || '0.1.0',
    source: state.selectedExpertTemplate?.source || 'local',
    manifest: parseExpertJson('expertTemplateManifest', '专家配置'),
    permissions: parseExpertJson('expertTemplatePermissions', '模板权限'),
    visibility: $('expertTemplateVisibility').value,
    enabled: $('expertTemplateEnabled').checked,
    organization_id: scope.organization_id,
    workspace_id: scope.workspace_id,
    owner_user_id: scope.user_id,
  };
}

async function saveExpertTemplate() {
  const button = $('saveExpertTemplateBtn'); setBusy(button, true);
  try {
    const payload = expertTemplatePayload();
    let saved;
    if (state.selectedExpertTemplate) {
      const { id, organization_id, workspace_id, owner_user_id, ...changes } = payload;
      saved = await api(`/api/expert-templates/${encodeURIComponent(id)}?${expertQuery()}`, { method: 'PUT', body: JSON.stringify(changes) });
    } else {
      saved = await api('/api/expert-templates', { method: 'POST', body: JSON.stringify(payload) });
    }
    state.expertTemplates = await api(`/api/expert-templates?${expertQuery({ includeDisabled: true })}`);
    await selectExpertTemplate(saved.id, { reload: false });
    notify(`专家模板“${saved.name}”已保存`);
  } catch (err) { notify(`模板保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteExpertTemplate() {
  const item = state.selectedExpertTemplate;
  if (!item || !confirm(`确定删除专家模板“${item.name}”吗？`)) return;
  try {
    await api(`/api/expert-templates/${encodeURIComponent(item.id)}?${expertQuery()}`, { method: 'DELETE' });
    state.expertTemplates = await api(`/api/expert-templates?${expertQuery({ includeDisabled: true })}`);
    newExpertTemplate();
    notify('专家模板已删除');
  } catch (err) { notify(`模板删除失败：${err.message || err}`, 'error'); }
}

async function installSelectedExpertTemplate() {
  const item = state.selectedExpertTemplate;
  if (!item) return notify('请先选择已保存的专家模板', 'error');
  const button = $('installExpertTemplateBtn'); setBusy(button, true, '安装中…');
  try {
    const scope = expertScopeValues();
    const agentId = $('expertInstallAgentId').value.trim();
    if (agentId) validateExpertId(agentId, 'Agent ID');
    const payload = {
      ...scope,
      agent_id: agentId || null,
      visibility: $('expertInstallVisibility').value,
      permissions: parseExpertJson('expertInstallPermissions', '安装权限'),
      overrides: {},
    };
    const installed = await api(`/api/expert-templates/${encodeURIComponent(item.id)}/install`, { method: 'POST', body: JSON.stringify(payload) });
    [state.expertInstallations, state.agents] = await Promise.all([
      api(`/api/expert-installations?${expertQuery()}`),
      api(`/api/agents?workspace_id=${encodeURIComponent(currentWorkspaceId())}`),
    ]);
    $('expertInstallAgentId').value = '';
    renderExpertInstallations();
    renderAgents();
    renderAgentsSelect();
    renderLoops();
    refreshExpertTeamAgentSelectors();
    notify(`专家“${installed.agent?.name || installed.agent_id}”已安装`);
  } catch (err) { notify(`专家安装失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

function renderExpertInstallations() {
  $('expertInstallationCount').textContent = String(state.expertInstallations.length);
  $('expertInstallationList').innerHTML = state.expertInstallations.map((item) => {
    const agent = state.agents.find((value) => value.id === item.agent_id);
    const template = state.expertTemplates.find((value) => value.id === item.template_id);
    return `
      <div class="expert-installation-card">
        <div class="expert-avatar">${agentIconSvg(agent || { id: item.agent_id, name: '专家' })}</div>
        <div><strong>${escapeHtml(agent?.name || item.agent_id)}</strong><span>${escapeHtml(template?.name || item.template_id)} · v${escapeHtml(item.installed_version)}</span></div>
        <div class="expert-installation-actions"><button class="text-button" data-open-expert-agent="${escapeHtml(item.agent_id)}" type="button">配置</button><button class="text-button danger-text" data-disable-expert-install="${escapeHtml(item.id)}" type="button">停用</button></div>
      </div>`;
  }).join('') || '<div class="meta empty">尚未安装专家。选择模板后点击“安装当前模板”。</div>';
  $('expertInstallationList').querySelectorAll('[data-open-expert-agent]').forEach((button) => {
    button.onclick = () => { switchTab('agents'); selectAgent(button.dataset.openExpertAgent); };
  });
  $('expertInstallationList').querySelectorAll('[data-disable-expert-install]').forEach((button) => {
    button.onclick = () => disableExpertInstallation(button.dataset.disableExpertInstall);
  });
}

async function disableExpertInstallation(id) {
  const item = state.expertInstallations.find((value) => value.id === id);
  if (!item || !confirm(`确定停用专家“${item.agent_id}”的安装吗？正在使用它的启用团队会阻止此操作。`)) return;
  try {
    await api(`/api/expert-installations/${encodeURIComponent(id)}?${expertQuery()}`, { method: 'DELETE' });
    state.expertInstallations = await api(`/api/expert-installations?${expertQuery()}`);
    renderExpertInstallations();
    refreshExpertTeamAgentSelectors();
    notify('专家安装已停用');
  } catch (err) { notify(`停用失败：${err.message || err}`, 'error'); }
}

function orderedExpertAgents() {
  const installedIds = new Set(state.expertInstallations.map((item) => item.agent_id));
  return [...state.agents].sort((left, right) => Number(installedIds.has(right.id)) - Number(installedIds.has(left.id)) || String(left.name).localeCompare(String(right.name), 'zh-CN'));
}

function expertAgentOptions(selectedId = '') {
  const installedIds = new Set(state.expertInstallations.map((item) => item.agent_id));
  return `<option value="">请选择智能体</option>${orderedExpertAgents().map((agent) => `<option value="${escapeHtml(agent.id)}" ${agent.id === selectedId ? 'selected' : ''}>${escapeHtml(agent.name)}${installedIds.has(agent.id) ? ' · 已安装专家' : ''}</option>`).join('')}`;
}

function refreshExpertTeamAgentSelectors() {
  const supervisor = $('expertTeamSupervisor');
  const supervisorValue = supervisor.value;
  supervisor.innerHTML = expertAgentOptions(supervisorValue);
  if (state.agents.some((agent) => agent.id === supervisorValue)) supervisor.value = supervisorValue;
  $('expertTeamMembers').querySelectorAll('[data-member-agent]').forEach((select) => {
    const value = select.value;
    select.innerHTML = expertAgentOptions(value);
    if (state.agents.some((agent) => agent.id === value)) select.value = value;
  });
}

function renderExpertTeams() {
  $('expertTeamCount').textContent = `${state.expertTeams.length} 个团队`;
  $('expertTeamList').innerHTML = state.expertTeams.map((item) => `
    <button class="card expert-team-card ${state.selectedExpertTeam?.id === item.id ? 'active' : ''} ${item.enabled ? '' : 'disabled'}" data-expert-team="${escapeHtml(item.id)}" type="button">
      <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.enabled ? '已启用' : '已停用'}</span></div>
      <div class="card-desc">${escapeHtml(item.description || '未填写团队说明')}</div>
      <div class="expert-card-meta"><span>${(item.members || []).length} 位成员</span><span>主管 ${escapeHtml(item.supervisor_agent_id)}</span></div>
    </button>
  `).join('') || '<div class="meta empty">还没有专家团。请先准备主管和至少两位成员。</div>';
  $('expertTeamList').querySelectorAll('[data-expert-team]').forEach((element) => {
    element.onclick = () => selectExpertTeam(element.dataset.expertTeam).catch((err) => notify(`团队读取失败：${err.message || err}`, 'error'));
  });
  renderWorkbenchTeamOptions();
  renderWorkbenchMode();
}

function scrollExpertTeamCardIntoView(id) {
  if (!id) return;
  const card = document.querySelector(`[data-expert-team="${CSS.escape(id)}"]`);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function defaultExpertTeamMembers() {
  const agents = orderedExpertAgents();
  return [0, 1].map((index) => ({
    id: '',
    agent_id: agents[index]?.id || '',
    role: index === 0 ? '分析专家' : '审查专家',
    member_prompt: index === 0 ? '独立分析目标，给出事实依据、关键结论和待验证事项。' : '从风险、遗漏和可执行性角度独立审查目标并给出建议。',
    permissions: { read_only: true },
  }));
}

function snapshotExpertTeamMembers() {
  return [...$('expertTeamMembers').querySelectorAll('[data-team-member-row]')].map((row) => ({
    id: row.dataset.memberId || '',
    agent_id: row.querySelector('[data-member-agent]').value,
    role: row.querySelector('[data-member-role]').value,
    member_prompt: row.querySelector('[data-member-prompt]').value,
    permissions_text: row.querySelector('[data-member-permissions]').value,
  }));
}

function renderExpertTeamMembers(members) {
  const items = Array.isArray(members) ? members : [];
  $('expertMemberCount').textContent = `${items.length} 位 · 至少 2 位`;
  $('expertTeamMembers').innerHTML = items.map((member, index) => `
    <div class="expert-member-row" data-team-member-row data-member-id="${escapeHtml(member.id || '')}">
      <div class="expert-member-index"><span>${String(index + 1).padStart(2, '0')}</span><strong>${escapeHtml(member.role || '成员')}</strong></div>
      <div class="expert-member-fields">
        <div class="form-grid two">
          <label>成员智能体<select data-member-agent>${expertAgentOptions(member.agent_id || '')}</select></label>
          <label>团队角色<input data-member-role value="${escapeHtml(member.role || '')}" placeholder="例如：数据分析专家" /></label>
        </div>
        <label>成员提示 / 分工<textarea data-member-prompt rows="3" placeholder="说明该成员独立负责的范围和交付要求。">${escapeHtml(member.member_prompt || '')}</textarea></label>
        <label>成员权限 JSON<textarea data-member-permissions class="code" rows="3">${escapeHtml(member.permissions_text ?? formatJson(member.permissions || {}))}</textarea></label>
      </div>
      <button class="icon-button secondary expert-member-remove" data-remove-expert-member="${index}" type="button" title="移除成员" ${items.length <= 2 ? 'disabled' : ''}>×</button>
    </div>
  `).join('');
  $('expertTeamMembers').querySelectorAll('[data-member-role]').forEach((input) => {
    input.oninput = () => { const row = input.closest('[data-team-member-row]'); row.querySelector('.expert-member-index strong').textContent = input.value || '成员'; };
  });
  $('expertTeamMembers').querySelectorAll('[data-remove-expert-member]').forEach((button) => {
    button.onclick = () => {
      const drafts = snapshotExpertTeamMembers();
      drafts.splice(Number(button.dataset.removeExpertMember), 1);
      renderExpertTeamMembers(drafts);
    };
  });
}

function addExpertTeamMember() {
  const drafts = snapshotExpertTeamMembers();
  if (drafts.length >= 20) return notify('一个团队最多配置 20 位成员', 'error');
  const used = new Set(drafts.map((item) => item.agent_id));
  const candidate = orderedExpertAgents().find((agent) => !used.has(agent.id));
  drafts.push({ id: '', agent_id: candidate?.id || '', role: `成员 ${drafts.length + 1}`, member_prompt: '', permissions_text: '{\n  "read_only": true\n}' });
  renderExpertTeamMembers(drafts);
}

function newExpertTeam({ preserveLists = false } = {}) {
  clearExpertRunPolling();
  state.selectedExpertTeam = null;
  state.selectedExpertRun = null;
  state.expertTeamRuns = [];
  if (!preserveLists) renderExpertTeams();
  $('expertTeamEditorTitle').textContent = '新建专家团';
  $('expertTeamEditorMeta').textContent = '主管负责最终汇总与目标验收';
  $('expertTeamId').disabled = false;
  $('expertTeamId').value = '';
  $('expertTeamName').value = '';
  $('expertTeamSupervisor').innerHTML = expertAgentOptions(state.agents.find((agent) => agent.id === 'general-agent')?.id || orderedExpertAgents()[0]?.id || '');
  $('expertTeamDescription').value = '';
  $('expertTeamAggregationPrompt').value = '整合成员独立交付，明确一致结论、分歧、风险和最终建议，并逐项检查验收标准。';
  $('expertTeamAcceptance').value = formatJson(['所有成员分工均有可核验结论', '最终汇总直接回应共同目标']);
  $('expertTeamPermissions').value = '{}';
  $('expertTeamVisibility').value = 'organization';
  $('expertTeamEnabled').checked = true;
  $('deleteExpertTeamBtn').classList.add('hidden');
  $('saveExpertTeamBtn').disabled = false;
  renderExpertTeamMembers(defaultExpertTeamMembers());
  renderExpertRunControls();
  renderExpertRunHistory();
  resetExpertRunLive();
}

async function selectExpertTeam(id, { reload = true, loadRuns = true } = {}) {
  const item = reload
    ? await api(`/api/expert-teams/${encodeURIComponent(id)}?${expertQuery()}`)
    : state.expertTeams.find((team) => team.id === id);
  if (!item) return;
  clearExpertRunPolling();
  state.selectedExpertTeam = item;
  const index = state.expertTeams.findIndex((team) => team.id === item.id);
  if (index >= 0) state.expertTeams[index] = item;
  renderExpertTeams();
  const owned = expertOwns(item);
  $('expertTeamEditorTitle').textContent = item.name;
  $('expertTeamEditorMeta').textContent = `${item.id} · ${expertVisibilityLabels[item.visibility] || item.visibility}${owned ? '' : ' · 只读团队'}`;
  $('expertTeamId').disabled = true;
  $('expertTeamId').value = item.id;
  $('expertTeamName').value = item.name || '';
  $('expertTeamSupervisor').innerHTML = expertAgentOptions(item.supervisor_agent_id);
  $('expertTeamSupervisor').value = item.supervisor_agent_id;
  $('expertTeamVisibility').value = item.visibility || 'organization';
  $('expertTeamDescription').value = item.description || '';
  $('expertTeamAggregationPrompt').value = item.aggregation_prompt || '';
  $('expertTeamAcceptance').value = formatJson(item.acceptance || []);
  $('expertTeamPermissions').value = formatJson(item.permissions || {});
  $('expertTeamEnabled').checked = !!item.enabled;
  $('deleteExpertTeamBtn').classList.toggle('hidden', !owned);
  $('saveExpertTeamBtn').disabled = !owned;
  renderExpertTeamMembers(item.members || []);
  renderExpertRunControls();
  if (loadRuns) await loadExpertTeamRuns(item.id, { preserveSelection: true });
  scrollExpertTeamCardIntoView(id);
}

function expertTeamMembersPayload() {
  const drafts = snapshotExpertTeamMembers();
  if (drafts.length < 2) throw new Error('专家团至少需要两位成员');
  const members = drafts.map((member, index) => {
    if (!member.agent_id) throw new Error(`请选择第 ${index + 1} 位成员的智能体`);
    if (!member.role.trim()) throw new Error(`请填写第 ${index + 1} 位成员的团队角色`);
    let permissions;
    try { permissions = JSON.parse(member.permissions_text.trim() || '{}'); }
    catch (_) { throw new Error(`第 ${index + 1} 位成员的权限不是有效 JSON`); }
    if (!permissions || Array.isArray(permissions) || typeof permissions !== 'object') throw new Error(`第 ${index + 1} 位成员的权限必须是 JSON 对象`);
    return {
      ...(member.id ? { id: member.id } : {}),
      agent_id: member.agent_id,
      role: member.role.trim(),
      execution_mode: 'parallel',
      depends_on: [],
      member_prompt: member.member_prompt.trim(),
      position: index,
      permissions,
    };
  });
  if (new Set(members.map((member) => member.agent_id)).size !== members.length) throw new Error('同一个智能体不能作为多个并行成员');
  return members;
}

function expertTeamPayload() {
  const scope = expertScopeValues();
  const id = $('expertTeamId').value.trim();
  const name = $('expertTeamName').value.trim();
  validateExpertId(id, '团队 ID');
  if (!name) throw new Error('请填写团队名称');
  if (!$('expertTeamSupervisor').value) throw new Error('请选择主管智能体');
  return {
    id, name,
    description: $('expertTeamDescription').value.trim(),
    supervisor_agent_id: $('expertTeamSupervisor').value,
    aggregation_prompt: $('expertTeamAggregationPrompt').value.trim(),
    acceptance: parseExpertJson('expertTeamAcceptance', '验收标准', 'array'),
    budget: state.selectedExpertTeam?.budget || {},
    members: expertTeamMembersPayload(),
    organization_id: scope.organization_id,
    workspace_id: scope.workspace_id,
    owner_user_id: scope.user_id,
    visibility: $('expertTeamVisibility').value,
    permissions: parseExpertJson('expertTeamPermissions', '团队权限'),
    enabled: $('expertTeamEnabled').checked,
  };
}

async function saveExpertTeam() {
  const button = $('saveExpertTeamBtn'); setBusy(button, true);
  try {
    const payload = expertTeamPayload();
    let saved;
    if (state.selectedExpertTeam) {
      const { id, organization_id, workspace_id, owner_user_id, ...changes } = payload;
      saved = await api(`/api/expert-teams/${encodeURIComponent(id)}?${expertQuery()}`, { method: 'PUT', body: JSON.stringify(changes) });
    } else {
      saved = await api('/api/expert-teams', { method: 'POST', body: JSON.stringify(payload) });
    }
    state.expertTeams = await api(`/api/expert-teams?${expertQuery({ includeDisabled: true })}`);
    await selectExpertTeam(saved.id, { reload: false, loadRuns: true });
    notify(`专家团“${saved.name}”已保存`);
  } catch (err) { notify(`团队保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteExpertTeam() {
  const item = state.selectedExpertTeam;
  if (!item || !confirm(`确定删除专家团“${item.name}”吗？已有运行审计记录的团队不能删除，可取消“启用团队”后保存。`)) return;
  try {
    await api(`/api/expert-teams/${encodeURIComponent(item.id)}?${expertQuery()}`, { method: 'DELETE' });
    state.expertTeams = await api(`/api/expert-teams?${expertQuery({ includeDisabled: true })}`);
    newExpertTeam();
    notify('专家团已删除');
  } catch (err) { notify(`团队删除失败：${err.message || err}`, 'error'); }
}

function renderExpertRunModelOptions() {
  const select = $('expertRunModel');
  const previous = select.value || readPreference('model') || 'deterministic';
  const models = state.models.filter((model) => model.enabled);
  select.innerHTML = models.map((model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`).join('');
  if (models.some((model) => model.id === previous)) select.value = previous;
  else if (models.length) select.value = models[0].id;
}

function renderExpertRunControls() {
  const team = state.selectedExpertTeam;
  $('expertRunTeamName').value = team ? team.name : '尚未选择';
  $('runExpertTeamBtn').disabled = !team || !team.enabled;
  $('expertRunLiveMeta').textContent = team ? `${team.members?.length || 0} 位成员 · ${team.enabled ? '可运行' : '已停用'}` : '等待选择团队';
  renderExpertRunModelOptions();
}

function expertRunStatusLabel(status) {
  return expertRunStatusLabels[status] || status || '未知';
}

function expertRunIsActive(status) {
  return ['queued', 'running', 'aggregating'].includes(status);
}

function latestExpertMemberRuns(memberRuns) {
  const latest = new Map();
  for (const item of memberRuns || []) {
    const current = latest.get(item.member_id);
    if (!current || Number(item.attempt || 0) >= Number(current.attempt || 0)) latest.set(item.member_id, item);
  }
  return [...latest.values()];
}

function renderExpertRunHistory() {
  $('expertRunCount').textContent = String(state.expertTeamRuns.length);
  $('expertRunList').innerHTML = state.expertTeamRuns.map((run) => {
    const latest = latestExpertMemberRuns(run.member_runs);
    const completed = latest.filter((member) => member.status === 'completed').length;
    return `
      <button class="expert-run-history-item ${state.selectedExpertRun?.id === run.id ? 'active' : ''}" data-expert-run="${escapeHtml(run.id)}" type="button">
        <span class="expert-run-dot ${runtimeStatusClass(run.status)}"></span>
        <span><strong>${escapeHtml(expertRunStatusLabel(run.status))}</strong><small>${completed}/${latest.length || state.selectedExpertTeam?.members?.length || 0} 位成员完成 · ${escapeHtml(runtimeTime(run.created_at))}</small></span>
        <small>查看 →</small>
      </button>`;
  }).join('') || `<div class="meta empty">${state.selectedExpertTeam ? '该团队还没有运行记录。' : '选择团队后查看运行历史。'}</div>`;
  $('expertRunList').querySelectorAll('[data-expert-run]').forEach((button) => {
    button.onclick = () => selectExpertRun(button.dataset.expertRun).catch((err) => notify(`运行读取失败：${err.message || err}`, 'error'));
  });
}

function resetExpertRunLive(message = '选择并保存团队，然后提交一个共同目标。') {
  $('expertRunLive').innerHTML = `<div class="expert-empty-state"><strong>尚未开始运行</strong><span>${escapeHtml(message)}</span></div>`;
}

function renderExpertRunLive() {
  const run = state.selectedExpertRun;
  if (!run) return resetExpertRunLive();
  const team = state.selectedExpertTeam;
  const latest = latestExpertMemberRuns(run.member_runs);
  const completed = latest.filter((member) => member.status === 'completed').length;
  const total = latest.length || team?.members?.length || 0;
  const progress = total ? Math.round((completed / total) * 100) : 0;
  const retryAllowed = ['partial_failed', 'failed'].includes(run.status);
  const supervisor = run.result?.supervisor || {};
  const summary = run.result?.summary || supervisor.summary || '';
  const runError = run.error?.message || '';
  $('expertRunLiveMeta').textContent = expertRunIsActive(run.status) ? '每秒刷新运行状态' : `${expertRunStatusLabel(run.status)} · ${runtimeTime(run.updated_at || run.finished_at)}`;
  $('expertRunLive').innerHTML = `
    <div class="expert-run-live-header">
      <div><span class="status ${runtimeStatusClass(run.status)}">${escapeHtml(expertRunStatusLabel(run.status))}</span><h2>${escapeHtml(team?.name || run.team_id)}</h2><p>${escapeHtml(run.id)} · 创建于 ${escapeHtml(runtimeTime(run.created_at))}</p></div>
      ${run.parent_task_id ? `<button class="secondary" data-team-parent-task="${escapeHtml(run.parent_task_id)}" type="button">查看总任务</button>` : ''}
    </div>
    <div class="expert-progress"><div><span>成员进度</span><strong>${completed}/${total}</strong></div><i><b style="width:${progress}%"></b></i></div>
    <div class="expert-live-section">
      <div class="expert-live-section-title"><h3>并行成员</h3><span>每位成员使用独立上下文</span></div>
      <div class="expert-member-run-grid">
        ${latest.map((memberRun) => {
          const member = (team?.members || []).find((value) => value.id === memberRun.member_id) || {};
          const delivery = memberRun.output?.summary || '';
          const error = memberRun.error?.message || '';
          const canRetry = retryAllowed && ['failed', 'cancelled'].includes(memberRun.status);
          return `<article class="expert-member-run ${runtimeStatusClass(memberRun.status)}">
            <div class="expert-member-run-head"><div><strong>${escapeHtml(member.role || memberRun.input?.role || memberRun.member_id)}</strong><span>第 ${Number(memberRun.attempt || 1)} 次运行</span></div><span class="status ${runtimeStatusClass(memberRun.status)}">${escapeHtml(expertRunStatusLabel(memberRun.status))}</span></div>
            <div class="expert-child-task"><span>子任务</span><code>${escapeHtml(memberRun.child_task_id || '等待创建')}</code>${memberRun.child_task_id ? `<button class="text-button" data-team-child-task="${escapeHtml(memberRun.child_task_id)}" type="button">打开</button>` : ''}</div>
            ${delivery ? `<div class="expert-delivery"><strong>交付摘要</strong>${renderMarkdown(delivery)}</div>` : error ? `<div class="expert-member-error">${escapeHtml(error)}</div>` : '<div class="expert-member-waiting">正在等待成员交付…</div>'}
            ${canRetry ? `<button class="secondary expert-retry-member" data-retry-team-member="${escapeHtml(memberRun.id)}" type="button">仅重试此成员</button>` : ''}
          </article>`;
        }).join('') || '<div class="meta empty">成员任务正在创建，请稍候。</div>'}
      </div>
    </div>
    <div class="expert-live-section supervisor-summary">
      <div class="expert-live-section-title"><h3>主管汇总</h3><span>${run.supervisor_child_task_id ? '主管任务已创建' : expertRunIsActive(run.status) ? '成员完成后开始' : '暂无主管交付'}</span></div>
      ${summary ? `<div class="expert-supervisor-answer">${renderMarkdown(summary)}</div>` : `<div class="expert-supervisor-placeholder">${runError ? escapeHtml(runError) : run.status === 'aggregating' ? '主管正在整合成员结论并执行最终验收…' : run.status === 'partial_failed' ? '失败成员完成独立重试后，主管会重新汇总。' : '等待全部成员完成。'}</div>`}
      ${run.supervisor_child_task_id ? `<button class="text-button" data-team-child-task="${escapeHtml(run.supervisor_child_task_id)}" type="button">查看主管任务</button>` : ''}
    </div>`;
  if ($('timelineSection')) $('timelineSection').open = true;
  if ($('timelineStatus')) $('timelineStatus').textContent = `执行计划 · ${nodes.length} 个节点`;
  $('expertRunLive').querySelectorAll('[data-team-child-task], [data-team-parent-task]').forEach((button) => {
    button.onclick = () => openTask(button.dataset.teamChildTask || button.dataset.teamParentTask);
  });
  $('expertRunLive').querySelectorAll('[data-retry-team-member]').forEach((button) => {
    button.onclick = () => retryExpertTeamMember(button.dataset.retryTeamMember, button);
  });
}

function updateExpertRunState(run) {
  state.selectedExpertRun = run;
  const index = state.expertTeamRuns.findIndex((item) => item.id === run.id);
  if (index >= 0) state.expertTeamRuns[index] = run;
  else state.expertTeamRuns.unshift(run);
  renderExpertRunHistory();
  renderExpertRunLive();
}

function clearExpertRunPolling() {
  if (state.expertRunPollTimer) clearTimeout(state.expertRunPollTimer);
  state.expertRunPollTimer = null;
  state.expertRunPollToken += 1;
}

function startExpertRunPolling(runId, { graceMs = 0 } = {}) {
  clearExpertRunPolling();
  const token = state.expertRunPollToken;
  const graceUntil = Date.now() + graceMs;
  let sawActive = false;
  const poll = async () => {
    if (token !== state.expertRunPollToken || state.selectedExpertRun?.id !== runId) return;
    try {
      const run = await api(`/api/expert-team-runs/${encodeURIComponent(runId)}?${expertQuery()}`);
      if (token !== state.expertRunPollToken || state.selectedExpertRun?.id !== runId) return;
      updateExpertRunState(run);
      if (expertRunIsActive(run.status)) sawActive = true;
      if (expertRunIsActive(run.status) || (!sawActive && Date.now() < graceUntil)) {
        state.expertRunPollTimer = setTimeout(poll, 900);
      }
    } catch (_) {
      if (token !== state.expertRunPollToken) return;
      $('expertRunLiveMeta').textContent = '状态刷新暂时中断，正在重试';
      if (expertRunIsActive(state.selectedExpertRun?.status) || Date.now() < graceUntil) state.expertRunPollTimer = setTimeout(poll, 1500);
    }
  };
  state.expertRunPollTimer = setTimeout(poll, 150);
}

async function loadExpertTeamRuns(teamId, { preserveSelection = false } = {}) {
  const selectedRunId = preserveSelection ? state.selectedExpertRun?.id : '';
  state.expertTeamRuns = await api(`/api/expert-teams/${encodeURIComponent(teamId)}/runs?${expertQuery()}`);
  const candidate = state.expertTeamRuns.find((run) => run.id === selectedRunId) || state.expertTeamRuns[0];
  if (candidate) await selectExpertRun(candidate.id, { reload: true });
  else {
    clearExpertRunPolling();
    state.selectedExpertRun = null;
    renderExpertRunHistory();
    resetExpertRunLive('该团队还没有运行记录，请提交一个共同目标。');
  }
}

async function selectExpertRun(id, { reload = true } = {}) {
  const run = reload
    ? await api(`/api/expert-team-runs/${encodeURIComponent(id)}?${expertQuery()}`)
    : state.expertTeamRuns.find((item) => item.id === id);
  if (!run) return;
  clearExpertRunPolling();
  updateExpertRunState(run);
  if (expertRunIsActive(run.status)) startExpertRunPolling(run.id);
}

async function runSelectedExpertTeam() {
  const team = state.selectedExpertTeam;
  if (!team) return notify('请先选择并保存专家团', 'error');
  const message = $('expertRunGoal').value.trim();
  if (!message) return notify('请填写团队共同目标', 'error');
  const button = $('runExpertTeamBtn'); setBusy(button, true, '提交中…');
  try {
    const payload = {
      message,
      model_id: $('expertRunModel').value || null,
      conversation_id: createConversationId(),
      ...expertScopeValues(),
    };
    const accepted = await api(`/api/expert-teams/${encodeURIComponent(team.id)}/runs`, { method: 'POST', body: JSON.stringify(payload) });
    state.selectedExpertRun = accepted.team_run;
    state.expertTeamRuns = [accepted.team_run, ...state.expertTeamRuns.filter((run) => run.id !== accepted.team_run.id)];
    updateExpertRunState(accepted.team_run);
    startExpertRunPolling(accepted.team_run.id);
    notify('团队任务已提交，成员将并行执行');
  } catch (err) { notify(`团队运行提交失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function retryExpertTeamMember(memberRunId, button) {
  const run = state.selectedExpertRun;
  if (!run) return;
  setBusy(button, true, '提交中…');
  try {
    await api(`/api/expert-team-runs/${encodeURIComponent(run.id)}/members/${encodeURIComponent(memberRunId)}/retry?${expertQuery()}`, {
      method: 'POST', body: JSON.stringify({ note: '从专家团运行页面独立重试失败成员' }),
    });
    $('expertRunLiveMeta').textContent = '成员重试已提交，正在刷新';
    startExpertRunPolling(run.id, { graceMs: 5000 });
    notify('失败成员已单独提交重试，其他成员结果会保留');
  } catch (err) {
    notify(`成员重试失败：${err.message || err}`, 'error');
    setBusy(button, false);
  }
}

const memoryScopeLabels = { organization: '当前实例', workspace: '当前工作区', user: '本机使用者', agent: '智能体', conversation: '对话' };
const knowledgeVisibilityLabels = { organization: '当前实例', workspace: '当前工作区', private: '仅本机使用者' };

function memoryScopeValues() {
  return {
    ...platformScopeValues(),
    agent_id: $('agentSelect')?.value || readPreference('agent') || 'general-agent',
    conversation_id: state.conversationId,
  };
}

function memoryQuery() {
  return new URLSearchParams(memoryScopeValues()).toString();
}

function knowledgeScopeValues() {
  const { organization_id, workspace_id, user_id } = memoryScopeValues();
  return { organization_id, workspace_id, user_id };
}

function knowledgeQuery(extra = {}) {
  return new URLSearchParams({ ...knowledgeScopeValues(), ...extra }).toString();
}

async function loadKnowledgeBasesOnly({ preserveSelection = false } = {}) {
  const selectedId = preserveSelection ? state.selectedKnowledgeBase?.id : '';
  state.knowledgeBases = await api(`/api/knowledge-bases?${knowledgeQuery()}`);
  state.selectedKnowledgeBase = selectedId
    ? state.knowledgeBases.find((item) => item.id === selectedId) || null
    : state.selectedKnowledgeBase && state.knowledgeBases.find((item) => item.id === state.selectedKnowledgeBase.id) || null;
  renderKnowledgeBases();
  renderKnowledgeCapability();
  if (state.selectedKnowledgeBase) await selectKnowledgeBase(state.selectedKnowledgeBase.id, { reload: false });
  else newKnowledgeBase({ clearListSelection: false });
}

function renderKnowledgeCapability() {
  const info = state.capabilities?.knowledge_base;
  if (!$('knowledgeCapabilityStatus')) return;
  if (!info?.supported) {
    $('knowledgeCapabilityStatus').textContent = '知识库服务未启用';
    $('knowledgeCapabilityStatus').classList.add('disabled');
    return;
  }
  $('knowledgeCapabilityStatus').classList.remove('disabled');
  const formats = Array.isArray(info.formats) ? info.formats.slice(0, 8).join('、') : '常见文档';
  $('knowledgeCapabilityStatus').textContent = `已启用 · ${info.retrieval || 'keyword'} · ${formats}`;
}

function renderKnowledgeBases() {
  const items = state.knowledgeBases || [];
  $('knowledgeBaseCount').textContent = String(items.length);
  $('knowledgeBaseList').innerHTML = items.map((item) => `
    <div class="card knowledge-base-card ${state.selectedKnowledgeBase?.id === item.id ? 'active' : ''} ${item.enabled ? '' : 'disabled'}" data-knowledge-base="${escapeHtml(item.id)}">
      <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.enabled ? '已启用' : '已停用'}</span></div>
      <div class="card-desc">${escapeHtml(item.description || '暂无描述')}</div>
      <div class="memory-card-meta"><span>${escapeHtml(knowledgeVisibilityLabels[item.visibility] || item.visibility)}</span><span>${escapeHtml(item.id)}</span></div>
    </div>
  `).join('') || '<div class="meta empty">还没有知识库。点击“新建知识库”开始。</div>';
  document.querySelectorAll('[data-knowledge-base]').forEach((element) => {
    element.onclick = () => selectKnowledgeBase(element.dataset.knowledgeBase).catch((err) => notify(`知识库读取失败：${err.message || err}`, 'error'));
  });
}

function scrollKnowledgeBaseCardIntoView(id) {
  if (!id) return;
  const card = document.querySelector(`[data-knowledge-base="${CSS.escape(id)}"]`);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function renderKnowledgeDocuments() {
  const items = state.knowledgeDocuments || [];
  $('knowledgeDocumentCount').textContent = String(items.length);
  $('knowledgeDocumentList').innerHTML = items.map((item) => `
    <div class="knowledge-document">
      <div><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.mime_type || 'application/octet-stream')} · ${item.chunk_count || 0} 个片段</span></div>
      <span class="status ${item.status === 'indexed' ? 'completed' : ''}">${escapeHtml(item.status || 'unknown')}</span>
    </div>
  `).join('') || '<div class="meta empty">暂无已索引文档。</div>';
}

function renderKnowledgeSearchResults() {
  const items = state.knowledgeSearchResults || [];
  $('knowledgeSearchCount').textContent = String(items.length);
  $('knowledgeSearchResults').innerHTML = items.map((item, index) => `
    <div class="knowledge-result">
      <div class="knowledge-result-head"><strong>${index + 1}. ${escapeHtml(item.document_name || item.document_id)}</strong><span>片段 ${(Number(item.ordinal) || 0) + 1} · 分数 ${escapeHtml(item.score)}</span></div>
      <p>${escapeHtml(item.content || '')}</p>
      <div class="knowledge-result-meta">chunk_id=${escapeHtml(item.chunk_id || '')} · 命中：${escapeHtml((item.matched_terms || []).join('、') || '—')}</div>
    </div>
  `).join('') || '<div class="meta empty">没有命中片段。可以换更具体的关键词，或确认文档是否已成功索引。</div>';
}

async function loadDiagnosticsOnly() {
  state.diagnostics = await api(`/api/diagnostics?${new URLSearchParams(platformScopeValues()).toString()}`);
  renderDiagnostics();
  return state.diagnostics;
}

async function copyTextToClipboard(text) {
  if (!text) throw new Error('没有可复制的内容');
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  document.body.appendChild(textarea);
  textarea.select();
  const ok = document.execCommand('copy');
  document.body.removeChild(textarea);
  if (!ok) throw new Error('浏览器拒绝复制');
}

async function copyDiagnosticsReport() {
  if (!state.diagnostics) {
    await loadDiagnosticsOnly();
  }
  const report = state.diagnostics?.report_markdown || '';
  await copyTextToClipboard(report);
  notify('平台自检报告已复制');
}

function diagnosticsExportStamp() {
  const value = state.diagnostics?.generated_at || new Date().toISOString();
  return String(value).replace(/[:.]/g, '-').replace(/Z$/, '').slice(0, 19);
}

function downloadTextFile({ content, filename, type }) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function downloadDiagnosticsReport() {
  if (!state.diagnostics) {
    await loadDiagnosticsOnly();
  }
  const report = state.diagnostics?.report_markdown || '';
  if (!report) throw new Error('没有可下载的报告内容');
  const stamp = diagnosticsExportStamp();
  downloadTextFile({ content: report, filename: `agentnexus-diagnostics-${stamp}.md`, type: 'text/markdown;charset=utf-8' });
  notify('平台自检报告已开始下载');
}

async function downloadDiagnosticsJson() {
  if (!state.diagnostics) {
    await loadDiagnosticsOnly();
  }
  if (!state.diagnostics) throw new Error('没有可下载的自检数据');
  const stamp = diagnosticsExportStamp();
  const content = JSON.stringify(state.diagnostics, null, 2);
  downloadTextFile({ content, filename: `agentnexus-diagnostics-${stamp}.json`, type: 'application/json;charset=utf-8' });
  notify('平台自检数据已开始下载');
}

function diagnosticLabel(status) {
  if (status === 'pass') return '通过';
  if (status === 'fail') return '失败';
  return '提醒';
}

function readinessLabel(state) {
  if (state === 'ready') return '可使用';
  if (state === 'blocked') return '需处理';
  return '待配置';
}

function improvementStatusLabel(status) {
  if (status === 'ready') return '基本就绪';
  if (status === 'in_progress') return '建设中';
  return '待开发';
}

async function focusModelConfiguration({ preferProblem = true } = {}) {
  switchTab('models');
  try {
    state.models = await api('/api/models');
    renderModels();
    renderTaskModelSelect();
  } catch (err) {
    notify(`模型列表刷新失败：${err.message || err}`, 'error');
  }
  const problemModel = state.models.find((model) => (
    model.id !== 'deterministic'
    && model.enabled
    && (model.readiness?.state || 'ready') !== 'ready'
  ));
  const unconfiguredModel = state.models.find((model) => model.id !== 'deterministic');
  const target = preferProblem ? (problemModel || unconfiguredModel) : unconfiguredModel;
  if (target) {
    selectModel(target.id);
    const reason = target.readiness?.detail || modelCredentialText(target);
    notify(`已定位模型“${target.name}”：${reason}`);
  } else {
    newModel();
    notify('还没有在线模型配置，已打开新增模型表单');
  }
}

function isModelDiagnosticTarget(item = {}) {
  return AgentNexusDiagnosticsRouting.isModelDiagnosticTarget(item);
}

function isSkillMcpDiagnosticTarget(item = {}) {
  return AgentNexusDiagnosticsRouting.isSkillMcpDiagnosticTarget(item);
}

function diagnosticSkillMcpTarget(item = {}) {
  return AgentNexusDiagnosticsRouting.diagnosticSkillMcpTarget(item);
}

function diagnosticMcpPreference(item = {}) {
  return AgentNexusDiagnosticsRouting.diagnosticMcpPreference(item);
}

function chooseDiagnosticMcpServer(servers = [], source = {}) {
  return AgentNexusDiagnosticsRouting.chooseDiagnosticMcpServer(servers, source);
}

function marketplaceCandidates() {
  const market = state.marketplace || {};
  const skills = Array.isArray(market.skills) ? market.skills.map((item) => ({ ...item, kind: 'skill' })) : [];
  const mcps = Array.isArray(market.mcp_servers) ? market.mcp_servers.map((item) => ({ ...item, kind: 'mcp' })) : [];
  return [...skills, ...mcps];
}

async function focusSkillMcpConfiguration({ target = 'auto', source = null } = {}) {
  const resolvedTarget = target === 'auto' ? diagnosticSkillMcpTarget(source || {}) : target;
  if (resolvedTarget === 'skills') {
    switchTab('skills');
    const skills = await loadSkillsOnly();
    const targetSkill = skills.find((item) => item.enabled) || skills[0];
    if (targetSkill) {
      await selectSkill(targetSkill.id);
      notify(`已定位 Skill：“${targetSkill.name || targetSkill.id}”`);
    } else {
      newSkill();
      notify('未发现已安装 Skill，已打开新建 Skill 表单');
    }
    return;
  }
  if (resolvedTarget === 'mcp') {
    switchTab('mcp');
    const mcps = await loadMcpOnly();
    const preference = diagnosticMcpPreference(source || {});
    const targetMcp = chooseDiagnosticMcpServer(mcps, source || {});
    if (targetMcp) {
      await selectMcp(targetMcp.id);
      notify(`已定位 MCP：“${targetMcp.name || targetMcp.id}”`);
    } else {
      newMcp();
      if (preference.id || preference.tool) {
        $('mcpId').value = preference.id || 'web-search';
        $('mcpName').value = preference.id === 'web-search' ? '联网搜索 MCP' : '新的工具服务';
        $('mcpDescription').value = preference.id === 'web-search' ? '配置联网搜索 Provider 后用于实时检索。' : '';
      }
      notify(preference.id || preference.tool ? '未发现匹配的 MCP，已打开对应工具服务配置表单' : '未发现已配置 MCP，已打开新增工具服务表单');
    }
    return;
  }
  switchTab('marketplace');
  await loadMarketplaceOnly();
  const candidate = marketplaceCandidates().find((item) => !item.installed || !item.enabled) || marketplaceCandidates()[0];
  if (candidate) {
    state.marketplaceFocusId = candidate.id;
    renderMarketplace();
    notify(`已打开市场候选：${candidate.kind === 'skill' ? 'Skill' : 'MCP'}“${candidate.name || candidate.id}”`);
  } else {
    notify('市场暂无可推荐的 Skill/MCP，可在 Skill 或 MCP 页面手动创建', 'error');
  }
}

function diagnosticRouteTarget(item = {}) {
  return AgentNexusDiagnosticsRouting.diagnosticRouteTarget(item);
}

async function focusKnowledgeConfiguration() {
  switchTab('knowledge');
  await loadKnowledgeBasesOnly({ preserveSelection: true });
  const target = state.knowledgeBases.find((item) => item.enabled) || state.knowledgeBases[0];
  if (target) {
    await selectKnowledgeBase(target.id, { reload: false });
    notify(`已定位知识库：“${target.name || target.id}”`);
  } else {
    newKnowledgeBase();
    notify('未发现知识库，已打开新建知识库表单');
  }
}

async function focusArtifactWorkspace() {
  switchTab('artifacts');
  await loadArtifactsOnly({ preserveSelection: true });
  const target = state.selectedArtifact || state.artifacts[0];
  if (target) {
    await selectArtifact(target.id, { reload: false });
    notify(`已定位产物：“${target.name || target.id}”`);
  } else {
    resetArtifactPreview('当前工作区还没有生成文件。请先在工作台发起文档、表格、PPT、HTML 或 Markdown 生成任务。');
    notify('当前工作区还没有产物，已打开产物中心');
  }
}

async function focusExpertConfiguration() {
  switchTab('experts');
  await loadExpertWorkspace({ preserveSelection: true });
  const team = state.expertTeams.find((item) => item.enabled) || state.expertTeams[0];
  if (team) {
    await selectExpertTeam(team.id, { reload: false, loadRuns: true });
    notify(`已定位专家团：“${team.name || team.id}”`);
  } else if (state.expertTemplates.length) {
    await selectExpertTemplate(state.expertTemplates[0].id, { reload: false });
    newExpertTeam({ preserveLists: true });
    notify('已定位专家模板；还没有专家团，已打开新建团队表单');
  } else {
    newExpertTemplate({ preserveLists: true });
    newExpertTeam({ preserveLists: true });
    notify('未发现专家模板或团队，已打开专家配置表单');
  }
}

async function focusMemoryConfiguration() {
  switchTab('memory');
  await loadMemoriesOnly({ preserveSelection: true });
  const target = state.selectedMemory || state.memories.find((item) => item.enabled) || state.memories[0];
  if (target) {
    await selectMemory(target.id, { reload: false });
    notify(`已定位记忆：“${target.title || target.id}”`);
  } else {
    newMemory();
    notify('未发现长期记忆，已打开新建记忆表单');
  }
}

async function focusAutomationConfiguration() {
  switchTab('loops');
  await loadLoopsOnly();
  const target = state.loops.find((item) => ['active', 'running', 'queued'].includes(item.status)) || state.loops[0];
  if (target) {
    await selectLoop(target.id);
    notify(`已定位自动化：“${target.name || target.id}”`);
  } else {
    newLoop();
    notify('未发现自动化，已打开新建自动化表单');
  }
}

function focusWorkbenchConfiguration(item = {}) {
  const id = String(item.id || item.check_id || item.ref_id || '');
  switchTab('chat');
  const input = $('messageInput');
  if (input) input.focus();
  if (id === 'file.upload_context' || id === 'file_input') {
    notify('已打开工作台。点击输入框下方“添加附件”上传文件，再发送任务测试上下文读取');
  } else {
    notify('已打开工作台。可查看执行计划、节点状态、工具调用和交付校验');
  }
}

function handleDiagnosticNavigation(item = {}, fallbackTab = '') {
  const target = diagnosticRouteTarget(item) || fallbackTab;
  if (target === 'models') {
    focusModelConfiguration().catch((err) => notify(`模型定位失败：${err.message || err}`, 'error'));
    return true;
  }
  if (target === 'skill_mcp') {
    focusSkillMcpConfiguration({ source: item }).catch((err) => notify(`Skill/MCP 定位失败：${err.message || err}`, 'error'));
    return true;
  }
  if (target === 'knowledge') {
    focusKnowledgeConfiguration().catch((err) => notify(`知识库定位失败：${err.message || err}`, 'error'));
    return true;
  }
  if (target === 'artifacts') {
    focusArtifactWorkspace().catch((err) => notify(`产物定位失败：${err.message || err}`, 'error'));
    return true;
  }
  if (target === 'experts') {
    focusExpertConfiguration().catch((err) => notify(`专家团定位失败：${err.message || err}`, 'error'));
    return true;
  }
  if (target === 'memory') {
    focusMemoryConfiguration().catch((err) => notify(`记忆定位失败：${err.message || err}`, 'error'));
    return true;
  }
  if (target === 'loops') {
    focusAutomationConfiguration().catch((err) => notify(`自动化定位失败：${err.message || err}`, 'error'));
    return true;
  }
  if (target === 'chat') {
    focusWorkbenchConfiguration(item);
    return true;
  }
  if (target === 'diagnostics') {
    switchTab('diagnostics');
    notify(`已打开：${item.action_target?.label || item.action_label || item.title || '自检中心'}`);
    return true;
  }
  if (target) {
    switchTab(target);
    notify(`已打开：${item.action_target?.label || item.action_label || item.title || target}`);
    return true;
  }
  return false;
}

function useReadinessAction(id) {
  const item = (state.diagnostics?.readiness || []).find((entry) => String(entry.id) === String(id));
  const tab = item?.action_target?.tab || '';
  if (!item || !tab) {
    notify('这个能力项没有可跳转的配置入口', 'error');
    return;
  }
  handleDiagnosticNavigation(item, tab);
}

function useImprovementPrompt(id) {
  const item = (state.diagnostics?.improvement_backlog || []).find((entry) => String(entry.id) === String(id));
  const prompt = item?.prompt || '';
  const input = $('messageInput');
  if (!item || !input || !prompt.trim()) {
    notify('这个优化建议没有可填入的任务内容', 'error');
    return;
  }
  setWorkbenchMode('agent');
  switchTab('chat');
  input.value = prompt;
  input.focus();
  notify(`已填入优化任务：${item.title || item.id}`);
}

function useSelfTestPrompt(id) {
  const item = (state.diagnostics?.self_tests || []).find((entry) => String(entry.id) === String(id));
  const prompt = item?.prompt || '';
  const input = $('messageInput');
  if (!item || !input || !prompt.trim()) {
    notify('这个自测用例没有可填入的任务内容', 'error');
    return;
  }
  setWorkbenchMode(item.workbench_mode === 'expert' ? 'expert' : 'agent');
  switchTab('chat');
  input.value = prompt;
  input.focus();
  notify(`已填入${item.workbench_mode === 'expert' ? '专家' : '普通'}工作台，请确认后手动发送`);
}

function runNextAction(id) {
  const item = (state.diagnostics?.next_actions || []).find((entry) => String(entry.id) === String(id));
  if (!item) {
    notify('这个优先事项已不存在，请重新运行自检', 'error');
    return;
  }
  if (item.action_type === 'navigate' && item.target_tab) {
    handleDiagnosticNavigation(item, item.target_tab);
    return;
  }
  if (item.action_type === 'prompt' && item.prompt) {
    const input = $('messageInput');
    if (!input) {
      notify('工作台输入框不可用', 'error');
      return;
    }
    setWorkbenchMode('agent');
    switchTab('chat');
    input.value = item.prompt;
    input.focus();
    notify(`已填入：${item.title || '优先事项'}`);
    return;
  }
  if (item.kind === 'readiness') {
    useReadinessAction(item.ref_id);
    return;
  }
  if (item.kind === 'improvement') {
    useImprovementPrompt(item.ref_id);
    return;
  }
  notify('暂不支持这个优先事项类型', 'error');
}

function renderDiagnosticCapabilityEntries(data, capabilityEntryGrid, capabilityEntryCount) {
  if (!capabilityEntryGrid || !capabilityEntryCount) return;
  const checks = data?.checks || [];
  if (!checks.length) {
    capabilityEntryCount.textContent = '未检查';
    capabilityEntryGrid.innerHTML = '<div class="meta empty">运行自检后会显示平台默认支持的能力和使用入口。</div>';
    return;
  }
  const checksById = Object.fromEntries(checks.map((item) => [String(item.id || ''), item]));
  const entries = DIAGNOSTIC_CAPABILITY_ENTRIES.map((entry) => ({
    ...entry,
    check: checksById[entry.checkId] || null,
  }));
  const readyCount = entries.filter((entry) => entry.check?.status === 'pass').length;
  capabilityEntryCount.textContent = `${readyCount}/${entries.length} 可用`;
  capabilityEntryGrid.innerHTML = entries.map((entry) => {
    const status = entry.check?.status || 'warn';
    return `
      <article class="capability-entry-card ${escapeHtml(status)}">
        <div class="capability-entry-head">
          <strong>${escapeHtml(entry.label)}</strong>
          <span>${escapeHtml(diagnosticLabel(status))}</span>
        </div>
        <p>${escapeHtml(entry.check?.detail || '尚未检查到对应能力项。')}</p>
        <div class="capability-entry-footer">
          <span>${escapeHtml(entry.entry)}</span>
          <button class="secondary" data-capability-entry-tab="${escapeHtml(entry.tab)}" data-capability-entry-check="${escapeHtml(entry.checkId)}">打开</button>
        </div>
      </article>
    `;
  }).join('');
  capabilityEntryGrid.querySelectorAll('[data-capability-entry-tab]').forEach((button) => {
    button.onclick = () => {
      const source = {
        check_id: button.dataset.capabilityEntryCheck,
        target_tab: button.dataset.capabilityEntryTab,
      };
      handleDiagnosticNavigation(source, button.dataset.capabilityEntryTab);
    };
  });
}

function useDiagnosticIssueAction(id) {
  const item = (state.diagnostics?.issues || []).find((entry) => String(entry.id) === String(id));
  const tab = item?.action_target?.tab || '';
  if (!item || !tab) {
    notify('这个问题没有可跳转的处理入口', 'error');
    return;
  }
  handleDiagnosticNavigation(item, tab);
}

function useDiagnosticCheckAction(id) {
  const item = (state.diagnostics?.checks || []).find((entry) => String(entry.id) === String(id));
  if (!item) {
    notify('这个检查项已不存在，请重新运行自检', 'error');
    return;
  }
  if (!handleDiagnosticNavigation(item, diagnosticRouteTarget(item))) {
    notify('这个检查项没有可跳转的处理入口', 'error');
  }
}

function useDiagnosticIssuePrompt(id) {
  const item = (state.diagnostics?.issues || []).find((entry) => String(entry.id) === String(id));
  const prompt = item?.fix_prompt || '';
  const input = $('messageInput');
  if (!item || !input || !prompt.trim()) {
    notify('这个问题没有可填入的修复任务', 'error');
    return;
  }
  setWorkbenchMode('agent');
  switchTab('chat');
  input.value = prompt;
  input.focus();
  notify(`已填入修复任务：${item.title || item.check_id}`);
}

function renderDiagnosticSelfTests(selfTests, selfTestGrid, selfTestCount) {
  if (!selfTestGrid || !selfTestCount) return;
  const readyTests = selfTests.filter((item) => item.readiness === 'ready').length;
  const needsSetupTests = selfTests.filter((item) => item.readiness !== 'ready').length;
  const filter = state.diagnosticSelfTestFilter || 'all';
  const categoryFilter = state.diagnosticSelfTestCategory || 'all';
  const artifactFilter = state.diagnosticSelfTestArtifact || 'all';
  const categories = [...new Set(selfTests.map((item) => item.category).filter(Boolean))];
  const artifacts = [...new Set(selfTests.flatMap((item) => Array.isArray(item.artifacts) ? item.artifacts : []).filter(Boolean))];
  if (categoryFilter !== 'all' && !categories.includes(categoryFilter)) {
    state.diagnosticSelfTestCategory = 'all';
    writePreference('diagnostics-self-test-category', 'all');
  }
  if (artifactFilter !== 'all' && !artifacts.includes(artifactFilter)) {
    state.diagnosticSelfTestArtifact = 'all';
    writePreference('diagnostics-self-test-artifact', 'all');
  }
  const activeCategory = state.diagnosticSelfTestCategory || 'all';
  const activeArtifact = state.diagnosticSelfTestArtifact || 'all';
  const visibleSelfTests = selfTests.filter((item) => (filter === 'all' || item.readiness === filter)
    && (activeCategory === 'all' || item.category === activeCategory)
    && (activeArtifact === 'all' || (Array.isArray(item.artifacts) && item.artifacts.includes(activeArtifact))));
  const readinessFilterBox = $('selfTestReadinessFilter');
  if (readinessFilterBox) {
    readinessFilterBox.innerHTML = [
      `<button class="secondary" data-self-test-filter="all">全部 <span>${selfTests.length}</span></button>`,
      `<button class="secondary" data-self-test-filter="ready">可直接测 <span>${readyTests}</span></button>`,
      `<button class="secondary" data-self-test-filter="needs_setup">需准备 <span>${needsSetupTests}</span></button>`,
    ].join('');
    readinessFilterBox.querySelectorAll('[data-self-test-filter]').forEach((button) => {
      button.classList.toggle('active', button.dataset.selfTestFilter === filter);
      button.onclick = () => {
        state.diagnosticSelfTestFilter = button.dataset.selfTestFilter || 'all';
        writePreference('diagnostics-self-test-filter', state.diagnosticSelfTestFilter);
        renderDiagnostics();
      };
    });
  }
  const categoryFilterBox = $('selfTestCategoryFilter');
  if (categoryFilterBox) {
    const categoryCounts = selfTests.reduce((acc, item) => {
      if (item.category) acc[item.category] = (acc[item.category] || 0) + 1;
      return acc;
    }, {});
    categoryFilterBox.innerHTML = [
      `<button class="secondary" data-self-test-category="all">全部分类 <span>${selfTests.length}</span></button>`,
      ...categories.map((category) => `<button class="secondary" data-self-test-category="${escapeHtml(category)}">${escapeHtml(category)} <span>${Number(categoryCounts[category] || 0)}</span></button>`),
      '<button class="secondary self-test-reset" id="resetSelfTestFiltersBtn">重置筛选</button>',
    ].join('');
    categoryFilterBox.querySelectorAll('[data-self-test-category]').forEach((button) => {
      button.classList.toggle('active', button.dataset.selfTestCategory === activeCategory);
      button.onclick = () => {
        state.diagnosticSelfTestCategory = button.dataset.selfTestCategory || 'all';
        writePreference('diagnostics-self-test-category', state.diagnosticSelfTestCategory);
        renderDiagnostics();
      };
    });
    const resetButton = $('resetSelfTestFiltersBtn');
    if (resetButton) {
      resetButton.onclick = () => {
        state.diagnosticSelfTestFilter = 'all';
        state.diagnosticSelfTestCategory = 'all';
        state.diagnosticSelfTestArtifact = 'all';
        writePreference('diagnostics-self-test-filter', 'all');
        writePreference('diagnostics-self-test-category', 'all');
        writePreference('diagnostics-self-test-artifact', 'all');
        renderDiagnostics();
      };
    }
  }
  const artifactFilterBox = $('selfTestArtifactFilter');
  if (artifactFilterBox) {
    const artifactCounts = selfTests.reduce((acc, item) => {
      (Array.isArray(item.artifacts) ? item.artifacts : []).forEach((artifact) => {
        acc[artifact] = (acc[artifact] || 0) + 1;
      });
      return acc;
    }, {});
    artifactFilterBox.innerHTML = [
      `<button class="secondary" data-self-test-artifact="all">全部产物 <span>${selfTests.length}</span></button>`,
      ...artifacts.map((artifact) => `<button class="secondary" data-self-test-artifact="${escapeHtml(artifact)}">${escapeHtml(String(artifact).toUpperCase())} <span>${Number(artifactCounts[artifact] || 0)}</span></button>`),
    ].join('');
    artifactFilterBox.querySelectorAll('[data-self-test-artifact]').forEach((button) => {
      button.classList.toggle('active', button.dataset.selfTestArtifact === activeArtifact);
      button.onclick = () => {
        state.diagnosticSelfTestArtifact = button.dataset.selfTestArtifact || 'all';
        writePreference('diagnostics-self-test-artifact', state.diagnosticSelfTestArtifact);
        renderDiagnostics();
      };
    });
  }
  selfTestCount.textContent = selfTests.length ? `${readyTests} 可测 / ${needsSetupTests} 需准备 / ${visibleSelfTests.length} 当前` : '未检查';
  selfTestGrid.innerHTML = visibleSelfTests.length ? visibleSelfTests.map((item) => `
    <article class="self-test-card ${escapeHtml(item.readiness || 'needs_setup')}">
      <div class="self-test-head"><strong>${escapeHtml(item.title || item.id || '')}</strong><span>${item.readiness === 'ready' ? '可测试' : '需准备'}</span></div>
      <div class="self-test-meta">
        ${item.category ? `<span>${escapeHtml(item.category)}</span>` : ''}
        <span>${item.workbench_mode === 'expert' ? '专家模式' : '普通模式'}</span>
        ${Array.isArray(item.artifacts) && item.artifacts.length ? item.artifacts.map((entry) => `<span>${escapeHtml(String(entry).toUpperCase())}</span>`).join('') : ''}
      </div>
      <p>${escapeHtml(item.prompt || '')}</p>
      ${Array.isArray(item.requires) && item.requires.length ? `<div class="self-test-requires">${item.requires.map((entry) => `<span>${escapeHtml(entry)}</span>`).join('')}</div>` : ''}
      ${item.setup ? `<div class="self-test-setup">${escapeHtml(item.setup)}</div>` : ''}
      ${Array.isArray(item.expected) && item.expected.length ? `<ul>${item.expected.map((entry) => `<li>${escapeHtml(entry)}</li>`).join('')}</ul>` : ''}
      <div class="self-test-actions">
        <button class="secondary self-test-use" data-self-test-id="${escapeHtml(item.id || '')}">填入工作台</button>
      </div>
    </article>
  `).join('') : `<div class="meta empty">${selfTests.length ? '当前筛选下没有自测用例。' : '运行自检后会给出可复制到工作台的回归任务。'}</div>`;
  selfTestGrid.querySelectorAll('[data-self-test-id]').forEach((button) => {
    button.onclick = () => useSelfTestPrompt(button.dataset.selfTestId);
  });
}

function renderDiagnosticIssues(issues, issueGrid, issueCount) {
  if (!issueGrid || !issueCount) return;
  const blocking = issues.filter((item) => item.severity === 'blocking').length;
  issueCount.textContent = issues.length ? `${blocking} 阻塞 / ${issues.length} 项` : '无问题';
  issueGrid.innerHTML = issues.length ? issues.map((item) => `
    <article class="diagnostic-issue-card ${escapeHtml(item.severity || 'attention')}">
      <div class="diagnostic-issue-head">
        <strong>${escapeHtml(item.title || item.check_id || '')}</strong>
        <span>${escapeHtml(item.severity === 'blocking' ? '阻塞' : '提醒')}</span>
      </div>
      <p>${escapeHtml(item.detail || '')}</p>
      ${item.action ? `<div class="diagnostic-issue-action">${escapeHtml(item.action)}</div>` : ''}
      <div class="diagnostic-issue-actions">
        ${item.action_target?.tab ? `<button class="secondary diagnostic-issue-use" data-issue-id="${escapeHtml(item.id || '')}">${escapeHtml(item.action_target.label || '去处理')}</button>` : ''}
        ${item.fix_prompt ? `<button class="secondary diagnostic-issue-prompt" data-issue-prompt-id="${escapeHtml(item.id || '')}">填入修复任务</button>` : ''}
      </div>
    </article>
  `).join('') : '<div class="meta empty">当前没有失败或提醒项。</div>';
  issueGrid.querySelectorAll('[data-issue-id]').forEach((button) => {
    button.onclick = () => useDiagnosticIssueAction(button.dataset.issueId);
  });
  issueGrid.querySelectorAll('[data-issue-prompt-id]').forEach((button) => {
    button.onclick = () => useDiagnosticIssuePrompt(button.dataset.issuePromptId);
  });
}

function renderDiagnosticNextActions(nextActions, nextActionGrid, nextActionCount) {
  if (!nextActionGrid || !nextActionCount) return;
  nextActionCount.textContent = nextActions.length ? `${nextActions.length} 项` : '未检查';
  nextActionGrid.innerHTML = nextActions.length ? nextActions.map((item) => `
    <article class="next-action-card ${escapeHtml(String(item.priority || 'P1').toLowerCase())}">
      <div class="next-action-head">
        <span>${escapeHtml(item.priority || 'P1')}</span>
        <strong>${escapeHtml(item.title || item.id || '')}</strong>
      </div>
      <p>${escapeHtml(item.detail || '')}</p>
      <button class="secondary next-action-use" data-next-action-id="${escapeHtml(item.id || '')}">${escapeHtml(item.action_label || '处理')}</button>
    </article>
  `).join('') : '<div class="meta empty">当前没有必须优先处理的事项。</div>';
  nextActionGrid.querySelectorAll('[data-next-action-id]').forEach((button) => {
    button.onclick = () => runNextAction(button.dataset.nextActionId);
  });
}

function renderDiagnostics() {
  const overview = $('diagnosticsOverview');
  const list = $('diagnosticList');
  const reportPreview = $('diagnosticsReportPreview');
  const reportMeta = $('diagnosticsReportMeta');
  const issueGrid = $('diagnosticIssueGrid');
  const issueCount = $('diagnosticIssueCount');
  const capabilityEntryGrid = $('capabilityEntryGrid');
  const capabilityEntryCount = $('capabilityEntryCount');
  const nextActionGrid = $('nextActionGrid');
  const nextActionCount = $('nextActionCount');
  const readinessGrid = $('readinessGrid');
  const readinessCount = $('readinessCount');
  const improvementGrid = $('improvementGrid');
  const improvementCount = $('improvementCount');
  const selfTestGrid = $('selfTestGrid');
  const selfTestCount = $('selfTestCount');
  if (!overview || !list) return;
  const data = state.diagnostics || null;
  const summary = data?.summary || { passed: 0, warnings: 0, failed: 0 };
  const actionable = data?.actionable_summary || {};
  overview.className = `diagnostics-overview ${data?.overall || 'idle'}`;
  overview.innerHTML = `
    <div><span>整体状态</span><strong>${escapeHtml(data ? diagnosticLabel(data.overall) : '尚未运行')}</strong></div>
    <div><span>通过</span><strong>${Number(summary.passed || 0)}</strong></div>
    <div><span>提醒</span><strong>${Number(summary.warnings || 0)}</strong></div>
    <div><span>失败</span><strong>${Number(summary.failed || 0)}</strong></div>
    <div><span>待配置能力</span><strong>${Number(actionable.needs_config_capabilities || 0)}</strong></div>
    <div><span>阻塞能力</span><strong>${Number(actionable.blocked_capabilities || 0)}</strong></div>
    <div><span>P0 优化</span><strong>${Number(actionable.p0_improvements || 0)}</strong></div>
    <div><span>可测用例</span><strong>${Number(actionable.ready_self_tests || 0)}/${Number(actionable.total_self_tests || 0)}</strong></div>
  `;
  if (reportPreview && reportMeta) {
    const report = data?.report_markdown || '';
    reportPreview.textContent = report || '运行自检后会在这里预览可复制、可下载的 Markdown 报告。';
    reportMeta.textContent = report ? `${data?.generated_at || '未记录时间'} · ${report.length} 字符` : '未检查';
  }
  renderDiagnosticCapabilityEntries(data, capabilityEntryGrid, capabilityEntryCount);
  const issues = data?.issues || [];
  renderDiagnosticIssues(issues, issueGrid, issueCount);
  const nextActions = data?.next_actions || [];
  renderDiagnosticNextActions(nextActions, nextActionGrid, nextActionCount);
  const readiness = data?.readiness || [];
  if (readinessGrid && readinessCount) {
    const readyCount = readiness.filter((item) => item.state === 'ready').length;
    readinessCount.textContent = readiness.length ? `${readyCount}/${readiness.length} 可使用` : '未检查';
    readinessGrid.innerHTML = readiness.length ? readiness.map((item) => `
      <article class="readiness-card ${escapeHtml(item.state || 'needs_config')}">
        <div class="readiness-card-head">
          <strong>${escapeHtml(item.title || item.id || '')}</strong>
          <span>${escapeHtml(readinessLabel(item.state))}</span>
        </div>
        <p>${escapeHtml(item.detail || '')}</p>
        ${item.action ? `<div class="readiness-action">${escapeHtml(item.action)}</div>` : ''}
        ${item.action_target?.tab ? `<div class="readiness-actions"><button class="secondary readiness-use" data-readiness-id="${escapeHtml(item.id || '')}">${escapeHtml(item.action_target.label || '去处理')}</button></div>` : ''}
      </article>
    `).join('') : '<div class="meta empty">运行自检后会显示各项平台能力的可用状态。</div>';
    readinessGrid.querySelectorAll('[data-readiness-id]').forEach((button) => {
      button.onclick = () => useReadinessAction(button.dataset.readinessId);
    });
  }
  const improvements = data?.improvement_backlog || [];
  if (improvementGrid && improvementCount) {
    const p0Count = improvements.filter((item) => item.priority === 'P0').length;
    improvementCount.textContent = improvements.length ? `${p0Count} 个 P0 / ${improvements.length} 项` : '未检查';
    improvementGrid.innerHTML = improvements.length ? improvements.map((item) => `
      <article class="improvement-card ${escapeHtml(String(item.priority || 'P2').toLowerCase())} ${escapeHtml(item.status || 'planned')}">
        <div class="improvement-card-head">
          <div>
            <span>${escapeHtml(item.priority || 'P2')}</span>
            <strong>${escapeHtml(item.title || item.id || '')}</strong>
          </div>
          <em>${escapeHtml(improvementStatusLabel(item.status))}</em>
        </div>
        <p>${escapeHtml(item.detail || '')}</p>
        <div class="improvement-reason"><strong>原因</strong><span>${escapeHtml(item.reason || '')}</span></div>
        <div class="improvement-next"><strong>下一步</strong><span>${escapeHtml(item.next_step || '')}</span></div>
        <div class="improvement-actions">
          <button class="secondary improvement-use" data-improvement-id="${escapeHtml(item.id || '')}">填入工作台</button>
        </div>
      </article>
    `).join('') : '<div class="meta empty">运行自检后会按优先级给出下一步开发和配置建议。</div>';
    improvementGrid.querySelectorAll('[data-improvement-id]').forEach((button) => {
      button.onclick = () => useImprovementPrompt(button.dataset.improvementId);
    });
  }
  const selfTests = data?.self_tests || [];
  renderDiagnosticSelfTests(selfTests, selfTestGrid, selfTestCount);
  const checks = data?.checks || [];
  if (!checks.length) {
    list.innerHTML = '<div class="meta empty">点击“运行自检”查看平台关键能力是否可用。</div>';
    return;
  }
  list.innerHTML = checks.map((item) => {
    const evidence = item.evidence && Object.keys(item.evidence).length
      ? `<pre>${escapeHtml(JSON.stringify(item.evidence, null, 2))}</pre>`
      : '<div class="small">暂无额外证据</div>';
    return `
      <article class="diagnostic-card ${escapeHtml(item.status)}">
        <div class="diagnostic-card-head">
          <div><h2>${escapeHtml(item.title)}</h2><span>${escapeHtml(item.id)}</span></div>
          <strong>${escapeHtml(diagnosticLabel(item.status))}</strong>
        </div>
        <p>${escapeHtml(item.detail || '')}</p>
        <details>
          <summary>证据与建议</summary>
          ${evidence}
          ${item.action ? `<div class="diagnostic-action">${escapeHtml(item.action)}</div>` : ''}
          <div class="diagnostic-detail-actions">
            <button class="secondary diagnostic-check-use" data-check-id="${escapeHtml(item.id || '')}">${escapeHtml(item.action_target?.label || '去处理')}</button>
          </div>
        </details>
      </article>
    `;
  }).join('');
  list.querySelectorAll('[data-check-id]').forEach((button) => {
    button.onclick = () => useDiagnosticCheckAction(button.dataset.checkId);
  });
}

function newKnowledgeBase({ clearListSelection = true } = {}) {
  if (clearListSelection) {
    state.selectedKnowledgeBase = null;
    state.knowledgeDocuments = [];
    state.knowledgeSearchResults = [];
    renderKnowledgeBases();
  }
  $('knowledgeEditorTitle').textContent = '新建知识库';
  $('knowledgeEditorMeta').textContent = '保存后即可上传文档建立索引';
  $('knowledgeBaseId').value = '';
  $('knowledgeBaseName').value = '';
  $('knowledgeBaseDescription').value = '';
  $('knowledgeBaseVisibility').value = 'workspace';
  $('knowledgeBaseEnabled').checked = true;
  $('deleteKnowledgeBaseBtn').classList.add('hidden');
  $('knowledgeFileInput').disabled = true;
  $('knowledgeSearchInput').disabled = true;
  $('knowledgeSearchBtn').disabled = true;
  renderKnowledgeDocuments();
  renderKnowledgeSearchResults();
}

async function selectKnowledgeBase(id, { reload = true } = {}) {
  if (reload) state.knowledgeBases = await api(`/api/knowledge-bases?${knowledgeQuery()}`);
  const item = state.knowledgeBases.find((base) => base.id === id);
  if (!item) return;
  state.selectedKnowledgeBase = item;
  renderKnowledgeBases();
  $('knowledgeEditorTitle').textContent = item.name;
  $('knowledgeEditorMeta').textContent = `${item.id} · 更新于 ${runtimeTime(item.updated_at)}`;
  $('knowledgeBaseId').value = item.id;
  $('knowledgeBaseName').value = item.name || '';
  $('knowledgeBaseDescription').value = item.description || '';
  $('knowledgeBaseVisibility').value = item.visibility || 'workspace';
  $('knowledgeBaseEnabled').checked = !!item.enabled;
  $('deleteKnowledgeBaseBtn').classList.remove('hidden');
  $('knowledgeFileInput').disabled = false;
  $('knowledgeSearchInput').disabled = false;
  $('knowledgeSearchBtn').disabled = false;
  state.knowledgeDocuments = await api(`/api/knowledge-bases/${encodeURIComponent(item.id)}/documents?${knowledgeQuery()}`);
  state.knowledgeSearchResults = [];
  renderKnowledgeDocuments();
  renderKnowledgeSearchResults();
  scrollKnowledgeBaseCardIntoView(id);
}

async function saveKnowledgeBase() {
  const name = $('knowledgeBaseName').value.trim();
  if (!name) return notify('请填写知识库名称', 'error');
  const button = $('saveKnowledgeBaseBtn'); setBusy(button, true);
  try {
    const baseId = $('knowledgeBaseId').value;
    const payload = {
      name,
      description: $('knowledgeBaseDescription').value.trim(),
      visibility: $('knowledgeBaseVisibility').value,
      enabled: $('knowledgeBaseEnabled').checked,
    };
    const saved = baseId
      ? await api(`/api/knowledge-bases/${encodeURIComponent(baseId)}?${knowledgeQuery()}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      })
      : await api('/api/knowledge-bases', {
        method: 'POST',
        body: JSON.stringify({ ...knowledgeScopeValues(), ...payload }),
      });
    await loadKnowledgeBasesOnly({ preserveSelection: false });
    await selectKnowledgeBase(saved.id, { reload: false });
    notify(`知识库“${saved.name}”已保存`);
  } catch (err) { notify(`知识库保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteKnowledgeBase() {
  const item = state.selectedKnowledgeBase;
  if (!item || !confirm(`确定删除知识库“${item.name}”吗？该知识库下的文档索引和片段会一并删除，原始上传文件不受影响。`)) return;
  const button = $('deleteKnowledgeBaseBtn'); setBusy(button, true, '删除中…');
  try {
    await api(`/api/knowledge-bases/${encodeURIComponent(item.id)}?${knowledgeQuery()}`, { method: 'DELETE' });
    state.selectedKnowledgeBase = null;
    state.knowledgeDocuments = [];
    state.knowledgeSearchResults = [];
    await loadKnowledgeBasesOnly({ preserveSelection: false });
    notify('知识库已删除，后续任务不会再检索它');
  } catch (err) { notify(`知识库删除失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function indexKnowledgeFile(file) {
  const base = state.selectedKnowledgeBase;
  if (!base) return notify('请先选择或创建知识库', 'error');
  const input = $('knowledgeFileInput');
  input.disabled = true;
  try {
    const form = new FormData();
    form.append('file', file);
    const uploaded = await api('/api/uploads', { method: 'POST', body: form });
    const document = await api(`/api/knowledge-bases/${encodeURIComponent(base.id)}/documents/upload?${knowledgeQuery()}`, {
      method: 'POST',
      body: JSON.stringify({ upload_id: uploaded.id }),
    });
    state.knowledgeDocuments = [document, ...state.knowledgeDocuments.filter((item) => item.id !== document.id)];
    renderKnowledgeDocuments();
    notify(`已索引“${document.name}”，生成 ${document.chunk_count || 0} 个片段`);
  } catch (err) { notify(`文档索引失败：${err.message || err}`, 'error'); }
  finally {
    input.disabled = !state.selectedKnowledgeBase;
    input.value = '';
  }
}

async function searchKnowledge() {
  const query = $('knowledgeSearchInput').value.trim();
  if (!query) return notify('请输入检索问题或关键词', 'error');
  const baseId = state.selectedKnowledgeBase?.id || '';
  const button = $('knowledgeSearchBtn'); setBusy(button, true, '检索中…');
  try {
    const result = await api(`/api/knowledge/search?${knowledgeQuery({ q: query, base_id: baseId, limit: '8' })}`);
    state.knowledgeSearchResults = result.matches || [];
    renderKnowledgeSearchResults();
  } catch (err) { notify(`知识库检索失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function loadMemoriesOnly({ preserveSelection = false } = {}) {
  const selectedId = preserveSelection ? state.selectedMemory?.id : '';
  const [memories] = await Promise.all([
    api(`/api/memories?${memoryQuery()}`),
    loadConversationSummaries({ preserveSelection: true }),
  ]);
  state.memories = memories;
  state.selectedMemory = selectedId ? state.memories.find((item) => item.id === selectedId) || null : state.selectedMemory;
  renderMemories();
  await loadEffectiveMemoryContext();
  if (state.selectedMemory) await selectMemory(state.selectedMemory.id, { reload: false });
}

function conversationSummaryScopeQuery() {
  const { organization_id, workspace_id, user_id } = memoryScopeValues();
  return new URLSearchParams({ organization_id, workspace_id, user_id }).toString();
}

function renderConversationSummaryEditor() {
  const items = state.conversationSummaries;
  const currentId = state.conversationId;
  const selectedId = state.selectedConversationSummary?.conversation_id || currentId;
  const options = items.map((item) => item.conversation_id);
  if (!options.includes(currentId)) options.unshift(currentId);
  $('conversationSummarySelect').innerHTML = options.map((id) => {
    const item = items.find((value) => value.conversation_id === id);
    const label = id === currentId ? `当前对话 · ${id}` : `${id}${item ? ` · v${item.version}` : ''}`;
    return `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`;
  }).join('');
  $('conversationSummarySelect').value = options.includes(selectedId) ? selectedId : currentId;
  const item = items.find((value) => value.conversation_id === $('conversationSummarySelect').value) || null;
  state.selectedConversationSummary = item;
  $('conversationSummaryContent').value = item?.summary || '';
  $('conversationSummaryConstraints').value = (item?.preserved_constraints || []).join('\n');
  $('conversationSummaryThroughTask').value = item?.through_task_id || '';
  $('conversationSummaryMeta').textContent = item
    ? `版本 ${item.version} · 约 ${item.token_count || 0} tokens · 更新于 ${runtimeTime(item.updated_at)}`
    : `“${$('conversationSummarySelect').value}”尚无摘要，可人工创建或等待平台自动压缩`;
  $('deleteConversationSummaryBtn').disabled = !item;
  $('saveConversationSummaryBtn').disabled = false;
}

async function loadConversationSummaries({ preserveSelection = false } = {}) {
  const selectedId = preserveSelection
    ? state.selectedConversationSummary?.conversation_id || state.conversationId
    : state.conversationId;
  const response = await api(`/api/conversation-summaries?${conversationSummaryScopeQuery()}`);
  state.conversationSummaries = Array.isArray(response) ? response : [];
  state.selectedConversationSummary = state.conversationSummaries.find((item) => item.conversation_id === selectedId) || null;
  renderConversationSummaryEditor();
}

function selectConversationSummary(conversationId) {
  state.selectedConversationSummary = state.conversationSummaries.find((item) => item.conversation_id === conversationId) || null;
  renderConversationSummaryEditor();
  $('conversationSummarySelect').value = conversationId;
}

async function saveConversationSummary() {
  const conversationId = $('conversationSummarySelect').value || state.conversationId;
  const summary = $('conversationSummaryContent').value.trim();
  if (!summary) return notify('请填写对话摘要内容', 'error');
  const button = $('saveConversationSummaryBtn'); setBusy(button, true);
  try {
    const preservedConstraints = $('conversationSummaryConstraints').value
      .split('\n').map((item) => item.trim()).filter(Boolean);
    const existing = state.conversationSummaries.find((item) => item.conversation_id === conversationId);
    const saved = await api(`/api/conversation-summaries/${encodeURIComponent(conversationId)}?${conversationSummaryScopeQuery()}`, {
      method: 'PUT',
      body: JSON.stringify({
        summary,
        preserved_constraints: preservedConstraints,
        through_task_id: existing?.through_task_id || '',
        model_id: 'manual-editor',
      }),
    });
    const index = state.conversationSummaries.findIndex((item) => item.conversation_id === conversationId);
    if (index >= 0) state.conversationSummaries[index] = saved;
    else state.conversationSummaries.unshift(saved);
    state.selectedConversationSummary = saved;
    renderConversationSummaryEditor();
    $('conversationSummarySelect').value = conversationId;
    notify('对话摘要已保存，后续任务会把它作为较早背景使用');
  } catch (err) { notify(`摘要保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteConversationSummary() {
  const item = state.selectedConversationSummary;
  if (!item || !confirm(`确定删除对话“${item.conversation_id}”的摘要吗？原始任务记录不会删除。`)) return;
  const button = $('deleteConversationSummaryBtn'); setBusy(button, true, '删除中…');
  try {
    await api(`/api/conversation-summaries/${encodeURIComponent(item.conversation_id)}?${conversationSummaryScopeQuery()}`, { method: 'DELETE' });
    state.conversationSummaries = state.conversationSummaries.filter((value) => value.conversation_id !== item.conversation_id);
    state.selectedConversationSummary = null;
    renderConversationSummaryEditor();
    notify('对话摘要已删除，原始任务记录仍然保留');
  } catch (err) { notify(`摘要删除失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

function renderMemories() {
  const scopeFilter = $('memoryScopeFilter')?.value || '';
  const statusFilter = $('memoryStatusFilter')?.value || 'all';
  const items = state.memories.filter((item) => {
    if (scopeFilter && item.scope_type !== scopeFilter) return false;
    if (statusFilter === 'enabled' && !item.enabled) return false;
    if (statusFilter === 'disabled' && item.enabled) return false;
    return true;
  });
  $('memoryCount').textContent = String(items.length);
  $('memoryList').innerHTML = items.map((item) => `
    <div class="card memory-card ${state.selectedMemory?.id === item.id ? 'active' : ''} ${item.enabled ? '' : 'disabled'}" data-memory="${escapeHtml(item.id)}">
      <div class="card-title"><span>${escapeHtml(item.title || item.content.slice(0, 32))}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.enabled ? '已启用' : '已停用'}</span></div>
      <div class="card-desc">${escapeHtml(item.content)}</div>
      <div class="memory-card-meta"><span>${escapeHtml(memoryScopeLabels[item.scope_type] || item.scope_type)}</span><span>${escapeHtml(item.kind)}</span><span>信任 ${item.trust_level}</span></div>
    </div>
  `).join('') || '<div class="meta empty">当前筛选条件下没有记忆。</div>';
  document.querySelectorAll('[data-memory]').forEach((element) => { element.onclick = () => selectMemory(element.dataset.memory); });
}

function scrollMemoryCardIntoView(id) {
  if (!id) return;
  const card = document.querySelector(`[data-memory="${CSS.escape(id)}"]`);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function memoryScopeId(scopeType) {
  const values = memoryScopeValues();
  return ({ organization: values.organization_id, workspace: values.workspace_id, user: values.user_id, agent: values.agent_id, conversation: values.conversation_id })[scopeType] || '';
}

function syncMemoryScopeId() {
  $('memoryScopeId').value = memoryScopeId($('memoryScopeType').value);
}

function newMemory() {
  state.selectedMemory = null;
  renderMemories();
  $('memoryEditorTitle').textContent = '新建记忆';
  $('memoryId').value = '';
  $('memoryTitle').value = '';
  $('memoryKind').value = 'preference';
  $('memoryScopeType').disabled = false;
  $('memoryScopeType').value = 'user';
  syncMemoryScopeId();
  $('memoryTrust').value = '80';
  $('memoryExpiresAt').value = '';
  $('memoryContent').value = '';
  $('memoryTags').value = '';
  $('memoryEnabled').checked = true;
  $('deleteMemoryBtn').classList.add('hidden');
  $('memoryRevisionMeta').textContent = '保存后自动记录修订';
  $('memoryRevisions').innerHTML = '<div class="meta empty">选择已有记忆后查看修订记录</div>';
}

async function selectMemory(id, { reload = true } = {}) {
  if (reload) state.memories = await api(`/api/memories?${memoryQuery()}`);
  const item = state.memories.find((memory) => memory.id === id);
  if (!item) return;
  state.selectedMemory = item;
  renderMemories();
  $('memoryEditorTitle').textContent = item.title || '未命名记忆';
  $('memoryId').value = item.id;
  $('memoryTitle').value = item.title || '';
  $('memoryKind').value = item.kind || 'preference';
  $('memoryScopeType').value = item.scope_type;
  $('memoryScopeType').disabled = true;
  $('memoryScopeId').value = item.scope_id;
  $('memoryTrust').value = String(item.trust_level ?? 80);
  $('memoryExpiresAt').value = item.expires_at ? item.expires_at.slice(0, 16) : '';
  $('memoryContent').value = item.content || '';
  $('memoryTags').value = (item.tags || []).join(', ');
  $('memoryEnabled').checked = !!item.enabled;
  $('deleteMemoryBtn').classList.remove('hidden');
  const revisions = await api(`/api/memories/${encodeURIComponent(id)}/revisions?${memoryQuery()}`);
  $('memoryRevisionMeta').textContent = `${revisions.length} 个修订 · 最近更新 ${runtimeTime(item.updated_at)}`;
  $('memoryRevisions').innerHTML = revisions.slice().reverse().map((revision) => `
    <div class="memory-revision"><strong>版本 ${revision.revision}</strong><span>${escapeHtml(revision.reason)} · ${escapeHtml(runtimeTime(revision.created_at))}</span></div>
  `).join('') || '<div class="meta empty">暂无修订记录</div>';
  scrollMemoryCardIntoView(id);
}

function memoryPayload() {
  const scope = memoryScopeValues();
  return {
    ...scope,
    scope_type: $('memoryScopeType').value,
    kind: $('memoryKind').value,
    title: $('memoryTitle').value.trim(),
    content: $('memoryContent').value.trim(),
    tags: $('memoryTags').value.split(',').map((item) => item.trim()).filter(Boolean),
    trust_level: Number($('memoryTrust').value),
    enabled: $('memoryEnabled').checked,
    expires_at: $('memoryExpiresAt').value ? new Date($('memoryExpiresAt').value).toISOString() : null,
  };
}

async function saveMemory() {
  const button = $('saveMemoryBtn'); setBusy(button, true);
  try {
    const payload = memoryPayload();
    if (!payload.content) throw new Error('请填写要记住的内容');
    let saved;
    if (state.selectedMemory) {
      const { scope_type, organization_id, workspace_id, user_id, agent_id, conversation_id, ...changes } = payload;
      saved = await api(`/api/memories/${encodeURIComponent(state.selectedMemory.id)}?${memoryQuery()}`, { method: 'PUT', body: JSON.stringify({ ...changes, reason: 'ui_update' }) });
    } else {
      saved = await api('/api/memories', { method: 'POST', body: JSON.stringify(payload) });
    }
    await loadMemoriesOnly({ preserveSelection: false });
    await selectMemory(saved.id, { reload: false });
    notify(`记忆“${saved.title || saved.id}”已保存`);
  } catch (err) { notify(`记忆保存失败：${err.message || err}`, 'error'); }
  finally { setBusy(button, false); }
}

async function deleteMemory() {
  const item = state.selectedMemory;
  if (!item || !confirm(`确定删除记忆“${item.title || item.content.slice(0, 24)}”吗？修订审计会保留。`)) return;
  await api(`/api/memories/${encodeURIComponent(item.id)}?${memoryQuery()}`, { method: 'DELETE' });
  state.selectedMemory = null;
  await loadMemoriesOnly();
  newMemory();
  notify('记忆已删除，不会再注入后续任务');
}

async function loadEffectiveMemoryContext() {
  const effective = await api(`/api/context/effective?${memoryQuery()}`);
  $('memoryEffectiveContext').textContent = effective.effective_context || '当前没有生效的长期记忆。';
  $('memoryEffectiveMeta').textContent = `当前任务将使用 ${effective.used_memory_ids?.length || 0} 条记忆 · ${memoryScopeValues().agent_id}`;
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function safeArtifactDownloadUrl(value, { inline = false } = {}) {
  try {
    const url = new URL(String(value || ''), window.location.href);
    if (url.origin !== window.location.origin || !/^\/api\/artifacts\/[^/]+\/download$/.test(url.pathname)) return '';
    return inline && url.searchParams.get('inline') === 'true' ? `${url.pathname}?inline=true` : url.pathname;
  } catch (_) {
    return '';
  }
}

function safeArtifactPreviewUrl(value) {
  try {
    const url = new URL(String(value || ''), window.location.href);
    if (url.origin !== window.location.origin || !/^\/api\/artifacts\/[^/]+\/preview$/.test(url.pathname)) return '';
    return url.pathname;
  } catch (_) {
    return '';
  }
}

function resetArtifactPreview(message = '从左侧选择一个文件。') {
  $('artifactPreviewTitle').textContent = '选择文件预览';
  $('artifactPreviewMeta').textContent = '支持 Word、PDF、PPT、Excel、Markdown 和 HTML';
  const download = $('artifactPreviewDownload');
  download.removeAttribute('href');
  download.classList.add('hidden');
  $('artifactPreview').className = 'artifact-preview-empty';
  $('artifactPreview').textContent = message;
}

async function loadArtifactsOnly({ preserveSelection = false } = {}) {
  const selectedId = preserveSelection ? state.selectedArtifact?.id : '';
  const kind = $('artifactKindFilter')?.value || '';
  const query = new URLSearchParams({ workspace_id: currentWorkspaceId(), limit: '300' });
  if (kind) query.set('kind', kind);
  state.artifacts = runtimeArray(await api(`/api/artifacts?${query.toString()}`));
  state.selectedArtifact = selectedId ? state.artifacts.find((item) => item.id === selectedId) || null : null;
  renderArtifactWorkspace();
  if (state.selectedArtifact) await selectArtifact(state.selectedArtifact.id, { reload: false });
  else resetArtifactPreview();
}

function renderArtifactWorkspace() {
  $('artifactCount').textContent = String(state.artifacts.length);
  $('artifactWorkspaceList').innerHTML = state.artifacts.map((item) => {
    const kind = String(item.kind || 'file');
    const version = item.version ? `v${String(item.version)} · ` : '';
    const metadata = `${String(item.task_id || '手动生成')} · ${version}${formatBytes(item.size)}`;
    const hash = item.sha256 ? `SHA-256 ${String(item.sha256).slice(0, 12)}…` : '暂无校验值';
    return `
      <button class="artifact-workspace-item ${state.selectedArtifact?.id === item.id ? 'active' : ''}" data-workspace-artifact="${escapeHtml(item.id)}" type="button">
        <span class="artifact-file-icon">${escapeHtml(kind.slice(0, 4).toUpperCase())}</span>
        <span class="artifact-file-copy"><strong>${escapeHtml(item.name || '未命名文件')}</strong><small>${escapeHtml(metadata)}</small><small class="artifact-file-hash">${escapeHtml(hash)}</small></span>
        <span class="artifact-file-time">${escapeHtml(runtimeTime(item.created_at))}</span>
      </button>
    `;
  }).join('') || '<div class="meta empty">当前工作区还没有生成文件。</div>';
  $('artifactWorkspaceList').querySelectorAll('[data-workspace-artifact]').forEach((element) => {
    element.onclick = () => selectArtifact(element.dataset.workspaceArtifact).catch((err) => notify(`产物预览失败：${err.message || err}`, 'error'));
  });
}

function scrollArtifactCardIntoView(id) {
  if (!id) return;
  const card = document.querySelector(`[data-workspace-artifact="${CSS.escape(id)}"]`);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

async function openArtifactPreview(id) {
  if (!id) return;
  switchTab('artifacts');
  if (!state.artifacts.some((item) => item.id === id)) await loadArtifactsOnly();
  await selectArtifact(id, { reload: false });
}

async function selectArtifact(id, { reload = true } = {}) {
  if (reload && !state.artifacts.some((item) => item.id === id)) await loadArtifactsOnly();
  const item = state.artifacts.find((artifact) => artifact.id === id) || await api(`/api/artifacts/${encodeURIComponent(id)}`);
  state.selectedArtifact = item;
  renderArtifactWorkspace();
  $('artifactPreviewTitle').textContent = item.name;
  $('artifactPreviewMeta').textContent = `${String(item.kind || '').toUpperCase()} · ${formatBytes(item.size)} · v${item.version || 1}${item.sha256 ? ` · SHA-256 ${item.sha256.slice(0, 12)}…` : ''}`;
  const download = $('artifactPreviewDownload');
  const downloadUrl = safeArtifactDownloadUrl(item.download_url);
  if (downloadUrl) {
    download.href = downloadUrl;
    download.classList.remove('hidden');
  } else {
    download.removeAttribute('href');
    download.classList.add('hidden');
  }
  $('artifactPreview').className = 'artifact-preview-loading';
  $('artifactPreview').textContent = '正在生成受控预览…';
  try {
    const preview = await api(`/api/artifacts/${encodeURIComponent(id)}/preview`);
    renderArtifactPreview(preview);
  } catch (err) {
    $('artifactPreview').className = 'artifact-preview-error';
    $('artifactPreview').textContent = `预览生成失败：${err.message || err}`;
    throw err;
  }
  scrollArtifactCardIntoView(id);
}

function previewTable(rows) {
  if (!rows?.length) return '<div class="meta empty">空表格</div>';
  return `<div class="artifact-table-wrap"><table>${rows.map((row, index) => `<tr>${(row || []).map((cell) => `<${index === 0 ? 'th' : 'td'}>${escapeHtml(cell)}</${index === 0 ? 'th' : 'td'}>`).join('')}</tr>`).join('')}</table></div>`;
}

function renderArtifactPreview(preview) {
  const box = $('artifactPreview');
  const previewKind = ['markdown', 'html', 'pdf', 'spreadsheet', 'document', 'slides', 'text'].includes(preview?.preview_kind) ? preview.preview_kind : 'unknown';
  box.className = `artifact-preview ${previewKind}`;
  if (previewKind === 'markdown') {
    box.innerHTML = `<div class="artifact-markdown">${renderMarkdown(preview.content || '')}</div>`;
    return;
  }
  if (previewKind === 'html') {
    const frame = document.createElement('iframe');
    frame.className = 'artifact-frame';
    frame.title = 'HTML 文件预览';
    frame.setAttribute('sandbox', '');
    frame.referrerPolicy = 'no-referrer';
    frame.srcdoc = String(preview.content || '');
    box.replaceChildren(frame);
    return;
  }
  if (previewKind === 'pdf') {
    const source = safeArtifactDownloadUrl(preview.url, { inline: true });
    if (!source) {
      box.innerHTML = '<div class="artifact-preview-empty">PDF 预览地址无效，可下载原文件查看。</div>';
      return;
    }
    const frame = document.createElement('iframe');
    frame.className = 'artifact-frame pdf';
    frame.title = 'PDF 文件预览';
    frame.setAttribute('sandbox', '');
    frame.referrerPolicy = 'no-referrer';
    frame.src = source;
    box.replaceChildren(frame);
    return;
  }
  if (previewKind === 'spreadsheet') {
    box.innerHTML = (preview.sheets || []).map((sheet) => `<section class="artifact-sheet"><h3>${escapeHtml(sheet.name)}</h3>${previewTable(sheet.rows)}</section>`).join('') || '<div class="artifact-preview-empty">工作簿中没有可预览的数据。</div>';
    return;
  }
  if (previewKind === 'document') {
    const paragraphs = (preview.paragraphs || []).map((item) => `<p>${escapeHtml(item)}</p>`).join('');
    const tables = (preview.tables || []).map(previewTable).join('');
    box.innerHTML = paragraphs || tables ? `<article class="artifact-document">${paragraphs}${tables}</article>` : '<div class="artifact-preview-empty">文档中没有可提取的文字或表格。</div>';
    return;
  }
  if (previewKind === 'slides') {
    const slides = (preview.slides || []).map((slide) => `<section><span>${escapeHtml(slide.number)}</span><h3>${escapeHtml(slide.title)}</h3>${(slide.texts || []).slice(1).map((text) => `<p>${escapeHtml(text)}</p>`).join('')}</section>`).join('');
    box.innerHTML = slides ? `<div class="artifact-slides">${slides}</div>` : '<div class="artifact-preview-empty">演示文稿中没有可预览的页面。</div>';
    return;
  }
  if (previewKind === 'text') {
    box.innerHTML = `<pre>${escapeHtml(preview.content || '')}</pre>`;
    return;
  }
  box.innerHTML = `<div class="artifact-preview-empty">${escapeHtml(preview.message || '该格式暂不支持预览，可下载原文件。')}</div>`;
}

function renderCapabilities() {
  const capability = state.capabilities || {};
  const status = (enabled, configured = true) => {
    if (!configured) return { label: '待配置', className: 'pending' };
    return enabled ? { label: '可使用', className: 'ready' } : { label: '已关闭', className: 'off' };
  };
  const fileUpload = capability.file_upload || {};
  const webSearch = capability.web_search || {};
  const localMcp = capability.stdio_mcp || {};
  const remoteMcp = capability.remote_mcp || {};
  const documentCapability = capability.document_output || {};
  const documents = Array.isArray(documentCapability)
    ? documentCapability
    : Array.isArray(documentCapability.formats) ? documentCapability.formats : [];
  const pptxReady = Array.isArray(documentCapability)
    ? documentCapability.includes('pptx')
    : !!documentCapability.pptx_configured;
  const memory = capability.memory || {};
  const experts = capability.expert_teams || {};
  const automation = capability.automation || {};
  const cards = [
    {
      title: '文件与资料',
      detail: fileUpload.supported
        ? `单文件最大 ${fileUpload.max_mb || 20} MB · 支持 ${Array.isArray(fileUpload.text_extraction) ? fileUpload.text_extraction.map((item) => String(item).toUpperCase()).join(' / ') : '常见文档'} 正文提取`
        : '当前版本不支持文件上传',
      state: status(!!fileUpload.supported),
    },
    {
      title: '联网搜索',
      detail: webSearch.configured ? (webSearch.enabled ? '已配置并允许任务调用' : '服务已配置，可在能力开关中启用') : '需要先配置搜索服务',
      state: status(!!webSearch.enabled, !!webSearch.configured),
    },
    {
      title: '本地 MCP',
      detail: localMcp.enabled ? '允许启动白名单内的本地工具进程' : '支持接入，默认关闭进程执行',
      state: status(!!localMcp.enabled),
    },
    {
      title: '远程工具',
      detail: remoteMcp.enabled ? '远程 MCP 已开放' : '支持远程 MCP 与 HTTP 工具，默认关闭',
      state: status(!!remoteMcp.enabled),
    },
    {
      title: '文档交付',
      detail: documents.length
        ? `${documents.map((item) => String(item).toUpperCase()).join(' · ')}${pptxReady ? ' · PPTX' : ' · PPTX 待配置'}`
        : '暂无可用格式',
      state: status(documents.length > 0),
    },
    {
      title: '分层记忆',
      detail: memory.supported ? `${(memory.scopes || []).length} 类作用域 · 自动摘要与人工维护` : '当前版本未启用',
      state: status(!!memory.supported),
    },
    {
      title: '专家协作',
      detail: experts.supported ? '并行成员 · 独立上下文 · 主管汇总' : '当前版本未启用',
      state: status(!!experts.supported),
    },
    {
      title: '自动化',
      detail: automation.supported ? '定时 · Cron · 单次 · Webhook' : '当前版本未启用',
      state: status(!!automation.supported),
    },
  ];
  $('capabilities').innerHTML = `
    <div class="capability-heading"><div><h2>能力状态</h2><p>平台默认边界与当前可用能力</p></div><span>${cards.filter((item) => item.state.className === 'ready').length}/${cards.length} 可使用</span></div>
    <div class="capability-grid">${cards.map((item) => `
      <article class="capability-card">
        <div><strong>${escapeHtml(item.title)}</strong><span class="capability-state ${item.state.className}">${escapeHtml(item.state.label)}</span></div>
        <p>${escapeHtml(item.detail)}</p>
        ${item.title === '文档交付' && !pptxReady ? '<button class="text-button capability-action" data-open-pptx-config type="button">打开 PPTX 配置向导</button>' : ''}
      </article>`).join('')}
    </div>`;
  $('capabilities').querySelectorAll('[data-open-pptx-config]').forEach((button) => {
    button.onclick = () => openPptxConfigDialog().catch((err) => notify(`读取 PPTX 配置失败：${err.message || err}`, 'error'));
  });
}

function renderPptxConfigInstructions(config = {}) {
  const instructions = config.instructions || {};
  const renderList = (title, values) => `<section><strong>${escapeHtml(title)}</strong><div>${runtimeArray(values).map((item, index) => `${index + 1}. ${escapeHtml(item)}`).join('<br>')}</div></section>`;
  $('pptxConfigInstructions').innerHTML = [
    renderList('推荐：平台内置 Python 生成器', instructions.native_python),
    renderList('已有 Artifact Tool 时', instructions.artifact_tool),
    `<section><strong>平台实际写入的配置项</strong><div>${runtimeArray(instructions.environment).map((item) => `<code>${escapeHtml(item)}</code>`).join('<br>')}</div></section>`,
    '<section><strong>为什么需要确认</strong><div>生成 PPTX 会在本机创建文件。平台不会从任意网址下载或执行组件；只有你点击确认后，才会保存本机配置并立即启用。</div></section>',
  ].join('');
}

function renderPptxConfigStatus(config = {}) {
  state.pptxConfiguration = config;
  const status = $('pptxConfigStatus');
  if (!status) return;
  const configured = !!config.configured;
  status.className = `config-status ${configured ? 'ready' : 'pending'}`;
  status.innerHTML = configured
    ? `<strong>当前已可生成 PPTX</strong><br>方式：${escapeHtml(config.mode === 'python' ? '平台内置 Python' : 'Artifact Tool')}。${config.node_binary ? `Node.js：${escapeHtml(config.node_binary)}` : ''}`
    : `<strong>当前尚未配置 PPTX 生成器</strong><br>${escapeHtml(config.reason || '请选择一种生成方式并确认保存。')}`;
  if (config.node_binary) $('pptxNodeBinary').value = config.node_binary;
  if (config.entrypoint) $('pptxArtifactEntrypoint').value = config.entrypoint;
  const selectedMode = config.mode === 'artifact_tool' ? 'artifact' : 'python';
  $('pptxModePython').checked = selectedMode === 'python';
  $('pptxModeArtifact').checked = selectedMode === 'artifact';
  $('pptxArtifactFields').classList.toggle('hidden', selectedMode !== 'artifact');
  renderPptxConfigInstructions(config);
}

async function loadPptxConfiguration() {
  const config = await api('/api/presentation/configuration');
  renderPptxConfigStatus(config);
  return config;
}

async function openPptxConfigDialog() {
  $('pptxConfigDialog').classList.remove('hidden');
  await loadPptxConfiguration();
}

function closePptxConfigDialog() {
  $('pptxConfigDialog').classList.add('hidden');
}

async function savePptxConfiguration() {
  const mode = document.querySelector('input[name="pptxMode"]:checked')?.value || 'python';
  const confirmed = confirm(
    mode === 'python'
      ? '确认启用平台内置 Python PPTX 生成器？平台会写入本机 .env.local，并立即生效。'
      : '确认保存 Artifact Tool 路径？平台会验证入口并写入本机 .env.local。',
  );
  if (!confirmed) return;
  const button = $('savePptxConfigBtn');
  setBusy(button, true, '验证并保存中…');
  try {
    const result = await api('/api/presentation/configure', {
      method: 'POST',
      body: JSON.stringify({
        mode,
        confirmed: true,
        node_binary: $('pptxNodeBinary').value.trim() || 'node',
        entrypoint: $('pptxArtifactEntrypoint').value.trim(),
      }),
    });
    renderPptxConfigStatus(result.configuration || {});
    state.capabilities = await api('/api/capabilities');
    renderCapabilities();
    notify('PPTX 生成器配置成功，当前服务已立即生效');
  } catch (err) {
    const status = $('pptxConfigStatus');
    status.className = 'config-status error';
    status.textContent = err.message || 'PPTX 配置失败';
    notify(`PPTX 配置失败：${err.message || err}`, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function loadMarketplaceOnly() {
  state.marketplace = await api('/api/marketplace');
  renderMarketplace();
  return state.marketplace;
}

function marketplacePermissionLabel(key) {
  return {
    reads_uploaded_files: '读取附件正文',
    writes_artifacts: '生成文件',
    runs_local_process: '本地进程',
    uses_network: '联网访问',
  }[key] || key;
}

function renderMarketplacePlan(item, fallbackTools = []) {
  const plan = item.install_plan && typeof item.install_plan === 'object' ? item.install_plan : {};
  const permissions = plan.permissions && typeof plan.permissions === 'object' ? plan.permissions : {};
  const activePermissions = Object.entries(permissions).filter(([, value]) => !!value).map(([key]) => marketplacePermissionLabel(key));
  const willCreate = Array.isArray(plan.will_create) ? plan.will_create : [];
  const willEnable = Array.isArray(plan.will_enable) ? plan.will_enable : [];
  const requiredMcps = Array.isArray(plan.required_mcps) ? plan.required_mcps : [];
  const tools = Array.isArray(plan.tools) ? plan.tools : fallbackTools;
  const effects = Array.isArray(plan.tool_effects) ? plan.tool_effects : [];
  const changes = [
    willCreate.length ? `创建 ${willCreate.join('、')}` : '',
    willEnable.length ? `启用 ${willEnable.join('、')}` : '',
  ].filter(Boolean);
  return `
    <div class="marketplace-plan">
      <div class="marketplace-plan-row"><span>安装影响</span><strong>${escapeHtml(changes.join('；') || (item.installed ? '无需变更' : '登记能力'))}</strong></div>
      ${activePermissions.length ? `<div class="marketplace-tags">${activePermissions.map((label) => `<span>${escapeHtml(label)}</span>`).join('')}</div>` : '<div class="marketplace-tags muted"><span>不请求额外运行权限</span></div>'}
      ${requiredMcps.length ? `<div class="small">依赖 MCP：${escapeHtml(requiredMcps.join('、'))}</div>` : ''}
      ${tools.length ? `<div class="small">工具：${escapeHtml(tools.join('、'))}${effects.length ? ` · ${escapeHtml(effects.join('/'))}` : ''}</div>` : ''}
      ${plan.impact ? `<p>${escapeHtml(plan.impact)}</p>` : ''}
      ${plan.post_install ? `<p class="marketplace-post-install">${escapeHtml(plan.post_install)}</p>` : ''}
    </div>
  `;
}

function renderMarketplace() {
  const market = state.marketplace || { skills: [], mcp_servers: [] };
  const skills = Array.isArray(market.skills) ? market.skills : [];
  const mcps = Array.isArray(market.mcp_servers) ? market.mcp_servers : [];
  $('marketplaceSkillCount').textContent = String(skills.length);
  $('marketplaceMcpCount').textContent = String(mcps.length);
  $('marketplaceSkillList').innerHTML = skills.map((item) => `
    <article class="card marketplace-card ${item.installed ? 'active' : ''} ${state.marketplaceFocusId === item.id ? 'focus' : ''}" data-marketplace-skill="${escapeHtml(item.id)}">
      <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.installed ? (item.enabled ? '已安装' : '已停用') : '未安装'}</span></div>
      <div class="card-desc">${escapeHtml(item.description)}</div>
      <div class="small">${escapeHtml(item.source_label || '市场推荐')} · ${escapeHtml((item.keywords || []).join('、') || '无关键词')}</div>
      ${renderMarketplacePlan(item)}
      <div class="marketplace-actions">
        <button class="secondary" data-install-market-skill="${escapeHtml(item.id)}">${item.installed ? (item.enabled ? '已安装' : '启用') : '安装'}</button>
        <button class="text-button" data-open-skill="${escapeHtml(item.id)}">查看</button>
      </div>
    </article>
  `).join('') || '<div class="meta empty">暂无推荐 Skill。</div>';
  $('marketplaceMcpList').innerHTML = mcps.map((item) => `
    <article class="card marketplace-card ${item.installed ? 'active' : ''} ${state.marketplaceFocusId === item.id ? 'focus' : ''}" data-marketplace-mcp="${escapeHtml(item.id)}">
      <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${item.enabled ? 'completed' : ''}">${item.installed ? (item.enabled ? '已启用' : '已停用') : '未安装'}</span></div>
      <div class="card-desc">${escapeHtml(item.description)}</div>
      <div class="small">${escapeHtml(item.source_label || '平台内置 MCP')} · ${escapeHtml((item.tools || []).join('、') || '无工具')}</div>
      ${renderMarketplacePlan(item, item.tools || [])}
      <div class="marketplace-actions">
        <button class="secondary" data-enable-market-mcp="${escapeHtml(item.id)}">${item.installed ? (item.enabled ? '已启用' : '启用') : '启用'}</button>
        <button class="text-button" data-open-mcp="${escapeHtml(item.id)}">查看</button>
      </div>
    </article>
  `).join('') || '<div class="meta empty">暂无推荐 MCP。</div>';
  document.querySelectorAll('[data-install-market-skill]').forEach((btn) => {
    btn.onclick = () => installMarketplaceSkill(btn.dataset.installMarketSkill);
  });
  document.querySelectorAll('[data-enable-market-mcp]').forEach((btn) => {
    btn.onclick = () => enableMarketplaceMcp(btn.dataset.enableMarketMcp);
  });
  document.querySelectorAll('[data-open-skill]').forEach((btn) => {
    btn.onclick = async () => {
      switchTab('skills');
      const skillId = btn.dataset.openSkill;
      if (state.skills.some((item) => item.id === skillId)) {
        await selectSkill(skillId).catch((err) => notify(`技能读取失败：${err.message || err}`, 'error'));
      } else {
        notify('该 Skill 尚未安装，可直接点击安装。');
      }
    };
  });
  document.querySelectorAll('[data-open-mcp]').forEach((btn) => {
    btn.onclick = async () => {
      switchTab('mcp');
      const serverId = btn.dataset.openMcp;
      if (state.mcp.some((item) => item.id === serverId)) {
        await selectMcp(serverId).catch((err) => notify(`工具服务读取失败：${err.message || err}`, 'error'));
      }
    };
  });
  if (state.marketplaceFocusId) {
    const focusElement = document.querySelector(`[data-marketplace-skill="${CSS.escape(state.marketplaceFocusId)}"], [data-marketplace-mcp="${CSS.escape(state.marketplaceFocusId)}"]`);
    if (focusElement) focusElement.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    state.marketplaceFocusId = '';
  }
}

async function installMarketplaceSkill(skillId) {
  const button = document.querySelector(`[data-install-market-skill="${CSS.escape(skillId)}"]`);
  if (button) setBusy(button, true, '安装中…');
  try {
    const installed = await api(`/api/marketplace/skills/${encodeURIComponent(skillId)}/install`, { method: 'POST' });
    state.skills = await api('/api/skills');
    state.marketplace = await api('/api/marketplace');
    renderSkills();
    renderMarketplace();
    await selectSkill(installed.id);
    notify(`技能“${installed.name}”已准备好`);
  } catch (err) {
    notify(`Skill 安装失败：${err.message || err}`, 'error');
  } finally {
    if (button) setBusy(button, false);
  }
}

async function enableMarketplaceMcp(serverId) {
  const button = document.querySelector(`[data-enable-market-mcp="${CSS.escape(serverId)}"]`);
  if (button) setBusy(button, true, '启用中…');
  try {
    await api(`/api/marketplace/mcp/${encodeURIComponent(serverId)}/enable`, { method: 'POST' });
    state.mcp = await api('/api/mcp');
    state.marketplace = await api('/api/marketplace');
    renderMcp();
    renderMarketplace();
    await selectMcp(serverId);
    notify(`工具服务“${serverId}”已启用`);
  } catch (err) {
    notify(`MCP 启用失败：${err.message || err}`, 'error');
  } finally {
    if (button) setBusy(button, false);
  }
}
function modelReadinessLabel(model) {
  return model?.readiness?.label || (model?.enabled ? '已启用' : '已停用');
}

function modelReadinessClass(model) {
  const stateValue = model?.readiness?.state || (model?.enabled ? 'ready' : 'off');
  if (stateValue === 'ready') return 'ready';
  if (stateValue === 'off') return 'off';
  return 'pending';
}

function modelCredentialText(model) {
  if (model.provider === 'deterministic') return '无需密钥';
  if (model.has_api_key) return '直接 API Key 已加密保存';
  if (model.api_key_env) return `环境变量 ${model.api_key_env}`;
  return '未配置密钥';
}

function modelCapabilityTags(model) {
  const caps = model.capabilities || {};
  const tags = [];
  tags.push(caps.protocol === 'offline' ? '离线' : 'OpenAI Chat');
  if (caps.streaming) tags.push('流式');
  if (caps.tool_calling) tags.push('工具调用');
  if (caps.online_required) tags.push('需联网');
  const contextWindow = caps.context_window;
  if (contextWindow) tags.push(`上下文 ${contextWindow}`);
  return tags;
}

function modelLastTest(model) {
  const local = state.modelTestResults[model.id];
  if (local) {
    return {
      status: local.ok ? 'pass' : 'fail',
      message: local.message || '',
      tested_at: local.tested_at || '',
    };
  }
  return model.last_test || { status: 'untested', message: '尚未测试连接', tested_at: '' };
}

function modelTestBadge(model) {
  const result = modelLastTest(model);
  if (!result || result.status === 'untested') return '<span class="model-test-badge pending">未测试</span>';
  return `<span class="model-test-badge ${result.status === 'pass' ? 'ready' : 'failed'}">${result.status === 'pass' ? '测试通过' : '测试失败'}</span>`;
}

function modelTestSummary(model) {
  const result = modelLastTest(model);
  if (!result || result.status === 'untested') return '最近测试：未测试';
  const label = result.status === 'pass' ? '最近测试通过' : '最近测试失败';
  const time = result.tested_at ? ` · ${result.tested_at}` : '';
  return `${label}${time}${result.message ? ` · ${result.message}` : ''}`;
}

function renderModels() {
  $('modelCount').textContent = state.models.length;
  $('modelList').innerHTML = state.models.map((m) => {
    const tags = modelCapabilityTags(m);
    return `<div class="card model-card ${state.selectedModel?.id === m.id ? 'active' : ''}" data-model="${escapeHtml(m.id)}">
      <div class="card-title"><span>${escapeHtml(m.name)}</span><span class="status ${escapeHtml(modelReadinessClass(m))}">${escapeHtml(modelReadinessLabel(m))}</span></div>
      <div class="card-desc">${escapeHtml(m.provider)} · ${escapeHtml(m.model)}</div>
      <div class="model-meta-row"><span>${escapeHtml(m.id)}</span>${modelTestBadge(m)}</div>
      <div class="model-tags">${tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join('')}</div>
      <div class="small">${escapeHtml(modelCredentialText(m))}</div>
      <div class="model-test-summary">${escapeHtml(modelTestSummary(m))}</div>
      ${m.readiness?.detail ? `<div class="model-readiness-detail">${escapeHtml(m.readiness.detail)}</div>` : ''}
    </div>`;
  }).join('') || '<div class="meta empty">尚未配置模型</div>';
  $('agentModel').innerHTML = state.models.filter((m) => m.enabled).map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join('');
  document.querySelectorAll('[data-model]').forEach((el) => el.onclick = () => selectModel(el.dataset.model));
}

function toggleModelKeyMode() {
  const direct = $('modelKeyMode').value === 'direct';
  $('modelKeyEnvField').classList.toggle('hidden', direct);
  $('modelKeyDirectField').classList.toggle('hidden', !direct);
  $('modelKeyStatus').textContent = direct
    ? (state.selectedModel?.has_api_key ? '本机已保存加密密钥；输入新值可替换，留空保持不变。' : '密钥将在本机加密保存；生产部署建议接入专用密钥服务。')
    : '平台运行时从服务端环境变量读取密钥；保存的是变量名，不保存明文。';
}

function newModel() {
  state.selectedModel = null;
  renderModels();
  $('deleteModelBtn').classList.add('hidden');
  $('saveModelBtn').disabled = false;
  $('modelEditorTitle').textContent = '添加模型配置';
  $('modelId').disabled = false;
  $('modelId').value = 'openai-main';
  $('modelName').value = 'OpenAI 主模型';
  $('modelProvider').value = 'openai_compatible';
  $('modelNameValue').value = '';
  $('modelBaseUrl').value = 'https://api.openai.com/v1';
  $('modelKeyMode').value = 'env';
  $('modelApiKeyEnv').value = 'OPENAI_API_KEY';
  $('modelApiKey').value = '';
  $('modelConfig').value = '{"temperature":0.2,"timeout":90}';
  $('modelEnabled').checked = true;
  $('modelTestResult').textContent = '填写后保存，再测试连接。保存成功后会出现在左侧列表和工作台模型选择器。';
  toggleModelKeyMode();
}

function selectModel(id) {
  const m = state.models.find((x) => x.id === id);
  if (!m) return;
  state.selectedModel = m;
  renderModels();
  requestAnimationFrame(() => {
    const card = document.querySelector(`[data-model="${CSS.escape(id)}"]`);
    if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  });
  $('deleteModelBtn').classList.toggle('hidden', id === 'deterministic');
  $('modelId').disabled = true;
  $('modelId').value = m.id;
  $('modelName').value = m.name;
  $('modelProvider').value = m.provider;
  $('modelNameValue').value = m.model;
  $('modelBaseUrl').value = m.base_url || '';
  $('modelKeyMode').value = m.api_key_mode || (m.has_api_key ? 'direct' : 'env');
  $('modelApiKeyEnv').value = m.api_key_env || '';
  $('modelApiKey').value = '';
  $('modelConfig').value = formatJson(m.config || {});
  $('modelEnabled').checked = !!m.enabled;
  const lastTest = modelLastTest(m);
  if (id === 'deterministic') {
    $('modelEditorTitle').textContent = '内置离线模型（只读）';
    $('modelKeyStatus').textContent = '内置模型不需要密钥。';
    $('saveModelBtn').disabled = true;
    $('modelTestResult').textContent = '离线确定性模型可直接使用，无需连接测试。';
    return;
  }
  $('saveModelBtn').disabled = false;
  $('modelEditorTitle').textContent = m.name;
  $('modelTestResult').textContent = lastTest && lastTest.status !== 'untested'
    ? `${lastTest.status === 'pass' ? '最近测试通过' : '最近测试失败'}：${lastTest.message || ''}${lastTest.tested_at ? `\n时间：${lastTest.tested_at}` : ''}`
    : `${m.readiness?.label || '尚未测试'}：${m.readiness?.detail || '保存后可测试连接。'}`;
  toggleModelKeyMode();
  renderWorkbenchModelStatus();
}

async function deleteModel() {
  const model = state.selectedModel;
  if (!model || model.id === 'deterministic') return;
  if (!confirm(`确定删除模型“${model.name}”吗？`)) return;
  await api(`/api/models/${model.id}`, { method: 'DELETE' });
  state.models = await api('/api/models');
  delete state.modelTestResults[model.id];
  newModel(); renderModels(); renderTaskModelSelect(); notify(`模型“${model.name}”已删除`);
}
async function saveModel() {
  const button = $('saveModelBtn'); setBusy(button, true);
  try {
    const payload = { id: $('modelId').value.trim(), name: $('modelName').value.trim(), provider: $('modelProvider').value, model: $('modelNameValue').value.trim(), base_url: $('modelBaseUrl').value.trim(), api_key_mode: $('modelKeyMode').value, api_key_env: $('modelApiKeyEnv').value.trim(), config: JSON.parse($('modelConfig').value || '{}'), enabled: $('modelEnabled').checked };
    if (payload.api_key_mode === 'direct' && $('modelApiKey').value) payload.api_key = $('modelApiKey').value;
    if (!payload.id || !payload.name || !payload.model) throw new Error('请填写 ID、名称和模型名');
    if (state.selectedModel?.id === 'deterministic') throw new Error('内置离线模型不能修改，请点击“添加模型”');
    if (state.selectedModel) await api(`/api/models/${state.selectedModel.id}`, { method: 'PUT', body: JSON.stringify(payload) });
    else await api('/api/models', { method: 'POST', body: JSON.stringify(payload) });
    delete state.modelTestResults[payload.id];
    state.models = await api('/api/models'); selectModel(payload.id); renderModels(); renderTaskModelSelect();
    $('modelTestResult').textContent = `已保存：${payload.name}（${payload.model}）\n现在可点击“测试连接”，也可在工作台选择该模型。`;
    notify(`模型“${payload.name}”已保存，可在智能体配置中选择`);
  } catch (err) {
    $('modelTestResult').textContent = `保存失败：${err.message || err}`;
    notify(`模型保存失败：${err.message || err}`, 'error');
  } finally { setBusy(button, false); }
}
async function testModel() {
  const modelId = state.selectedModel?.id || $('modelId').value.trim();
  if (!modelId || (!state.models.some((m) => m.id === modelId))) { notify('请先保存并选择一个模型', 'error'); return; }
  const button = $('testModelBtn'); setBusy(button, true, '测试中…'); $('modelTestResult').textContent = '正在连接模型…';
  try {
    const result = await api(`/api/models/${modelId}/test`, { method: 'POST' });
    state.modelTestResults[modelId] = { ok: true, message: String(result.response || 'OK').slice(0, 300), tested_at: new Date().toISOString() };
    $('modelTestResult').textContent = `测试通过：${String(result.response || 'OK').slice(0, 300)}`;
    state.models = await api('/api/models');
    renderModels();
    renderTaskModelSelect();
    if (state.selectedModel?.id === modelId) selectModel(modelId);
    notify('模型连接测试成功');
  } catch (err) {
    state.modelTestResults[modelId] = { ok: false, message: String(err.message || err).slice(0, 300), tested_at: new Date().toISOString() };
    $('modelTestResult').textContent = String(err.message || err);
    renderModels();
    renderWorkbenchModelStatus();
    notify(`模型连接失败：${err.message || err}`, 'error');
  } finally { setBusy(button, false); }
}


function canManageExecutionEngines() {
  if (!state.authEnabled) return true;
  return state.currentUser?.role === 'admin';
}

function engineReadinessClass(engine) {
  const value = engine?.readiness?.state || (engine?.enabled ? 'ready' : 'off');
  if (value === 'ready') return 'ready';
  if (value === 'off') return 'off';
  return 'pending';
}

function engineReadinessLabel(engine) {
  return engine?.readiness?.label || (engine?.enabled ? '可用' : '已停用');
}

function engineCredentialText(engine) {
  if (engine?.has_api_key) return '已保存加密密钥';
  if (engine?.api_key_env) return `环境变量：${engine.api_key_env}`;
  return '尚未配置密钥';
}

async function loadExecutionEnginesOnly({ preserveSelection = false } = {}) {
  const previous = preserveSelection ? state.selectedExecutionEngine?.id : null;
  state.executionEngines = await api('/api/execution-engines');
  renderExecutionEngines();
  renderExecutionEngineSelect();
  if (previous && state.executionEngines.some((item) => item.id === previous)) {
    selectExecutionEngine(previous);
  }
  return state.executionEngines;
}

function syncExecutionEngineModelUi() {
  const engineSelect = $('executionEngineSelect');
  const taskModelSelect = $('taskModelSelect');
  const pill = $('modelStatusPill');
  if (!engineSelect || !taskModelSelect) return;
  const val = engineSelect.value;
  if (val === 'codex' || val === 'claude' || val === 'container') {
    taskModelSelect.disabled = true;
    taskModelSelect.title = `当前由 ${val.toUpperCase()} 独立容器沙箱执行，使用引擎自身独立配置的模型与环境。`;
    if (pill) {
      pill.textContent = val === 'codex' ? 'Codex (容器引擎)' : val === 'claude' ? 'Claude (容器引擎)' : '自定义容器引擎';
      pill.className = 'model-status-pill ready';
      pill.title = '当前由三方容器沙箱执行，模型在“执行引擎”标签页中配置';
    }
  } else {
    taskModelSelect.disabled = false;
    taskModelSelect.title = '选择内置智能体模型';
    renderWorkbenchModelStatus();
  }
}

function renderExecutionEngineSelect() {
  const select = $('executionEngineSelect');
  if (!select) return;
  const remembered = select.value || readPreference('execution-engine') || 'builtin';
  const engines = state.executionEngines || [];
  const options = [
    { id: 'builtin', name: '内置智能体引擎', enabled: true, ready: true },
    ...engines.map((engine) => ({
      id: engine.id,
      name: engine.name,
      enabled: !!engine.enabled,
      ready: (engine.readiness?.state || 'ready') === 'ready',
    })),
    { id: 'container', name: '自定义容器沙箱', enabled: true, ready: true },
  ];
  select.innerHTML = options.map((item) => {
    const disabled = !item.enabled;
    const label = disabled ? `${item.name} · 已停用` : item.name;
    return `<option value="${escapeHtml(item.id)}" ${disabled ? 'disabled' : ''}>${escapeHtml(label)}</option>`;
  }).join('');
  const available = options.filter((item) => item.enabled);
  const preferred = available.some((item) => item.id === remembered) ? remembered : 'builtin';
  select.value = preferred;
  syncExecutionEngineModelUi();
}

function renderExecutionEngines() {
  if (!$('engineList')) return;
  const engines = state.executionEngines || [];
  $('engineCount').textContent = String(engines.length);
  $('engineList').innerHTML = engines.map((engine) => `
    <div class="card model-card ${state.selectedExecutionEngine?.id === engine.id ? 'active' : ''}" data-engine="${escapeHtml(engine.id)}">
      <div class="card-title"><span>${escapeHtml(engine.name)}</span><span class="status ${escapeHtml(engineReadinessClass(engine))}">${escapeHtml(engineReadinessLabel(engine))}</span></div>
      <div class="card-desc">${escapeHtml(engine.description || engine.kind || '')}</div>
      <div class="small">${escapeHtml(engineCredentialText(engine))}</div>
      ${engine.readiness?.detail ? `<div class="model-readiness-detail">${escapeHtml(engine.readiness.detail)}</div>` : ''}
    </div>
  `).join('') || '<div class="meta empty">尚未接入执行引擎</div>';
  document.querySelectorAll('[data-engine]').forEach((el) => {
    el.onclick = () => selectExecutionEngine(el.dataset.engine);
  });
  const canManage = canManageExecutionEngines();
  ['engineBaseUrl', 'engineModel', 'engineKeyMode', 'engineApiKeyEnv', 'engineApiKey', 'engineEnabled', 'saveEngineBtn'].forEach((id) => {
    const node = $(id);
    if (node) node.disabled = !canManage || !state.selectedExecutionEngine;
  });
}

function toggleEngineKeyMode() {
  const direct = $('engineKeyMode')?.value === 'direct';
  $('engineKeyEnvField')?.classList.toggle('hidden', direct);
  $('engineKeyDirectField')?.classList.toggle('hidden', !direct);
  if (!$('engineKeyStatus')) return;
  if (!state.selectedExecutionEngine) {
    $('engineKeyStatus').textContent = '选择引擎后查看密钥状态';
    return;
  }
  $('engineKeyStatus').textContent = direct
    ? (state.selectedExecutionEngine.has_api_key ? '本机已保存加密密钥；输入新值可替换，留空保持不变。' : '密钥将在本机加密保存，页面不会回显。')
    : '平台运行时从服务进程的环境变量读取密钥；这里只保存变量名。';
}

function selectExecutionEngine(id) {
  const engine = (state.executionEngines || []).find((item) => item.id === id);
  if (!engine) return;
  state.selectedExecutionEngine = engine;
  renderExecutionEngines();
  $('engineEditorTitle').textContent = engine.name;
  $('engineEditorHint').textContent = engine.description || '配置该引擎使用的接口地址和密钥。';
  $('engineName').value = engine.name;
  $('engineBaseUrl').value = engine.base_url || '';
  $('engineModel').value = engine.config?.model || '';
  $('engineKeyMode').value = engine.api_key_mode || (engine.has_api_key ? 'direct' : 'env');
  $('engineApiKeyEnv').value = engine.api_key_env || engine.default_api_key_env || '';
  $('engineApiKey').value = '';
  $('engineEnabled').checked = !!engine.enabled;
  $('engineSaveResult').textContent = engine.readiness?.detail || '保存后，新的任务会使用这里的配置。';
  const canManage = canManageExecutionEngines();
  ['engineBaseUrl', 'engineModel', 'engineKeyMode', 'engineApiKeyEnv', 'engineApiKey', 'engineEnabled', 'saveEngineBtn'].forEach((fieldId) => {
    const node = $(fieldId);
    if (node) node.disabled = !canManage;
  });
  toggleEngineKeyMode();
}

async function saveExecutionEngine() {
  const engine = state.selectedExecutionEngine;
  if (!engine) { notify('请先选择一个执行引擎', 'error'); return; }
  if (!canManageExecutionEngines()) { notify('只有管理员可以修改执行引擎', 'error'); return; }
  const button = $('saveEngineBtn'); setBusy(button, true);
  try {
    const payload = {
      enabled: $('engineEnabled').checked,
      base_url: $('engineBaseUrl').value.trim(),
      api_key_mode: $('engineKeyMode').value,
      api_key_env: $('engineApiKeyEnv').value.trim(),
      model: $('engineModel').value.trim(),
    };
    if (payload.api_key_mode === 'direct' && $('engineApiKey').value) payload.api_key = $('engineApiKey').value;
    const saved = await api(`/api/execution-engines/${engine.id}`, { method: 'PUT', body: JSON.stringify(payload) });
    state.executionEngines = await api('/api/execution-engines');
    selectExecutionEngine(saved.id);
    renderExecutionEngineSelect();
    $('engineApiKey').value = '';
    $('engineSaveResult').textContent = `已保存“${saved.name}”。新任务会使用当前配置；已在运行的任务不会改动。`;
    notify(`已保存“${saved.name}”的配置`);
  } catch (err) {
    $('engineSaveResult').textContent = `保存失败：${err.message || err}`;
    notify(`执行引擎保存失败：${err.message || err}`, 'error');
  } finally { setBusy(button, false); }
}



async function openCodexSessionsDialog() {
  const dialog = $('codexSessionsDialog');
  if (!dialog) return;
  dialog.classList.remove('hidden');
  $('codexSessionsListContainer').classList.remove('hidden');
  $('codexSessionDetailContainer').classList.add('hidden');
  const list = $('codexSessionsList');
  list.innerHTML = '<div class="meta empty">正在加载当前项目的 Codex 历史会话…</div>';
  try {
    const wsId = currentWorkspaceId();
    const sessions = await api(`/api/workspaces/${encodeURIComponent(wsId)}/codex-sessions`);
    if (!sessions || sessions.length === 0) {
      list.innerHTML = '<div class="meta empty">当前项目下暂无 Codex 会话记录。在执行引擎中选用 Codex 发送任务后，会话记录将自动保存并出现在这里。</div>';
      return;
    }
    list.innerHTML = sessions.map((s) => `
      <div class="card model-card" data-codex-session="${escapeHtml(s.session_id)}" style="cursor: pointer; padding: 12px 14px; margin-bottom: 8px;">
        <div class="card-title">
          <span><strong>会话 ID: ${escapeHtml(s.session_id.slice(0, 12))}…</strong></span>
          <span class="status pass">${escapeHtml(s.turn_count || 1)} 轮交互</span>
        </div>
        <div class="card-desc" style="font-size: 13px; margin: 4px 0; color: var(--text-color);">
          ${escapeHtml(s.first_prompt || s.last_reply || '执行会话记录')}
        </div>
        <div class="small" style="color: var(--muted); display: flex; justify-content: space-between;">
          <span>模型: ${escapeHtml(s.model || 'Codex 引擎')}</span>
          <span>${formatIsoDate(s.updated_at)}</span>
        </div>
      </div>
    `).join('');
    list.querySelectorAll('[data-codex-session]').forEach((el) => {
      el.onclick = () => openCodexSessionDetail(el.dataset.codexSession);
    });
  } catch (err) {
    list.innerHTML = `<div class="meta empty">加载会话失败：${escapeHtml(err.message || err)}</div>`;
  }
}

async function openCodexSessionDetail(sessionId) {
  $('codexSessionsListContainer').classList.add('hidden');
  $('codexSessionDetailContainer').classList.remove('hidden');
  const messagesBox = $('codexSessionDetailMessages');
  const metaBox = $('codexSessionDetailMeta');
  metaBox.textContent = `会话 ID: ${sessionId} (加载中…)`;
  messagesBox.innerHTML = '<div class="meta empty">正在读取会话历史…</div>';
  try {
    const wsId = currentWorkspaceId();
    const detail = await api(`/api/workspaces/${encodeURIComponent(wsId)}/codex-sessions/${encodeURIComponent(sessionId)}`);
    metaBox.innerHTML = `<strong>会话 ID:</strong> <code>${escapeHtml(sessionId)}</code> &nbsp;·&nbsp; <strong>模型:</strong> ${escapeHtml(detail.meta?.model || 'gpt-5.2')} &nbsp;·&nbsp; 共 ${detail.messages?.length || 0} 条对话记录`;
    const msgs = detail.messages || [];
    if (msgs.length === 0) {
      messagesBox.innerHTML = '<div class="meta empty">该会话无可见对话记录。</div>';
      return;
    }
    messagesBox.innerHTML = msgs.map((m) => {
      const isUser = m.role === 'user';
      return `
        <div class="message ${isUser ? 'user' : 'assistant'}" style="margin: 4px 0; max-width: 90%; align-self: ${isUser ? 'flex-end' : 'flex-start'};">
          <div class="message-bubble" style="padding: 10px 14px; border-radius: 8px; background: ${isUser ? 'var(--accent-subtle)' : 'var(--card-bg)'}; border: 1px solid var(--border-color);">
            <div style="font-size: 11px; color: var(--muted); margin-bottom: 4px;"><strong>${isUser ? '用户指令' : 'Codex 答复'}</strong> ${m.timestamp ? `· ${formatIsoDate(m.timestamp)}` : ''}</div>
            <div style="white-space: pre-wrap; word-break: break-word;">${escapeHtml(m.content)}</div>
          </div>
        </div>
      `;
    }).join('');
  } catch (err) {
    messagesBox.innerHTML = `<div class="meta empty">读取失败：${escapeHtml(err.message || err)}</div>`;
  }
}

async function discoverModelsForModelConfig() {
  const baseUrl = $('modelBaseUrl')?.value.trim();
  if (!baseUrl) {
    notify('请先填写 Base URL', 'error');
    $('modelBaseUrl')?.focus();
    return;
  }
  const button = $('discoverModelsBtn');
  setBusy(button, true, '正在获取…');
  const hint = $('discoveredModelsHint');
  if (hint) hint.textContent = '正在向上游接口查询模型列表…';
  try {
    const payload = {
      base_url: baseUrl,
      api_key_mode: $('modelKeyMode')?.value || 'env',
      api_key_env: $('modelApiKeyEnv')?.value.trim() || '',
    };
    if (payload.api_key_mode === 'direct' && $('modelApiKey')?.value) {
      payload.api_key = $('modelApiKey').value;
    }
    if (state.selectedModel?.id) {
      payload.model_id = state.selectedModel.id;
    }
    const result = await api('/api/models/discover', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    const models = result.models || [];
    const datalist = $('discoveredModelsList');
    if (datalist) {
      datalist.innerHTML = models.map((m) => `<option value="${escapeHtml(m)}"></option>`).join('');
    }
    const picker = $('modelPickerSelect');
    if (picker) {
      picker.innerHTML = `<option value="">-- 点击直接选择已获取的模型 (${models.length} 个) --</option>` +
        models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
      picker.classList.remove('hidden');
      picker.onchange = () => {
        if (picker.value) {
          $('modelNameValue').value = picker.value;
          if (!$('modelId').value || $('modelId').value === 'openai-main' || $('modelId').value.startsWith('model-')) {
            $('modelId').value = picker.value.toLowerCase().replace(/[^a-z0-9_\-\.]/g, '-');
            $('modelName').value = picker.value;
          }
        }
      };
    }
    const addBtn = $('addDiscoveredModelBtn');
    if (addBtn) {
      addBtn.classList.remove('hidden');
      addBtn.onclick = async () => {
        const chosenModel = $('modelNameValue').value.trim() || picker?.value;
        if (!chosenModel) {
          notify('请先在下拉框中选择或输入一个模型', 'error');
          return;
        }
        const cleanId = chosenModel.toLowerCase().replace(/[^a-z0-9_\-\.]/g, '-');
        try {
          setBusy(addBtn, true, '正在添加…');
          await api('/api/models', {
            method: 'POST',
            body: JSON.stringify({
              id: cleanId,
              name: chosenModel,
              model: chosenModel,
              base_url: $('modelBaseUrl').value.trim(),
              provider: $('modelProvider').value || 'openai_compatible',
              api_key_mode: $('modelKeyMode').value,
              api_key: $('modelApiKey')?.value || '',
              api_key_env: $('modelApiKeyEnv')?.value.trim() || '',
              copy_credentials_from: state.selectedModel?.id || 'gpt-5.5',
              enabled: true,
            }),
          });
          notify(`已成功添加“${chosenModel}”为可用模型`);
          state.models = await api('/api/models');
          renderModels();
          renderTaskModelSelect();
          selectModel(cleanId);
        } catch (err) {
          notify(`添加模型失败：${err.message || err}`, 'error');
        } finally {
          setBusy(addBtn, false);
        }
      };
    }
    if (hint) hint.textContent = `已获取 ${models.length} 个可用模型，可通过下拉菜单直接点击选择。`;
    if (!$('modelNameValue').value && models.length > 0) {
      $('modelNameValue').value = models.find((m) => m.includes('gpt-5') || m.includes('gpt-4')) || models[0];
    }
    notify(`成功获取 ${models.length} 个可用模型`);
  } catch (err) {
    if (hint) hint.textContent = `获取模型失败：${err.message || err}`;
    notify(`获取可用模型失败：${err.message || err}`, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function discoverModelsForExecutionEngine() {
  const baseUrl = $('engineBaseUrl')?.value.trim();
  if (!baseUrl) {
    notify('请先填写接口地址 Base URL', 'error');
    $('engineBaseUrl')?.focus();
    return;
  }
  const button = $('discoverEngineModelsBtn');
  setBusy(button, true, '正在获取…');
  const hint = $('discoveredEngineModelsHint');
  if (hint) hint.textContent = '正在向上游接口查询模型列表…';
  try {
    const payload = {
      base_url: baseUrl,
      api_key_mode: $('engineKeyMode')?.value || 'env',
      api_key_env: $('engineApiKeyEnv')?.value.trim() || '',
    };
    if (payload.api_key_mode === 'direct' && $('engineApiKey')?.value) {
      payload.api_key = $('engineApiKey').value;
    }
    if (state.selectedExecutionEngine?.id) {
      payload.engine_id = state.selectedExecutionEngine.id;
    }
    const result = await api('/api/models/discover', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    const models = result.models || [];
    const datalist = $('discoveredEngineModelsList');
    if (datalist) {
      datalist.innerHTML = models.map((m) => `<option value="${escapeHtml(m)}"></option>`).join('');
    }
    const picker = $('engineModelPickerSelect');
    if (picker) {
      picker.innerHTML = `<option value="">-- 点击直接选择已获取的模型 (${models.length} 个) --</option>` +
        models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
      picker.classList.remove('hidden');
      picker.onchange = () => {
        if (picker.value) {
          $('engineModel').value = picker.value;
        }
      };
    }
    if (hint) hint.textContent = `已获取 ${models.length} 个可用模型，可通过下拉菜单直接选择。`;
    if (!$('engineModel').value && models.length > 0) {
      $('engineModel').value = models.find((m) => m.includes('gpt-5') || m.includes('gpt-4')) || models[0];
    }
    notify(`成功获取 ${models.length} 个可用模型`);
  } catch (err) {
    if (hint) hint.textContent = `获取模型失败：${err.message || err}`;
    notify(`获取可用模型失败：${err.message || err}`, 'error');
  } finally {
    setBusy(button, false);
  }
}

async function testExecutionEngine() {
  const engine = state.selectedExecutionEngine;
  if (!engine) { notify('请先选择一个执行引擎', 'error'); return; }
  const button = $('testEngineBtn');
  setBusy(button, true, '正在测试…');
  $('engineSaveResult').textContent = `正在为 ${engine.name} 启动容器并测试连接…`;
  try {
    const result = await api(`/api/execution-engines/${engine.id}/test`, { method: 'POST' });
    const reply = result.response || 'OK';
    const duration = result.duration ? `（耗时 ${result.duration} 秒）` : '';
    $('engineSaveResult').textContent = `测试通过：${reply} ${duration}
容器沙箱、网络与模型认证均正常。`;
    state.executionEngines = await api('/api/execution-engines');
    renderExecutionEngines();
    renderExecutionEngineSelect();
    notify(`执行引擎“${engine.name}”连接测试通过`);
  } catch (err) {
    $('engineSaveResult').textContent = `测试失败：${err.message || err}`;
    notify(`执行引擎测试失败：${err.message || err}`, 'error');
  } finally {
    setBusy(button, false);
  }
}

function uploadContextStateLabel(upload) {
  const context = upload?.context_status || {};
  if (context.label) return context.label;
  if (context.extractable) return '可进入上下文';
  return '已上传';
}

function uploadContextStateClass(upload) {
  const stateValue = upload?.context_status?.state || 'unknown';
  if (stateValue === 'ready') return 'ready';
  if (stateValue === 'too_large') return 'warning';
  if (stateValue === 'unsupported') return 'unsupported';
  return 'pending';
}

function uploadSizeLabel(size) {
  const value = Number(size || 0);
  if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)}MB`;
  return `${Math.max(1, Math.ceil(value / 1024))}KB`;
}

async function uploadFiles(files) {
  for (const file of files) {
    try {
      const form = new FormData();
      form.append('file', file);
      const uploaded = await api('/api/uploads', { method: 'POST', body: form });
      state.uploads.push(uploaded);
    } catch (err) {
      notify(`附件“${file.name}”上传失败：${err.message || err}`, 'error');
    }
  }
  renderUploads();
}

function removeUpload(uploadId) {
  state.uploads = state.uploads.filter((item) => String(item.id) !== String(uploadId));
  renderUploads();
}

function renderUploads() {
  const list = $('uploadList');
  if (!list) return;
  list.innerHTML = state.uploads.map((f) => {
    const context = f.context_status || {};
    return `<div class="upload-chip ${escapeHtml(uploadContextStateClass(f))}" title="${escapeHtml(context.detail || '')}">
      <div class="upload-chip-main">
        <span>${escapeHtml(f.name)} · ${escapeHtml(uploadSizeLabel(f.size))}</span>
        <button class="upload-remove" type="button" data-remove-upload="${escapeHtml(f.id || '')}" aria-label="移除附件 ${escapeHtml(f.name || '')}">×</button>
      </div>
      <small>${escapeHtml(uploadContextStateLabel(f))}</small>
    </div>`;
  }).join('');
  list.querySelectorAll('[data-remove-upload]').forEach((button) => {
    button.onclick = () => removeUpload(button.dataset.removeUpload);
  });
}
async function installSkillPackage(file) { try { const form = new FormData(); form.append('file', file); const installed = await api('/api/skills/install/upload', { method: 'POST', body: form }); state.skills = await api('/api/skills'); await selectSkill(installed.id); notify(`技能“${installed.name}”安装成功`); } catch (err) { notify(`技能安装失败：${err.message || err}`, 'error'); } }
async function importMcpPackage(file) { try { const form = new FormData(); form.append('file', file); const imported = await api('/api/mcp/import', { method: 'POST', body: form }); state.mcp = await api('/api/mcp'); renderMcp(); if (imported[0]) await selectMcp(imported[0].id); notify(`已导入 ${imported.length} 个工具服务`); } catch (err) { notify(`工具配置导入失败：${err.message || err}`, 'error'); } }
async function installSkillFromUrl() { const url = $('skillDownloadUrl').value.trim(); if (!url) return notify('请粘贴 Skill 下载直链', 'error'); try { const installed = await api('/api/skills/install/url', { method:'POST', body:JSON.stringify({url}) }); state.skills = await api('/api/skills'); await selectSkill(installed.id); notify(`技能“${installed.name}”安装成功`); } catch (err) { notify(`Skill 链接安装失败：${err.message || err}`, 'error'); } }
async function installMcpFromUrl() { const url = $('mcpDownloadUrl').value.trim(); if (!url) return notify('请粘贴 MCP JSON 下载直链', 'error'); try { const imported = await api('/api/mcp/install/url', { method:'POST', body:JSON.stringify({url}) }); state.mcp = await api('/api/mcp'); if (imported[0]) await selectMcp(imported[0].id); notify(`已安装 ${imported.length} 个工具服务`); } catch (err) { notify(`MCP 链接安装失败：${err.message || err}`, 'error'); } }

async function loadTasksOnly() {
  state.tasks = await api(`/api/tasks?${new URLSearchParams(platformScopeValues()).toString()}`);
  renderTasks();
}

async function loadLoopsOnly() {
  const selectedId = state.selectedLoop?.id;
  state.loops = await api(`/api/loops?${new URLSearchParams(platformScopeValues()).toString()}`);
  renderLoops();
  if (selectedId && state.loops.some((item) => item.id === selectedId)) await selectLoop(selectedId);
  else if (selectedId) newLoop();
}

function loopStatusLabel(status) {
  return ({
    paused: '已暂停', active: '等待触发', queued: '已排队', accepted: '已接收', running: '执行中',
    waiting_approval: '等待审批', blocked: '等待补充', completed: '已完成', failed: '失败', read: '已读', unread: '未读',
  })[status] || status;
}

function loopTriggerLabel(item) {
  const trigger = typeof item === 'string' ? item : item?.trigger_type;
  if (trigger === 'cron') return `Cron · ${item?.cron_expression || '未填写'}`;
  if (trigger === 'once') return `一次 · ${displayAutomationTime(item?.once_at)}`;
  if (trigger === 'webhook') return 'Webhook 事件';
  return `每 ${Number(item?.interval_seconds || 3600)} 秒`;
}

function displayAutomationTime(value) {
  if (!value) return '—';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString('zh-CN', { hour12: false });
}

function toDateTimeLocal(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

function automationDiffSummary(diff) {
  const changes = Array.isArray(diff?.changes) ? diff.changes : [];
  if (!diff?.changed || !changes.length) return '无变化';
  return `${changes.length}${diff.truncated ? '+' : ''} 项变化`;
}

function compactAutomationValue(value) {
  const raw = typeof value === 'string' ? value : JSON.stringify(value);
  if (raw == null) return 'null';
  return raw.length > 180 ? `${raw.slice(0, 177)}…` : raw;
}

function renderLoops() {
  const agent = $('loopAgent');
  const model = $('loopModel');
  const agentValue = agent?.value || state.selectedLoop?.agent_id || 'general-agent';
  const modelValue = model?.value || state.selectedLoop?.model_id || 'deterministic';
  if (agent) {
    agent.innerHTML = state.agents.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
    if (state.agents.some((item) => item.id === agentValue)) agent.value = agentValue;
  }
  if (model) {
    model.innerHTML = state.models.filter((item) => item.enabled).map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join('');
    if (state.models.some((item) => item.id === modelValue && item.enabled)) model.value = modelValue;
  }
  $('loopList').innerHTML = state.loops.map((item) => `
    <div class="card loop-card ${state.selectedLoop?.id === item.id ? 'active' : ''}" data-loop="${escapeHtml(item.id)}">
      <div class="card-title"><span>${escapeHtml(item.name)}</span><span class="status ${escapeHtml(item.status)}">${escapeHtml(loopStatusLabel(item.status))}</span></div>
      <div class="card-desc">${escapeHtml(item.prompt)}</div>
      <div class="loop-card-stats"><span>${item.run_count}/${item.max_runs} 轮</span><span>${escapeHtml(loopTriggerLabel(item))}</span><span>${item.consecutive_failures}/${item.max_failures} 失败</span></div>
      <div class="small">${item.next_run_at ? `下次：${escapeHtml(displayAutomationTime(item.next_run_at))}` : item.trigger_type === 'webhook' && item.status === 'active' ? '正在等待 Webhook 事件' : '当前未安排下一次触发'}</div>
    </div>
  `).join('') || '<div class="meta empty">还没有自动化。创建一个持续目标开始使用。</div>';
  document.querySelectorAll('[data-loop]').forEach((el) => el.onclick = () => selectLoop(el.dataset.loop));
}

function scrollLoopCardIntoView(id) {
  if (!id) return;
  const card = document.querySelector(`[data-loop="${CSS.escape(id)}"]`);
  if (card) card.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

function renderLoopOverview(item = null) {
  $('loopOverviewStatus').textContent = item ? loopStatusLabel(item.status) : '尚未保存';
  $('loopOverviewTrigger').textContent = item ? loopTriggerLabel(item) : '未配置';
  $('loopOverviewNext').textContent = item?.next_run_at
    ? displayAutomationTime(item.next_run_at)
    : item?.trigger_type === 'webhook' && item?.status === 'active' ? '等待事件' : '—';
  $('loopOverviewDiff').textContent = item ? automationDiffSummary(item.last_diff) : '—';
}

function syncLoopTriggerFields() {
  const trigger = $('loopTriggerType').value;
  $('loopIntervalField').classList.toggle('hidden', trigger !== 'interval');
  $('loopCronField').classList.toggle('hidden', trigger !== 'cron');
  $('loopOnceField').classList.toggle('hidden', trigger !== 'once');
  $('loopWebhookFields').classList.toggle('hidden', trigger !== 'webhook');
  $('loopScheduleHint').textContent = ({
    interval: '按固定秒数重复触发', cron: '按 5 段 Cron 表达式触发', once: '在指定时间执行一次', webhook: '由签名 HTTP 请求触发',
  })[trigger];
  $('startLoopBtn').textContent = trigger === 'webhook' ? '启用 Webhook' : trigger === 'once' ? '启动一次性调度' : '启动调度';
  const id = state.selectedLoop?.id || $('loopId').value.trim() || '{automation_id}';
  const path = `/api/loops/${encodeURIComponent(id)}/webhook`;
  $('loopWebhookUrl').textContent = location.protocol === 'http:' || location.protocol === 'https:' ? `${location.origin}${path}` : path;
  const configured = Boolean(state.selectedLoop?.webhook_secret_configured);
  $('loopWebhookSecretStatus').textContent = configured
    ? '签名密钥已安全保存；留空不会覆盖，填写新值可轮换密钥。'
    : '尚未配置签名密钥；Webhook 触发必须填写至少 16 位密钥。';
}

function syncLoopActionButtons(item = state.selectedLoop) {
  if (!item) {
    ['runLoopBtn', 'startLoopBtn', 'pauseLoopBtn'].forEach((id) => { $(id).disabled = true; });
    return;
  }
  $('runLoopBtn').disabled = item.status === 'running' || item.run_count >= item.max_runs;
  $('startLoopBtn').disabled = ['running', 'active'].includes(item.status) || item.run_count >= item.max_runs || (item.trigger_type === 'once' && item.run_count > 0);
  $('pauseLoopBtn').disabled = !['running', 'active'].includes(item.status);
}

function newLoop() {
  clearTimeout(state.loopPollTimer);
  state.loopPollTimer = null;
  state.selectedLoop = null;
  state.loopTriggerEvents = [];
  state.loopNotifications = [];
  state.loopEditorDirty = false;
  state.loopStateDirty = false;
  renderLoops();
  $('loopEditorTitle').textContent = '新建自动化';
  $('loopStatusText').textContent = '配置触发条件后可试运行或启动调度';
  $('loopId').disabled = false; $('loopId').value = '';
  $('loopName').value = ''; $('loopPrompt').value = '';
  $('loopTriggerType').value = 'interval'; $('loopInterval').value = '3600'; $('loopCronExpression').value = ''; $('loopOnceAt').value = '';
  $('loopWebhookSecret').value = ''; $('loopWebhookTolerance').value = '300';
  $('loopMaxRuns').value = '10'; $('loopMaxFailures').value = '3'; $('loopMaxAttempts').value = '1'; $('loopRetryBackoff').value = '0';
  $('loopState').disabled = false; $('loopState').value = '{}'; $('loopStateTitle').textContent = '初始状态'; $('loopStateHint').textContent = '首轮运行可读取的结构化状态，必须填写 JSON 对象';
  $('deleteLoopBtn').classList.add('hidden');
  syncLoopActionButtons(null);
  renderLoopOverview();
  syncLoopTriggerFields();
  $('loopRunSummary').textContent = '';
  $('loopRuns').innerHTML = '<div class="meta empty">保存并选择自动化后查看历史</div>';
  $('loopTriggerEventSummary').textContent = '0 条';
  $('loopTriggerEvents').innerHTML = '<div class="meta empty">保存并选择自动化后查看触发记录</div>';
  $('loopNotificationSummary').textContent = '0 条';
  $('loopNotifications').innerHTML = '<div class="meta empty">暂无通知</div>';
}

async function selectLoop(id) {
  clearTimeout(state.loopPollTimer);
  state.loopPollTimer = null;
  const item = await api(`/api/loops/${id}`);
  const [triggerEvents, notifications] = await Promise.all([
    api(`/api/loops/${id}/trigger-events`).catch(() => []),
    api('/api/notifications?limit=200').catch(() => []),
  ]);
  state.selectedLoop = item;
  state.loopTriggerEvents = triggerEvents;
  state.loopNotifications = notifications.filter((notice) => notice.entity_type === 'loop' && notice.entity_id === id);
  renderLoops();
  $('loopEditorTitle').textContent = item.name;
  $('loopStatusText').textContent = `${loopStatusLabel(item.status)} · 已运行 ${item.run_count}/${item.max_runs} 轮${item.next_run_at ? ` · 下次 ${displayAutomationTime(item.next_run_at)}` : ''}`;
  $('loopId').disabled = true; $('loopId').value = item.id;
  $('loopName').value = item.name; $('loopPrompt').value = item.prompt;
  $('loopAgent').value = item.agent_id; $('loopModel').value = item.model_id;
  $('loopTriggerType').value = item.trigger_type || 'interval'; $('loopInterval').value = item.interval_seconds;
  $('loopCronExpression').value = item.cron_expression || ''; $('loopOnceAt').value = toDateTimeLocal(item.once_at);
  $('loopWebhookSecret').value = ''; $('loopWebhookTolerance').value = item.webhook_tolerance_seconds || 300;
  $('loopMaxRuns').value = item.max_runs; $('loopMaxFailures').value = item.max_failures;
  $('loopMaxAttempts').value = item.max_attempts || 1; $('loopRetryBackoff').value = item.retry_backoff_seconds || 0;
  $('loopState').value = formatJson(item.state || {}); $('loopState').disabled = item.status === 'running';
  $('loopStateTitle').textContent = '当前状态';
  $('loopStateHint').textContent = item.status === 'running' ? '本轮执行中，为避免覆盖运行结果，当前状态暂不可编辑' : '下一轮会读取此状态；保存后会替换当前结构化状态';
  $('deleteLoopBtn').classList.remove('hidden');
  syncLoopActionButtons(item);
  renderLoopOverview(item);
  syncLoopTriggerFields();
  renderLoopRuns(item.runs || []);
  renderLoopTriggerEvents();
  renderLoopNotifications();
  state.loopEditorDirty = false;
  state.loopStateDirty = false;
  scrollLoopCardIntoView(id);
  scheduleLoopRefresh(id, item.status);
}

function scheduleLoopRefresh(id, status) {
  if (!document.querySelector('#tab-loops.active') || !['active', 'running'].includes(status)) return;
  state.loopPollTimer = setTimeout(() => {
    if (state.selectedLoop?.id !== id || !document.querySelector('#tab-loops.active')) return;
    const refresh = state.loopEditorDirty ? refreshSelectedLoopRuntime : selectLoop;
    refresh(id).catch((err) => notify(`自动化状态刷新失败：${err.message || err}`, 'error'));
  }, status === 'running' ? 1200 : 5000);
}

async function refreshSelectedLoopRuntime(id) {
  const [item, triggerEvents, notifications] = await Promise.all([
    api(`/api/loops/${id}`),
    api(`/api/loops/${id}/trigger-events`).catch(() => []),
    api('/api/notifications?limit=200').catch(() => []),
  ]);
  if (state.selectedLoop?.id !== id) return;
  state.selectedLoop = item;
  state.loopTriggerEvents = triggerEvents;
  state.loopNotifications = notifications.filter((notice) => notice.entity_type === 'loop' && notice.entity_id === id);
  const { runs: _runs, ...loopSnapshot } = item;
  state.loops = state.loops.map((loop) => loop.id === id ? { ...loop, ...loopSnapshot } : loop);
  renderLoops();
  $('loopStatusText').textContent = `${loopStatusLabel(item.status)} · 已运行 ${item.run_count}/${item.max_runs} 轮${item.next_run_at ? ` · 下次 ${displayAutomationTime(item.next_run_at)}` : ''}`;
  $('loopState').disabled = item.status === 'running';
  $('loopStateHint').textContent = item.status === 'running' ? '本轮执行中，为避免覆盖运行结果，当前状态暂不可编辑' : '表单有尚未保存的修改；运行状态已刷新，但不会覆盖你的输入';
  syncLoopActionButtons(item);
  renderLoopOverview(item);
  renderLoopRuns(item.runs || []);
  renderLoopTriggerEvents();
  renderLoopNotifications();
  scheduleLoopRefresh(id, item.status);
}

function renderStateDiff(diff) {
  const changes = Array.isArray(diff?.changes) ? diff.changes : [];
  if (!diff?.changed || !changes.length) return '<div class="automation-no-change">本次运行未改变结构化状态</div>';
  return `<div class="automation-diff-list">${changes.map((change) => `
    <div class="automation-diff-row">
      <code>${escapeHtml(change.path || '$')}</code>
      <span title="${escapeHtml(compactAutomationValue(change.before))}">${escapeHtml(compactAutomationValue(change.before))}</span>
      <i aria-hidden="true">→</i>
      <strong title="${escapeHtml(compactAutomationValue(change.after))}">${escapeHtml(compactAutomationValue(change.after))}</strong>
    </div>
  `).join('')}</div>${diff.truncated ? '<div class="small">变化项较多，仅展示后端返回的前 200 项。</div>' : ''}`;
}

function renderLoopRuns(runs) {
  const uniqueRounds = new Set(runs.map((run) => run.run_number)).size;
  $('loopRunSummary').textContent = `${uniqueRounds} 轮 · ${runs.length} 次尝试`;
  $('loopRuns').innerHTML = runs.map((run, index) => `
    <details class="loop-run-detail ${escapeHtml(run.status)}" ${index === 0 ? 'open' : ''}>
      <summary>
        <span class="loop-run-number">${run.run_number}</span>
        <span class="loop-run-copy"><strong>第 ${run.run_number} 轮 · 尝试 ${run.attempt} · ${escapeHtml(loopStatusLabel(run.status))}</strong><small>${escapeHtml(run.decision?.reason || run.error?.message || '运行记录')} · ${escapeHtml(displayAutomationTime(run.finished_at || run.started_at))}</small></span>
        <span class="automation-diff-badge ${run.diff?.changed ? 'changed' : ''}">${escapeHtml(automationDiffSummary(run.diff))}</span>
      </summary>
      <div class="loop-run-body">
        <div class="loop-run-meta"><span>开始：${escapeHtml(displayAutomationTime(run.started_at))}</span><span>结束：${escapeHtml(displayAutomationTime(run.finished_at))}</span>${run.trigger_event_id ? `<span>触发事件：${escapeHtml(run.trigger_event_id)}</span>` : ''}</div>
        <div class="automation-state-diff"><h4>本次状态差异</h4>${renderStateDiff(run.diff)}</div>
        ${run.error?.message ? `<div class="automation-run-error">${escapeHtml(run.error.message)}</div>` : ''}
        ${run.task_id ? `<button class="text-button loop-open-task" data-loop-task="${escapeHtml(run.task_id)}">查看本轮任务与产物 →</button>` : ''}
      </div>
    </details>
  `).join('') || '<div class="meta empty">尚未执行。可立即运行一轮验证配置。</div>';
  document.querySelectorAll('[data-loop-task]').forEach((el) => {
    el.onclick = (event) => { event.stopPropagation(); if (el.dataset.loopTask) openTask(el.dataset.loopTask); };
  });
}

function renderLoopTriggerEvents() {
  const events = state.loopTriggerEvents || [];
  $('loopTriggerEventSummary').textContent = `${events.length} 条`;
  $('loopTriggerEvents').innerHTML = events.map((event) => `
    <article class="automation-event-item">
      <div><span class="status ${escapeHtml(event.status)}">${escapeHtml(loopStatusLabel(event.status))}</span><strong>${escapeHtml(event.trigger_type === 'webhook' ? 'Webhook' : event.trigger_type || '手动')} 触发</strong><time>${escapeHtml(displayAutomationTime(event.received_at))}</time></div>
      <div class="automation-event-meta"><span>幂等键：<code>${escapeHtml(event.idempotency_key || '—')}</code></span><span>请求摘要：<code>${escapeHtml((event.payload_sha256 || '').slice(0, 16) || '—')}</code></span></div>
      ${event.error ? `<p>${escapeHtml(event.error)}</p>` : ''}
    </article>
  `).join('') || '<div class="meta empty">尚无触发事件。手动试运行、计划调度或 Webhook 到达后会显示在这里。</div>';
}

function renderLoopNotifications() {
  const filter = $('loopNotificationFilter').value || 'all';
  const all = state.loopNotifications || [];
  const notices = filter === 'all' ? all : all.filter((notice) => notice.status === filter);
  const unread = all.filter((notice) => notice.status === 'unread').length;
  $('loopNotificationSummary').textContent = `${all.length} 条 · ${unread} 未读`;
  $('loopNotifications').innerHTML = notices.map((notice) => `
    <article class="automation-notice ${escapeHtml(notice.status)}">
      <div><span class="automation-unread-dot" aria-hidden="true"></span><strong>${escapeHtml(notice.title)}</strong><time>${escapeHtml(displayAutomationTime(notice.created_at))}</time></div>
      <p>${escapeHtml(notice.content)}</p>
      ${notice.status === 'unread' ? `<button class="text-button" data-notification-read="${escapeHtml(notice.id)}">标记已读</button>` : `<span class="small">已于 ${escapeHtml(displayAutomationTime(notice.read_at))} 阅读</span>`}
    </article>
  `).join('') || `<div class="meta empty">${all.length ? '当前筛选条件下没有通知' : '自动化完成、失败、等待审批或需要补充信息时会在这里通知。'}</div>`;
  document.querySelectorAll('[data-notification-read]').forEach((button) => {
    button.onclick = () => markLoopNotificationRead(button.dataset.notificationRead, button);
  });
}

async function markLoopNotificationRead(notificationId, button) {
  setBusy(button, true, '处理中…');
  try {
    const updated = await api(`/api/notifications/${notificationId}/read`, { method: 'POST' });
    state.loopNotifications = state.loopNotifications.map((notice) => notice.id === updated.id ? updated : notice);
    renderLoopNotifications();
  } catch (err) {
    setBusy(button, false);
    notify(`通知更新失败：${err.message || err}`, 'error');
  }
}

function loopPayload() {
  let automationState;
  try { automationState = JSON.parse($('loopState').value.trim() || '{}'); }
  catch (_) { throw new Error('状态必须是有效的 JSON 对象'); }
  if (!automationState || Array.isArray(automationState) || typeof automationState !== 'object') throw new Error('状态必须是 JSON 对象，不能是数组或普通文本');
  const triggerType = $('loopTriggerType').value;
  const intervalSeconds = Number($('loopInterval').value);
  const maxRuns = Number($('loopMaxRuns').value);
  const maxFailures = Number($('loopMaxFailures').value);
  const maxAttempts = Number($('loopMaxAttempts').value);
  const retryBackoff = Number($('loopRetryBackoff').value);
  if (triggerType === 'interval' && (!Number.isInteger(intervalSeconds) || intervalSeconds < 5)) throw new Error('固定间隔不能少于 5 秒');
  if (![maxRuns, maxFailures, maxAttempts, retryBackoff].every(Number.isInteger)) throw new Error('运行次数和重试设置必须填写整数');
  if (maxRuns < 1) throw new Error('最大轮数至少为 1');
  if (maxFailures < 1) throw new Error('连续失败熔断阈值至少为 1');
  if (maxAttempts < 1 || maxAttempts > 10) throw new Error('每轮最多尝试次数必须在 1 到 10 之间');
  if (retryBackoff < 0 || retryBackoff > 3600) throw new Error('重试等待必须在 0 到 3600 秒之间');
  const cronExpression = $('loopCronExpression').value.trim();
  if (triggerType === 'cron' && !cronExpression) throw new Error('请填写 Cron 表达式');
  let onceAt = '';
  if (triggerType === 'once') {
    const value = $('loopOnceAt').value;
    const parsed = new Date(value);
    if (!value || Number.isNaN(parsed.getTime())) throw new Error('请选择一次性执行时间');
    if (parsed.getTime() <= Date.now()) throw new Error('一次性执行时间必须晚于当前时间');
    onceAt = parsed.toISOString();
  }
  const payload = {
    ...platformScopeValues(),
    name: $('loopName').value.trim(), prompt: $('loopPrompt').value.trim(),
    agent_id: $('loopAgent').value, model_id: $('loopModel').value,
    trigger_type: triggerType, interval_seconds: intervalSeconds || 3600,
    cron_expression: cronExpression, once_at: onceAt,
    webhook_tolerance_seconds: Number($('loopWebhookTolerance').value) || 300,
    max_runs: maxRuns, max_failures: maxFailures,
    max_attempts: maxAttempts, retry_backoff_seconds: retryBackoff,
  };
  if (!state.selectedLoop) {
    payload.id = $('loopId').value.trim() || undefined;
    payload.initial_state = automationState;
  } else if (state.loopStateDirty && state.selectedLoop.status !== 'running') payload.state = automationState;
  const webhookSecret = $('loopWebhookSecret').value;
  if (triggerType === 'webhook') {
    const tolerance = Number($('loopWebhookTolerance').value);
    if (!Number.isInteger(tolerance) || tolerance < 30 || tolerance > 3600) throw new Error('Webhook 时间戳容差必须在 30 到 3600 秒之间');
    if (webhookSecret && webhookSecret.length < 16) throw new Error('Webhook 签名密钥至少需要 16 位');
    if (!webhookSecret && !state.selectedLoop?.webhook_secret_configured) throw new Error('Webhook 触发必须配置至少 16 位签名密钥');
    if (webhookSecret) payload.webhook_secret = webhookSecret;
  }
  return payload;
}

async function saveLoop() {
  const button = $('saveLoopBtn'); setBusy(button, true);
  try {
    const payload = loopPayload();
    if (!payload.name || !payload.prompt) throw new Error('请填写名称和自动化目标');
    const saved = state.selectedLoop
      ? await api(`/api/loops/${state.selectedLoop.id}`, { method: 'PUT', body: JSON.stringify(payload) })
      : await api('/api/loops', { method: 'POST', body: JSON.stringify(payload) });
    state.loops = await api(`/api/loops?${new URLSearchParams(platformScopeValues()).toString()}`); await selectLoop(saved.id); notify(`自动化“${saved.name}”已保存`);
  } catch (err) { notify(`自动化保存失败：${err.message || err}`, 'error'); }
  finally {
    setBusy(button, false);
    syncLoopTriggerFields();
    syncLoopActionButtons();
  }
}

async function loopAction(action) {
  if (!state.selectedLoop) return;
  const button = $({ run: 'runLoopBtn', start: 'startLoopBtn', pause: 'pauseLoopBtn' }[action]);
  setBusy(button, true, action === 'run' ? '提交中…' : '处理中…');
  try {
    await api(`/api/loops/${state.selectedLoop.id}/${action}`, { method: 'POST' });
    if (action === 'run') await new Promise((resolve) => setTimeout(resolve, 450));
    state.loops = await api(`/api/loops?${new URLSearchParams(platformScopeValues()).toString()}`); await selectLoop(state.selectedLoop.id);
    notify(action === 'run' ? '试运行已提交，运行历史会自动刷新' : action === 'start' ? '自动化调度已启动' : '自动化已暂停');
  } catch (err) { notify(`操作失败：${err.message || err}`, 'error'); }
  finally {
    setBusy(button, false);
    syncLoopTriggerFields();
    syncLoopActionButtons();
  }
}

async function deleteLoop() {
  if (!state.selectedLoop || !confirm(`确定删除自动化“${state.selectedLoop.name}”及其运行索引吗？已生成的普通任务和文件会保留。`)) return;
  await api(`/api/loops/${state.selectedLoop.id}`, { method: 'DELETE' });
  state.loops = await api(`/api/loops?${new URLSearchParams(platformScopeValues()).toString()}`); newLoop(); notify('自动化已删除，历史普通任务和产物仍保留');
}

function renderTasks() {
  $('taskList').innerHTML = state.tasks.map((t) => `
    <div class="task-card" data-task="${escapeHtml(t.id)}">
      <div class="card-title"><span>${escapeHtml(t.title)}</span><span class="status ${escapeHtml(t.status)}">${escapeHtml(t.status)}</span></div>
      <div class="card-desc">${escapeHtml(t.message)}</div>
      <div class="small">${escapeHtml(t.id)} · ${escapeHtml(t.created_at)}</div>
    </div>
  `).join('');
  document.querySelectorAll('[data-task]').forEach((el) => el.onclick = () => openTask(el.dataset.task));
}

async function openTask(id) {
  stopTaskStream();
  const task = await api(`/api/tasks/${id}`);
  state.currentTask = task;
  state.currentExpertSelection = null;
  setWorkbenchMode(task.executor_type === 'team' ? 'expert' : 'agent', { persist: false });
  if (task.executor_type === 'team' && $('expertTeamSelect') && enabledWorkbenchTeams().some((team) => team.id === task.executor_id)) {
    $('expertTeamSelect').value = task.executor_id;
    renderWorkbenchMode();
  }
  if (task.conversation_id) {
    state.conversationId = task.conversation_id;
    writePreference('conversation', state.conversationId);
  }
  switchTab('chat');
  renderTaskMeta(task);
  await watchTaskRuntime(id);
  await renderConversation(state.conversationId);
  const thinking = agentThinkingCard(id) || createAgentThinkingCard(id, { historical: true, open: false });
  thinking.historical = false;
  thinking.loaded = true;
  $('timeline').innerHTML = '';
  (task.events || []).forEach((event) => {
    updateAgentThinkingEvent(id, event);
    appendEvent(event);
  });
  renderArtifacts(task.artifacts || []);
  if (runtimeIsActive(currentRuntimeStatus())) {
    thinking.node.open = true;
    state.taskUiRunning = taskUiGeneratingStatus(currentRuntimeStatus());
    state.taskUiTaskId = id;
    setSendButtonState(state.taskUiRunning ? 'running' : 'idle');
    setTaskUiStatus(id, state.taskUiRunning ? '正在恢复任务执行状态…' : '等待你的确认…', state.taskUiRunning ? 'thinking' : 'verifying');
    startTaskStream(id, { seenEventIds: (task.events || []).map((event) => event.id) });
  } else if (['completed', 'failed', 'cancelled'].includes(task.status)) {
    state.taskUiTaskId = id;
    finishTaskUi(id, task.status);
  }
}

async function renderConversation(conversationId) {
  const data = await api(`/api/conversations/${encodeURIComponent(conversationId)}/messages`);
  $('conversation').innerHTML = '';
  for (const message of data.messages || []) {
    if (message.message_type === 'error') {
      publishTaskError(message.task_id || '', message);
      continue;
    }
    const localizedContent = message.role === 'assistant'
      ? localizeMissingInformationText(message.content)
      : message.content;
    if (message.role === 'assistant' && message.task_id && looksLikeClarificationResponse(localizedContent)) {
      publishClarification(message.task_id, { id: message.event_id, content: localizedContent });
      continue;
    }
    const messageNode = addMessage(message.role === 'assistant' ? 'agent' : 'user', localizedContent, message.event_id || null);
    if (message.task_id) messageNode.dataset.taskMessage = String(message.task_id);
    if (message.role === 'user' && message.task_id) {
      createAgentThinkingCard(message.task_id, { historical: true, open: false });
    }
  }
  return (data.messages || []).length > 0;
}

function newConversation() {
  stopTaskStream();
  state.taskUiRunning = false;
  state.taskUiCancelRequested = false;
  clearTaskUiStatus();
  setSendButtonState('idle');
  state.currentTask = null;
  state.currentExpertSelection = null;
  state.conversationId = createConversationId();
  writePreference('conversation', state.conversationId);
  $('conversation').innerHTML = '';
  $('timeline').innerHTML = '';
  $('taskMeta').className = 'meta empty';
  $('taskMeta').textContent = '尚未创建任务';
  $('taskOverviewSection').open = false;
  $('taskOverviewStatus').textContent = '尚未选择任务';
  $('timelineSection').open = false;
  $('timelineStatus').textContent = '按需查看节点日志';
  resetTaskRuntime();
  renderArtifacts([]);
  addMessage('agent', '新对话已开始。你可以继续描述要完成的事情。');
  loadMemoriesOnly({ preserveSelection: false }).catch((err) => notify(`记忆刷新失败：${err.message || err}`, 'error'));
}

function setSidebarCollapsed(collapsed) {
  const sidebar = $('sidebar');
  const toggle = $('sidebarToggle');
  if (!sidebar || !toggle) return;
  sidebar.classList.toggle('collapsed', collapsed);
  toggle.setAttribute('aria-label', collapsed ? '展开侧边栏' : '收起侧边栏');
  toggle.title = collapsed ? '展开侧边栏' : '收起侧边栏';
  writePreference('sidebar-collapsed', collapsed ? '1' : '0');
}

function initSidebar() {
  const collapsed = readPreference('sidebar-collapsed') === '1';
  setSidebarCollapsed(collapsed);
}

function bindEvents() {
  $('addWorkspaceMemberBtn').onclick = addWorkspaceMember;
  $('newUserBtn').onclick = () => selectAdminUser(null);
  $('refreshUsersBtn').onclick = () => loadAdminUsers().catch((error) => notify(error.message, 'error'));
  $('adminUserForm').onsubmit = saveAdminUser;
  document.querySelectorAll('.nav').forEach((btn) => btn.onclick = () => switchTab(btn.dataset.tab));
  $('sidebarToggle').onclick = () => setSidebarCollapsed(!$('sidebar').classList.contains('collapsed'));
  $('workspaceSelect').onchange = (event) => switchWorkspace(event.target.value).catch((err) => notify(`项目切换失败：${err.message || err}`, 'error'));
  $('openWorkspaceTabBtn').onclick = () => switchTab('workspaces');
  document.querySelectorAll('[data-workbench-mode]').forEach((button) => {
    button.onclick = () => setWorkbenchMode(button.dataset.workbenchMode);
  });
  $('expertTeamSelect').onchange = (event) => {
    writePreference('expert-team', event.target.value);
    renderWorkbenchMode();
  };
  $('sendBtn').onclick = sendTask;
    if ($('viewCodexSessionsBtn')) $('viewCodexSessionsBtn').onclick = openCodexSessionsDialog;
  if ($('closeCodexSessionsBtn')) $('closeCodexSessionsBtn').onclick = () => $('codexSessionsDialog').classList.add('hidden');
  if ($('backToCodexSessionsBtn')) $('backToCodexSessionsBtn').onclick = () => {
    $('codexSessionDetailContainer').classList.add('hidden');
    $('codexSessionsListContainer').classList.remove('hidden');
  };
  if ($('codexSessionsDialog')) $('codexSessionsDialog').addEventListener('click', (event) => {
    if (event.target === $('codexSessionsDialog')) $('codexSessionsDialog').classList.add('hidden');
  });
  $('closePptxConfigBtn').onclick = closePptxConfigDialog;
  $('detectPptxConfigBtn').onclick = () => loadPptxConfiguration().catch((err) => notify(`重新检测失败：${err.message || err}`, 'error'));
  $('savePptxConfigBtn').onclick = () => savePptxConfiguration().catch((err) => notify(`PPTX 配置失败：${err.message || err}`, 'error'));
  ['pptxModePython', 'pptxModeArtifact'].forEach((id) => {
    $(id).onchange = () => $('pptxArtifactFields').classList.toggle('hidden', !$('pptxModeArtifact').checked);
  });
  $('pptxConfigDialog').addEventListener('click', (event) => {
    if (event.target === $('pptxConfigDialog')) closePptxConfigDialog();
  });
  $('conversation').addEventListener('click', (event) => {
    const copyButton = event.target.closest('[data-copy-code]');
    if (copyButton) {
      const code = copyButton.closest('.code-block')?.querySelector('code')?.textContent || '';
      copyTextToClipboard(code)
        .then(() => {
          copyButton.classList.add('copied');
          copyButton.textContent = '已复制';
          window.setTimeout(() => {
            copyButton.classList.remove('copied');
            copyButton.textContent = '复制';
          }, 1200);
        })
        .catch((err) => notify(`复制失败：${err.message || err}`, 'error'));
      return;
    }
    const button = event.target.closest('[data-open-pptx-config]');
    if (button) openPptxConfigDialog().catch((err) => notify(`读取 PPTX 配置失败：${err.message || err}`, 'error'));
  });
  $('newConversationBtn').onclick = newConversation;
  $('agentSelect').onchange = (e) => {
    writePreference('agent', e.target.value);
    syncMemoryScopeId();
    loadMemoriesOnly({ preserveSelection: false }).catch((err) => notify(`记忆刷新失败：${err.message || err}`, 'error'));
  };
  $('taskModelSelect').onchange = (e) => {
    writePreference('model', e.target.value);
    writePreference('model-explicit', '1');
    renderWorkbenchModelStatus();
  };
  $('modelStatusPill').onclick = () => {
    const model = currentWorkbenchModel();
    switchTab('models');
    if (model) selectModel(model.id);
  };
  $('exampleBtn').onclick = () => {
    $('messageInput').value = state.workbenchMode === 'expert'
      ? '请从业务价值、交付风险和用户体验三个角度评审这份方案，归纳一致结论与主要分歧，并给出按优先级排序的改进建议。'
      : '帮我把本周工作进展整理成摘要、主要风险和下周行动计划。';
    $('messageInput').focus();
  };
  $('refreshBtn').onclick = loadAll;
  $('refreshWorkspacesBtn').onclick = () => loadWorkspacesOnly({ preserveSelection: true }).catch((err) => notify(`项目刷新失败：${err.message || err}`, 'error'));
  $('workspaceSearch').oninput = () => renderWorkspaces();
  $('workspaceShowDisabled').onchange = () => renderWorkspaces();
  $('agentSearch').oninput = () => renderAgents();
  $('agentShowDisabled').onchange = () => renderAgents();
  $('newWorkspaceBtn').onclick = newWorkspace;
  $('saveWorkspaceBtn').onclick = saveWorkspace;
  $('deleteWorkspaceBtn').onclick = deleteWorkspace;
  $('newSkillBtn').onclick = newSkill;
  $('skillSearch').oninput = () => renderSkills();
  $('skillShowDisabled').onchange = () => renderSkills();
  $('skillPackageInput').onchange = (e) => e.target.files[0] && installSkillPackage(e.target.files[0]);
  $('installSkillUrlBtn').onclick = installSkillFromUrl;
  $('saveSkillBtn').onclick = saveSkill;
  $('deleteSkillBtn').onclick = () => deleteSkill().catch((err) => notify(`技能卸载失败：${err.message || err}`, 'error'));
  $('newSkillFileBtn').onclick = newSkillFile;
  $('exportSkillBtn').onclick = exportSelectedSkill;
  $('skillFileUploadInput').onchange = (e) => {
    const file = e.target.files?.[0];
    if (file) uploadSkillFile(file).catch((err) => notify(`文件上传失败：${err.message || err}`, 'error'));
    e.target.value = '';
  };
  $('saveSkillFileBtn').onclick = () => saveSkillFile().catch((err) => notify(`文件保存失败：${err.message || err}`, 'error'));
  $('deleteSkillFileBtn').onclick = () => deleteSkillFile().catch((err) => notify(`文件删除失败：${err.message || err}`, 'error'));
  $('refreshMarketplaceBtn').onclick = () => loadMarketplaceOnly().catch((err) => notify(`市场刷新失败：${err.message || err}`, 'error'));
  $('invokeToolBtn').onclick = invokeTool;
  $('newMcpBtn').onclick = newMcp; $('saveMcpBtn').onclick = saveMcp; $('discoverMcpBtn').onclick = discoverMcp; $('deleteMcpBtn').onclick = () => deleteMcp().catch((err) => notify(`工具服务卸载失败：${err.message || err}`, 'error'));
  $('mcpImportInput').onchange = (e) => e.target.files[0] && importMcpPackage(e.target.files[0]);
  $('installMcpUrlBtn').onclick = installMcpFromUrl;
  $('newAgentBtn').onclick = newAgent; $('saveAgentBtn').onclick = saveAgent;
  $('reloadExpertsBtn').onclick = () => loadExpertWorkspace({ preserveSelection: true }).catch((err) => notify(`专家团刷新失败：${err.message || err}`, 'error'));
  $('newExpertTemplateBtn').onclick = newExpertTemplate;
  $('saveExpertTemplateBtn').onclick = saveExpertTemplate;
  $('deleteExpertTemplateBtn').onclick = deleteExpertTemplate;
  $('installExpertTemplateBtn').onclick = installSelectedExpertTemplate;
  $('newExpertTeamBtn').onclick = newExpertTeam;
  $('saveExpertTeamBtn').onclick = saveExpertTeam;
  $('deleteExpertTeamBtn').onclick = deleteExpertTeam;
  $('addExpertMemberBtn').onclick = addExpertTeamMember;
  $('runExpertTeamBtn').onclick = runSelectedExpertTeam;
  $('refreshKnowledgeBtn').onclick = () => loadKnowledgeBasesOnly({ preserveSelection: true }).catch((err) => notify(`知识库刷新失败：${err.message || err}`, 'error'));
  $('newKnowledgeBtn').onclick = () => newKnowledgeBase();
  $('saveKnowledgeBaseBtn').onclick = saveKnowledgeBase;
  $('deleteKnowledgeBaseBtn').onclick = deleteKnowledgeBase;
  $('runDiagnosticsBtn').onclick = () => loadDiagnosticsOnly().catch((err) => notify(`自检失败：${err.message || err}`, 'error'));
  $('copyDiagnosticsReportBtn').onclick = () => copyDiagnosticsReport().catch((err) => notify(`复制失败：${err.message || err}`, 'error'));
  $('downloadDiagnosticsReportBtn').onclick = () => downloadDiagnosticsReport().catch((err) => notify(`下载失败：${err.message || err}`, 'error'));
  $('downloadDiagnosticsJsonBtn').onclick = () => downloadDiagnosticsJson().catch((err) => notify(`下载失败：${err.message || err}`, 'error'));
  $('knowledgeFileInput').onchange = (event) => {
    const file = event.target.files?.[0];
    if (file) indexKnowledgeFile(file);
  };
  $('knowledgeSearchBtn').onclick = searchKnowledge;
  $('knowledgeSearchInput').addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') searchKnowledge();
  });
  $('refreshMemoriesBtn').onclick = () => loadMemoriesOnly({ preserveSelection: true }).catch((err) => notify(`记忆刷新失败：${err.message || err}`, 'error'));
  $('newMemoryBtn').onclick = newMemory;
  $('saveMemoryBtn').onclick = saveMemory;
  $('deleteMemoryBtn').onclick = () => deleteMemory().catch((err) => notify(`记忆删除失败：${err.message || err}`, 'error'));
  $('previewMemoryContextBtn').onclick = () => loadEffectiveMemoryContext().catch((err) => notify(`上下文读取失败：${err.message || err}`, 'error'));
  $('conversationSummarySelect').onchange = (event) => selectConversationSummary(event.target.value);
  $('saveConversationSummaryBtn').onclick = saveConversationSummary;
  $('deleteConversationSummaryBtn').onclick = deleteConversationSummary;
  $('memoryScopeType').onchange = syncMemoryScopeId;
  $('memoryScopeFilter').onchange = renderMemories;
  $('memoryStatusFilter').onchange = renderMemories;
  $('reloadArtifactsBtn').onclick = () => loadArtifactsOnly({ preserveSelection: true }).catch((err) => notify(`产物刷新失败：${err.message || err}`, 'error'));
  $('artifactKindFilter').onchange = () => loadArtifactsOnly({ preserveSelection: true }).catch((err) => notify(`产物筛选失败：${err.message || err}`, 'error'));
  $('newModelBtn').onclick = newModel; $('saveModelBtn').onclick = saveModel; $('testModelBtn').onclick = testModel; $('deleteModelBtn').onclick = () => deleteModel().catch((err) => notify(`模型删除失败：${err.message || err}`, 'error'));
  $('modelKeyMode').onchange = toggleModelKeyMode;
  if ($('saveEngineBtn')) $('saveEngineBtn').onclick = () => saveExecutionEngine().catch((err) => notify(`执行引擎保存失败：${err.message || err}`, 'error'));
  if ($('engineKeyMode')) $('engineKeyMode').onchange = toggleEngineKeyMode;
  if ($('executionEngineSelect')) $('executionEngineSelect').onchange = (event) => {
    writePreference('execution-engine', event.target.value);
    syncExecutionEngineModelUi();
  };
  if ($('testEngineBtn')) $('testEngineBtn').onclick = () => testExecutionEngine().catch((err) => notify(`测试失败：${err.message || err}`, 'error'));
  if ($('discoverModelsBtn')) $('discoverModelsBtn').onclick = discoverModelsForModelConfig;
  if ($('discoverEngineModelsBtn')) $('discoverEngineModelsBtn').onclick = discoverModelsForExecutionEngine;
  $('fileInput').onchange = (e) => {
    uploadFiles(Array.from(e.target.files || []));
    e.target.value = '';
  };
  $('reloadTasksBtn').onclick = loadTasksOnly;
  $('cancelTaskBtn').onclick = () => sendTaskRuntimeCommand('cancel', {}, $('cancelTaskBtn'));
  $('retryTaskBtn').onclick = () => sendTaskRuntimeCommand('retry', {}, $('retryTaskBtn'));
  $('resumeTaskBtn').onclick = () => sendTaskRuntimeCommand('resume', {}, $('resumeTaskBtn'));
  $('runtimeMessageBtn').onclick = submitRuntimeMessage;
  $('runtimeMessage').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') submitRuntimeMessage();
  });
  $('reloadLoopsBtn').onclick = loadLoopsOnly; $('newLoopBtn').onclick = newLoop; $('saveLoopBtn').onclick = saveLoop;
  $('runLoopBtn').onclick = () => loopAction('run'); $('startLoopBtn').onclick = () => loopAction('start'); $('pauseLoopBtn').onclick = () => loopAction('pause');
  $('deleteLoopBtn').onclick = () => deleteLoop().catch((err) => notify(`删除失败：${err.message || err}`, 'error'));
  $('loopTriggerType').onchange = () => { state.loopEditorDirty = true; syncLoopTriggerFields(); };
  $('loopId').oninput = () => { state.loopEditorDirty = true; syncLoopTriggerFields(); };
  [
    'loopName', 'loopAgent', 'loopModel', 'loopInterval', 'loopCronExpression', 'loopOnceAt',
    'loopWebhookSecret', 'loopWebhookTolerance', 'loopMaxRuns', 'loopMaxFailures',
    'loopMaxAttempts', 'loopRetryBackoff', 'loopPrompt',
  ].forEach((id) => $(id).addEventListener('input', () => { state.loopEditorDirty = true; }));
  $('loopState').addEventListener('input', () => { state.loopEditorDirty = true; state.loopStateDirty = true; });
  $('loopNotificationFilter').onchange = renderLoopNotifications;
  syncLoopTriggerFields();
  $('messageInput').addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') sendTask();
  });
}

function selectAdminUser(user) {
  state.adminSelectedUser = user;
  $('adminUserTitle').textContent = user ? `编辑用户：${user.username}` : '新建用户';
  $('adminUsername').value = user?.username || '';
  $('adminUsername').disabled = Boolean(user);
  $('adminUserRole').value = user?.role || 'user';
  $('adminUserPassword').value = '';
  $('adminUserPassword').required = !user;
  $('adminPasswordHint').textContent = user ? '留空保留当前密码；填写新密码将撤销已有会话。' : '新用户需设置至少 12 位密码。';
  $('adminUserEnabled').checked = user ? Boolean(user.enabled) : true;
  $('adminUserEnabled').disabled = !user;
  $('adminUserError').textContent = '';
}

async function loadAdminUsers() {
  state.adminUsers = await api('/api/users');
  const list = $('adminUserList');
  list.replaceChildren();
  for (const user of state.adminUsers) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary';
    button.textContent = `${user.username} · ${user.role === 'admin' ? '管理员' : '普通用户'} · ${user.enabled ? '已启用' : '已停用'}`;
    button.onclick = () => selectAdminUser(user);
    list.appendChild(button);
  }
  const selected = state.adminUsers.find((user) => user.id === state.adminSelectedUser?.id);
  selectAdminUser(selected || null);
}

async function saveAdminUser(event) {
  event.preventDefault();
  const selected = state.adminSelectedUser;
  const payload = { role: $('adminUserRole').value };
  if (selected) payload.enabled = $('adminUserEnabled').checked;
  else payload.username = $('adminUsername').value.trim();
  const password = $('adminUserPassword').value;
  if (password) payload.password = password;
  if (!selected && !password) {
    $('adminUserError').textContent = '新用户必须设置密码。';
    return;
  }
  $('saveUserBtn').disabled = true;
  $('adminUserError').textContent = '';
  try {
    const user = await api(selected ? `/api/users/${encodeURIComponent(selected.id)}` : '/api/users', {
      method: selected ? 'PUT' : 'POST', body: JSON.stringify(payload),
    });
    state.adminSelectedUser = user;
    await loadAdminUsers();
    notify('用户已保存');
  } catch (error) {
    $('adminUserError').textContent = error.message;
  } finally {
    $('adminUserPassword').value = '';
    $('saveUserBtn').disabled = false;
  }
}

async function initializeAuthentication() {
  const session = await api('/api/auth/me');
  if (session.enabled && !session.authenticated) {
    $('loginPanel').classList.remove('hidden');
    $('loginForm').onsubmit = async (event) => {
      event.preventDefault();
      $('loginSubmit').disabled = true;
      $('loginError').textContent = '';
      try {
        await api('/api/auth/login', { method: 'POST', body: JSON.stringify({
          username: $('loginUsername').value.trim(), password: $('loginPassword').value,
        }) });
        location.reload();
      } catch (error) {
        $('loginError').textContent = error.message;
        $('loginPassword').value = '';
      } finally {
        $('loginSubmit').disabled = false;
      }
    };
    $('loginUsername').focus();
    return false;
  }
  state.authenticated = Boolean(session.authenticated);
  state.currentUser = session.user || null;
  state.authEnabled = Boolean(session.enabled);
  if (session.enabled) {
    if (session.user.role === 'admin') {
      $('usersNav').classList.remove('hidden');
      $('enginesNav')?.classList.remove('hidden');
    }
    $('accountPanel').classList.remove('hidden');
    $('accountName').textContent = session.user.username;
    $('logoutButton').onclick = async () => {
      try {
        await api('/api/auth/logout', { method: 'POST' });
        location.reload();
      } catch (error) { notify(error.message, 'error'); }
    };
    if (readPreference('account-id') !== session.user.user_id) {
      state.conversationId = createConversationId();
      state.workspaceId = 'default';
      writePreference('conversation', state.conversationId);
      writePreference('workspace', state.workspaceId);
      writePreference('account-id', session.user.user_id);
    }
  } else {
    $('enginesNav')?.classList.remove('hidden');
  }
  document.body.classList.remove('auth-pending');
  return true;
}

(async function init() {
  try {
    if (!await initializeAuthentication()) return;
  } catch (error) {
    $('loginPanel').classList.remove('hidden');
    $('loginError').textContent = `无法检查登录状态：${error.message}`;
    $('loginSubmit').disabled = true;
    return;
  }
  initSidebar();
  bindEvents();
  renderWorkbenchMode();
  window.addEventListener('unhandledrejection', (event) => notify(event.reason?.message || '操作失败，请检查平台连接', 'error'));
  if (location.protocol === 'file:') $('connectionBanner').classList.remove('hidden');
  try {
    await loadAll();
    if ($('serviceStatus')) {
      $('serviceStatus').classList.remove('offline');
      $('serviceStatus').querySelector('b').textContent = '服务已连接';
    }
    writePreference('conversation', state.conversationId);
    const restored = await renderConversation(state.conversationId);
    if (!restored) addMessage('agent', '你好，我可以帮你分析资料、调用工具并生成文档。直接告诉我你想完成什么。');
  } catch (err) {
    if ($('serviceStatus')) {
      $('serviceStatus').classList.add('offline');
      $('serviceStatus').querySelector('b').textContent = '服务未连接';
    }
    addMessage('agent', location.protocol === 'file:' ? '页面尚未连接到平台服务，请从已启动的平台地址访问。' : `加载平台配置失败：${err.message || err}`);
  }
})();
