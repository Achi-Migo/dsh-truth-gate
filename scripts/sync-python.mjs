import { cpSync, mkdirSync, rmSync, writeFileSync, existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const pluginRoot = join(here, '..')
const src = process.env.OPC_TRUTH_SRC || 'E:\\cursor_ws\\0opc\\src\\opc_truth'
const dest = join(pluginRoot, 'python', 'opc_truth')

if (!existsSync(src)) {
  console.error(`sync-python: source missing: ${src}`)
  process.exit(1)
}

rmSync(dest, { recursive: true, force: true })
mkdirSync(dirname(dest), { recursive: true })
cpSync(src, dest, { recursive: true })
writeFileSync(
  join(pluginRoot, 'python', 'SOURCE.json'),
  JSON.stringify({
    source: src,
    syncedAt: new Date().toISOString(),
    note: 'Read-only copy of opc_truth for DSH bridge subprocess',
  }, null, 2),
)
console.log(`synced ${src} -> ${dest}`)
