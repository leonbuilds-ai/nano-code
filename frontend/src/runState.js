export function initialRunState(messages = []) {
  return { messages, draftIndex: null, approval: null, terminal: null, resetSessionId: null, error: null }
}

export function applyRunEvent(state, kind, data = {}) {
  const next = { ...state, messages: [...state.messages] }
  if (kind === 'delta') {
    if (next.draftIndex === null) {
      next.draftIndex = next.messages.length
      next.messages.push({ role: 'assistant', content: '', live: true })
    }
    const index = next.draftIndex
    next.messages[index] = { ...next.messages[index], content: next.messages[index].content + (data.text || '') }
  } else if (kind === 'tool_start') {
    next.draftIndex = null
    next.messages.push({ role: 'tool', title: data.name || data.tool || 'Tool', input: data.arguments || data.input, open: false, state: 'running' })
  } else if (kind === 'tool_result') {
    const index = next.messages.findLastIndex(m => m.role === 'tool' && m.state === 'running')
    if (index >= 0) next.messages[index] = { ...next.messages[index], output: data.output || data.result || data.error, state: data.error ? 'error' : 'done' }
  } else if (kind === 'approval') {
    next.approval = data
  } else if (kind === 'reset') {
    next.resetSessionId = data.session_id
  } else if (kind === 'error') {
    next.terminal = 'error'
    next.error = data.message || '运行失败'
  } else if (kind === 'done') {
    next.terminal = 'done'
  }
  return next
}
