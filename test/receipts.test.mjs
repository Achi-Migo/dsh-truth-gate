import test from 'node:test'
import assert from 'node:assert/strict'
import { createRequire } from 'node:module'

// After build, import from lib; for pre-build use dynamic compile skip — require built js
const require = createRequire(import.meta.url)

test('shell receipt extracts exitCode from value', async () => {
  const { shellReceiptFromResult } = await import('../lib/receipts.js')
  const msg = shellReceiptFromResult(
    's1',
    'bash',
    { command: 'python -m pytest -q' },
    {
      isError: false,
      value: {
        exitCode: 1,
        stdout: { text: '1 failed\n', truncated: false },
        stderr: { text: '', truncated: false },
      },
      content: [{ type: 'text', text: '1 failed\n[exit code: 1]' }],
    },
    'C:/repo',
  )
  assert.equal(msg.kind, 'shell')
  assert.equal(msg.exit_code, 1)
  assert.match(String(msg.command), /pytest/)
})

test('edit receipt skips view command', async () => {
  const { editReceiptFromArgs } = await import('../lib/receipts.js')
  assert.equal(
    editReceiptFromArgs('s1', 'str_replace_editor', { command: 'view', path: '/a.py' }),
    null,
  )
  const msg = editReceiptFromArgs('s1', 'str_replace_editor', {
    command: 'str_replace',
    path: '/tmp/a.py',
  })
  assert.equal(msg.kind, 'edit')
  assert.equal(msg.path, '/tmp/a.py')
})
