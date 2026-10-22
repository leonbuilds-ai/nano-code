import test from 'node:test'
import assert from 'node:assert/strict'
import { applyRunEvent, initialRunState } from '../src/runState.js'

test('streams assistant turns around a tool result', () => {
  let state = initialRunState()
  state = applyRunEvent(state, 'delta', { text: 'first ' })
  state = applyRunEvent(state, 'delta', { text: 'turn' })
  state = applyRunEvent(state, 'tool_start', { name: 'read_file', arguments: { path: 'a.txt' } })
  state = applyRunEvent(state, 'tool_result', { result: 'contents' })
  state = applyRunEvent(state, 'delta', { text: 'second turn' })
  assert.deepEqual(state.messages.map(m => m.role), ['assistant', 'tool', 'assistant'])
  assert.equal(state.messages[0].content, 'first turn')
  assert.equal(state.messages[1].output, 'contents')
  assert.equal(state.messages[2].content, 'second turn')
})

test('approval is bound to supplied id and reset requests session reload', () => {
  let state = applyRunEvent(initialRunState(), 'approval', { approval_id: 'abc', request: { tool: 'write_file' } })
  assert.equal(state.approval.approval_id, 'abc')
  state = applyRunEvent(state, 'reset', { session_id: 'session-1' })
  assert.equal(state.resetSessionId, 'session-1')
  state = applyRunEvent(state, 'done', { session_id: 'session-1' })
  assert.equal(state.terminal, 'done')
})
