<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { applyRunEvent, initialRunState } from './runState.js'

const sessions = ref([]), selected = ref(null), runView = ref(initialRunState()), prompt = ref('')
const busy = ref(false), runId = ref(null), error = ref('')
const status = ref(null), sidebarOpen = ref(false), feed = ref(null)
let stream = null
let lastPrompt = ''
const current = computed(() => sessions.value.find(s => s.id === selected.value))
const canSend = computed(() => !!prompt.value.trim() && !busy.value)
const messages = computed(() => runView.value.messages)
const approval = computed(() => runView.value.approval)
const toolState = state => ({ running: '运行中', done: '已完成', error: '失败' })[state] || state

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } })
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`
    try { detail = (await response.json()).detail || detail } catch {}
    throw new Error(detail)
  }
  return response.json()
}
async function loadSessions() {
  sessions.value = await api('/api/sessions')
  if (selected.value && !sessions.value.some(s => s.id === selected.value)) selected.value = null
}
async function openSession(id) {
  if (busy.value) return
  try {
    selected.value = id; sidebarOpen.value = false; error.value = ''
    const session = await api(`/api/sessions/${encodeURIComponent(id)}`)
    runView.value = initialRunState((session.messages || []).map(m => m.role === 'tool'
      ? { role: 'tool', title: 'Tool result', output: m.content, open: false, state: 'done' }
      : { ...m }))
    scrollDown()
  } catch (e) { error.value = e.message }
}
async function newSession() {
  try {
    const session = await api('/api/sessions', { method: 'POST', body: JSON.stringify({ title: '新会话' }) })
    await loadSessions(); await openSession(session.id)
  } catch (e) { error.value = e.message }
}
async function renameSession() {
  if (!current.value || busy.value) return
  const title = window.prompt('会话名称', current.value.title)
  if (!title?.trim()) return
  try {
    await api(`/api/sessions/${encodeURIComponent(selected.value)}`, { method: 'PATCH', body: JSON.stringify({ title: title.trim() }) })
    await loadSessions()
  } catch (e) { error.value = e.message }
}
function scrollDown() { nextTick(() => { if (feed.value) feed.value.scrollTop = feed.value.scrollHeight }) }
function finish() {
  busy.value = false; runView.value = { ...runView.value, approval: null }; stream?.close(); stream = null
  loadSessions().catch(e => { error.value = e.message })
}
function handleEvent(kind, data) {
  runView.value = applyRunEvent(runView.value, kind, data)
  if (kind === 'reset') {
    cancel().finally(() => { finish(); openSession(data.session_id); error.value = '事件记录已过期，请检查会话后重试。' })
  } else if (kind === 'error') { error.value = runView.value.error; finish() }
  else if (kind === 'done') { finish(); openSession(data.session_id) }
  scrollDown()
}
async function send() {
  if (!canSend.value) return
  try {
    if (!selected.value) await newSession()
    if (!selected.value) return
    const text = prompt.value.trim(); lastPrompt = text; prompt.value = ''; error.value = ''; busy.value = true
    runView.value = { ...runView.value, draftIndex: null, messages: [...messages.value, { role: 'user', content: text }] }; scrollDown()
    const run = await api(`/api/sessions/${encodeURIComponent(selected.value)}/runs`, { method: 'POST', body: JSON.stringify({ prompt: text }) })
    runId.value = run.id
    stream = new EventSource(`/api/runs/${encodeURIComponent(run.id)}/events`)
    for (const kind of ['status', 'delta', 'tool_start', 'tool_result', 'approval', 'done', 'error', 'reset'])
      stream.addEventListener(kind, event => { try { handleEvent(kind, JSON.parse(event.data)) } catch (e) { error.value = e.message; finish() } })
    stream.onopen = () => { if (error.value === '连接中断，正在重连…') error.value = '' }
    stream.onerror = () => { if (busy.value) error.value = '连接中断，正在重连…' }
  } catch (e) { error.value = e.message; busy.value = false }
}
async function decide(approved) {
  if (!approval.value) return
  try {
    await api(`/api/runs/${encodeURIComponent(runId.value)}/approvals/${encodeURIComponent(approval.value.approval_id)}`, { method: 'POST', body: JSON.stringify({ approved }) })
    runView.value = { ...runView.value, approval: null }
  } catch (e) { error.value = e.message }
}
async function cancel() {
  if (!runId.value) return
  try { await api(`/api/runs/${encodeURIComponent(runId.value)}/cancel`, { method: 'POST' }) }
  catch (e) { error.value = e.message }
}
function onKeydown(event) { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); send() } }
function retry() { prompt.value = lastPrompt; send() }
onMounted(async () => {
  try {
    const [s] = await Promise.all([api('/api/status'), loadSessions()]); status.value = s
    if (sessions.value.length) await openSession(sessions.value[0].id)
  } catch (e) { error.value = e.message }
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ visible: sidebarOpen }">
      <div class="brand"><span class="brand-icon">▣</span><span>nano<span class="accent">code</span></span><button class="sidebar-close" @click="sidebarOpen = false" aria-label="关闭侧栏">×</button></div>
      <button class="new-button" :disabled="busy" @click="newSession"><span>＋</span> 新建会话</button>
      <div class="section-label">会话列表 <span>{{ sessions.length }}</span></div>
      <nav class="session-list" aria-label="会话列表"><button v-for="session in sessions" :key="session.id" class="session" :class="{ active: session.id === selected }" @click="openSession(session.id)"><span class="session-icon">◇</span><span class="session-info"><span class="session-title">{{ session.title }}</span><small>{{ session.message_count || 0 }} 条消息</small></span><span class="session-dot" /></button></nav>
      <div class="sidebar-bottom"><span class="live-dot" /> 本地工作区 <span class="workspace-name">{{ status?.workspace || '—' }}</span></div>
    </aside>
    <div v-if="sidebarOpen" class="scrim" @click="sidebarOpen = false" />
    <main class="main">
      <header class="topbar"><button class="menu-button" @click="sidebarOpen = true" aria-label="打开侧栏">☰</button><div class="breadcrumbs"><span>工作区</span><b>/</b><strong>{{ current?.title || '新会话' }}</strong><small v-if="current">{{ current.message_count || 0 }} 条消息</small></div><div class="top-actions"><span class="model-badge"><i />{{ status?.model || '连接中' }}</span><button class="icon-button" @click="renameSession" :disabled="!selected || busy" title="重命名会话" aria-label="重命名会话">✎</button></div></header>
      <div ref="feed" class="feed">
        <div v-if="!messages.length" class="welcome"><div class="welcome-mark">▣</div><div class="eyebrow">本地编码工作台</div><h1>今天想做些什么？</h1><p>提问、探索代码，或描述你想修改的内容。NanoCode 会和你一起完成。</p><div class="welcome-hint"><span>↵</span> 在下方输入任务开始</div></div>
        <div v-else class="conversation"><template v-for="(message, index) in messages" :key="index">
          <div v-if="message.role === 'tool'" class="tool-card"><button class="tool-head" @click="message.open = !message.open"><span class="tool-symbol">⌘</span><span>{{ message.title }}</span><span class="tool-state">{{ toolState(message.state) }}</span><span class="chevron">{{ message.open ? '⌃' : '⌄' }}</span></button><pre v-if="message.open">{{ JSON.stringify({ input: message.input, output: message.output }, null, 2) }}</pre></div>
          <div v-else class="message" :class="message.role"><div class="avatar">{{ message.role === 'user' ? '我' : '▣' }}</div><div class="message-body"><div class="message-label">{{ message.role === 'user' ? '我' : 'NANOCODE' }}</div><div class="message-text">{{ message.content }}</div><span v-if="message.live && busy" class="typing-cursor" /></div></div>
        </template><div v-if="approval" class="approval"><div class="approval-title"><span>⚠</span> 等待审批</div><p>以下操作需要你明确批准后才会执行。</p><pre>{{ JSON.stringify(approval.request, null, 2) }}</pre><div class="approval-actions"><button class="deny" @click="decide(false)">拒绝</button><button class="allow" @click="decide(true)">批准操作 →</button></div></div></div>
      </div>
      <div class="composer-wrap"><div v-if="error" class="error-banner"><span>!</span>{{ error }}<button v-if="!busy && lastPrompt" @click="retry">重试</button><button @click="error = ''" aria-label="关闭错误提示">×</button></div><div class="composer"><textarea v-model="prompt" rows="2" placeholder="向 NanoCode 描述你的任务…" :disabled="busy" @keydown="onKeydown" aria-label="消息" /><div class="composer-bottom"><span><kbd>Enter</kbd> 发送 <span class="shortcut-divider">·</span> <kbd>Shift Enter</kbd> 换行</span><button v-if="busy" class="stop-button" @click="cancel">■ 停止</button><button v-else class="send-button" :disabled="!canSend" @click="send" aria-label="发送消息">↑</button></div></div><div class="disclaimer">请在使用前检查 NanoCode 生成的修改。</div></div>
    </main>
  </div>
</template>
