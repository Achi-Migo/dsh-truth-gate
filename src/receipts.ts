/** Map DSH tool executions to opc_truth bridge receipt messages. */

const SHELL_TOOLS = new Set(['bash', 'pwsh', 'shell', 'Bash', 'PowerShell'])
const EDIT_TOOLS = new Set([
  'str_replace_editor',
  'StrReplaceEditor',
  'write',
  'Write',
  'edit',
  'Edit',
])

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' ? v as Record<string, unknown> : null
}

function textFromContent(content: unknown): string {
  if (!Array.isArray(content)) return typeof content === 'string' ? content : ''
  const parts: string[] = []
  for (const block of content) {
    const b = asRecord(block)
    if (b?.type === 'text' && typeof b.text === 'string') parts.push(b.text)
  }
  return parts.join('\n')
}

export function isShellTool(name: string): boolean {
  return SHELL_TOOLS.has(name) || /bash|pwsh|powershell|shell/i.test(name)
}

export function isEditTool(name: string): boolean {
  return EDIT_TOOLS.has(name) || /str_replace|write_file|apply_patch|edit/i.test(name)
}

export function shellReceiptFromResult(
  session: string,
  name: string,
  args: unknown,
  result: { isError: boolean; value?: unknown; content?: unknown },
  repo?: string,
  treeFp = true,
): Record<string, unknown> | null {
  const a = asRecord(args) || {}
  const command = String(a.command ?? a.cmd ?? '')
  if (!command) return null
  const value = asRecord(result.value)
  let exitCode: number | null | undefined
  let output = textFromContent(result.content)
  if (value) {
    if (typeof value.exitCode === 'number') exitCode = value.exitCode
    else if (value.exitCode === null) exitCode = null
    const stdout = asRecord(value.stdout)
    const stderr = asRecord(value.stderr)
    const pieces: string[] = []
    if (stdout && typeof stdout.text === 'string') pieces.push(stdout.text)
    if (stderr && typeof stderr.text === 'string') pieces.push(stderr.text)
    if (pieces.length) output = pieces.join('\n')
  }
  if (exitCode === undefined) {
    // Fall back: parse "[exit code: N]" from rendered content; unknown if missing.
    const m = /\[exit code:\s*(-?\d+)\]/i.exec(output)
    exitCode = m ? Number(m[1]) : (result.isError ? 1 : 0)
  }
  return {
    op: 'receipt',
    session,
    kind: 'shell',
    tool_use_id: name,
    command,
    exit_code: exitCode,
    output,
    repo,
    tree_fp: treeFp,
  }
}

export function editReceiptFromArgs(
  session: string,
  name: string,
  args: unknown,
  repo?: string,
): Record<string, unknown> | null {
  const a = asRecord(args) || {}
  const command = String(a.command ?? '')
  // str_replace_editor view does not mutate
  if (command === 'view') return null
  const path = String(a.path ?? a.file_path ?? a.filePath ?? '')
  if (!path) return null
  return {
    op: 'receipt',
    session,
    kind: 'edit',
    tool_use_id: name,
    path,
    repo,
  }
}

export function assistantTextFromMessage(message: unknown): string {
  const m = asRecord(message)
  if (!m) return ''
  const content = m.content
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts: string[] = []
  for (const block of content) {
    const b = asRecord(block)
    if (!b) continue
    if (b.type === 'text' && typeof b.text === 'string') parts.push(b.text)
    else if (typeof b.text === 'string') parts.push(b.text)
  }
  return parts.join('\n')
}
