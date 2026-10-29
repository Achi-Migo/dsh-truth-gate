import { spawn } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const pluginRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
const pythonRoot = join(pluginRoot, 'python');
export async function callBridge(message, cfg, signal) {
    const env = {
        ...process.env,
        PYTHONPATH: pythonRoot + (process.env.PYTHONPATH ? `${process.platform === 'win32' ? ';' : ':'}${process.env.PYTHONPATH}` : ''),
    };
    if (cfg.ledgerDir)
        env.OPC_TRUTH_HOME = cfg.ledgerDir;
    return await new Promise((resolve) => {
        let settled = false;
        const finish = (value) => {
            if (settled)
                return;
            settled = true;
            resolve(value);
        };
        const child = spawn(cfg.python, ['-m', 'opc_truth', 'bridge'], {
            env,
            windowsHide: true,
            stdio: ['pipe', 'pipe', 'pipe'],
        });
        const timer = setTimeout(() => {
            child.kill();
            finish({ ok: false, decision: 'pass', error: `bridge timeout ${cfg.timeoutMs}ms` });
        }, cfg.timeoutMs);
        if (signal) {
            const onAbort = () => {
                child.kill();
                finish({ ok: false, decision: 'pass', error: 'bridge aborted' });
            };
            if (signal.aborted)
                onAbort();
            else
                signal.addEventListener('abort', onAbort, { once: true });
        }
        let stdout = '';
        let stderr = '';
        child.stdout.setEncoding('utf8');
        child.stderr.setEncoding('utf8');
        child.stdout.on('data', (chunk) => { stdout += chunk; });
        child.stderr.on('data', (chunk) => { stderr += chunk; });
        child.on('error', (err) => {
            clearTimeout(timer);
            finish({ ok: false, decision: 'pass', error: `spawn: ${err.message}` });
        });
        child.on('close', () => {
            clearTimeout(timer);
            try {
                const parsed = JSON.parse(stdout || '{}');
                if (!parsed.ok && !parsed.error && stderr)
                    parsed.error = stderr.slice(0, 500);
                finish(parsed);
            }
            catch (err) {
                finish({
                    ok: false,
                    decision: 'pass',
                    error: `bad bridge stdout: ${String(err)}; stderr=${stderr.slice(0, 300)}`,
                });
            }
        });
        child.stdin.write(JSON.stringify(message), 'utf8');
        child.stdin.end();
    });
}
