/**
 * dsh-truth-gate — native Cordis plugin.
 *
 * Seams (TRUTH-E-DSH-SEAMS-20261029):
 * - agent/pre-step → baseline
 * - tools/post-execute → shell/edit receipts
 * - session/event → buffer assistant/message text per turn
 * - agent/turn-stopping → verify; gate mode may agent.steer()
 */
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { Config } from './config.js';
import { callBridge } from './engine-client.js';
import { assistantTextFromMessage, editReceiptFromArgs, isEditTool, isShellTool, shellReceiptFromResult, } from './receipts.js';
const require = createRequire(import.meta.url);
export const name = 'truth-gate';
export const inject = ['workingDirectory'];
export { Config };
function tryCreateUserMessage(text) {
    try {
        const mod = require('@deepseek-ai/dsh-llm');
        return mod.createUserMessage({
            content: [{ type: 'text', text }],
            source: { kind: 'dsh-truth-gate' },
        });
    }
    catch {
        return {
            role: 'user',
            content: [{ type: 'text', text }],
            source: { kind: 'dsh-truth-gate' },
        };
    }
}
export function apply(ctx, config) {
    const cfg = {
        mode: config.mode ?? 'shadow',
        python: config.python || 'python',
        ledgerDir: config.ledgerDir,
        requireProtocol: config.requireProtocol !== false,
        loopLimit: config.loopLimit ?? 3,
        timeoutMs: config.timeoutMs ?? 15_000,
        treeFpOnShell: config.treeFpOnShell !== false,
    };
    if (cfg.mode === 'off') {
        ctx.logger.info('[truth-gate] mode=off; idle');
        return;
    }
    const assistantBySession = new Map();
    const bounceBySession = new Map();
    ctx.logger.info(`[truth-gate] loaded mode=${cfg.mode}`);
    const sessionKey = (agent) => String(agent?.id ?? agent?.session?.id ?? 'unknown');
    const rememberAssistant = (agent, turn, text) => {
        if (!text.trim())
            return;
        const sid = sessionKey(agent);
        let turns = assistantBySession.get(sid);
        if (!turns) {
            turns = new Map();
            assistantBySession.set(sid, turns);
        }
        turns.set(turn, text);
    };
    const lastAssistant = (agent, turn) => {
        const sid = sessionKey(agent);
        const cached = assistantBySession.get(sid)?.get(turn);
        if (cached)
            return cached;
        // Deprecated fallback — documented in TRUTH-E-DSH-SEAMS
        try {
            const events = agent?.session?.snapshotEvents?.();
            if (!Array.isArray(events))
                return '';
            for (let i = events.length - 1; i >= 0; i--) {
                const ev = events[i];
                if (ev?.type === 'assistant/message' && ev?.data?.turn === turn) {
                    const text = assistantTextFromMessage(ev.data.message);
                    if (text)
                        return text;
                }
            }
        }
        catch (err) {
            ctx.logger.warn(`[truth-gate] snapshotEvents fallback failed: ${String(err)}`);
        }
        return '';
    };
    // Buffer assistant messages via session append feed when available
    ctx.on('session/event', (event) => {
        try {
            if (event?.type !== 'assistant/message')
                return;
            const turn = Number(event?.data?.turn);
            const text = assistantTextFromMessage(event?.data?.message);
            const agentLike = { id: event?.sessionId ?? event?.session_id, session: { id: event?.sessionId } };
            if (Number.isFinite(turn))
                rememberAssistant(agentLike, turn, text);
        }
        catch (err) {
            ctx.logger.warn(`[truth-gate] session/event handler: ${String(err)}`);
        }
    });
    ctx.on('agent/assistant-stream', ({ agent, frame }) => {
        try {
            if (frame?.kind !== 'end' || frame?.outcome?.kind !== 'committed')
                return;
            // Prefer accumulating from session; stream end is a hint only
        }
        catch (err) {
            ctx.logger.warn(`[truth-gate] assistant-stream: ${String(err)}`);
        }
    });
    ctx.on('agent/pre-step', async ({ agent, signal }, next) => {
        try {
            const cwd = await ctx.workingDirectory.ensure(agent, signal);
            const ledgerDir = cfg.ledgerDir || (cwd ? join(cwd, '.tools', 'truth') : undefined);
            await callBridge({
                op: 'baseline',
                session: sessionKey(agent),
                repo: cwd,
            }, { ...cfg, ledgerDir }, signal);
        }
        catch (err) {
            ctx.logger.warn(`[truth-gate] baseline failed: ${String(err)}`);
        }
        return next();
    });
    ctx.on('tools/post-execute', async (exec, result, next) => {
        const downstream = await next();
        try {
            const agent = exec?.agent;
            const cwd = agent
                ? (ctx.workingDirectory.get?.(agent.session) ?? await ctx.workingDirectory.ensure(agent, exec.signal).catch(() => undefined))
                : undefined;
            const ledgerDir = cfg.ledgerDir || (cwd ? join(cwd, '.tools', 'truth') : undefined);
            const session = sessionKey(agent ?? { id: 'no-agent' });
            const name = String(exec?.name || '');
            let msg = null;
            if (isShellTool(name)) {
                msg = shellReceiptFromResult(session, name, exec.arguments, result, cwd, cfg.treeFpOnShell);
            }
            else if (isEditTool(name) && !result?.isError) {
                msg = editReceiptFromArgs(session, name, exec.arguments, cwd);
            }
            if (msg) {
                const out = await callBridge(msg, { ...cfg, ledgerDir }, exec.signal);
                if (!out.ok)
                    ctx.logger.warn(`[truth-gate] receipt: ${out.error}`);
            }
        }
        catch (err) {
            ctx.logger.warn(`[truth-gate] post-execute: ${String(err)}`);
        }
        return downstream;
    });
    ctx.on('agent/turn-stopping', async ({ agent, turn, signal }) => {
        try {
            const cwd = await ctx.workingDirectory.ensure(agent, signal).catch(() => undefined);
            const ledgerDir = cfg.ledgerDir || (cwd ? join(cwd, '.tools', 'truth') : undefined);
            const sid = sessionKey(agent);
            const text = lastAssistant(agent, turn);
            if (!text.trim()) {
                ctx.logger.warn('[truth-gate] no assistant text at turn-stopping; skip verify');
                return;
            }
            const bounce = bounceBySession.get(sid) ?? 0;
            const out = await callBridge({
                op: 'verify',
                session: sid,
                repo: cwd,
                text,
                mode: cfg.mode,
                require_protocol: cfg.requireProtocol,
                bounce,
                loop_limit: cfg.loopLimit,
            }, { ...cfg, ledgerDir }, signal);
            if (!out.ok) {
                ctx.logger.warn(`[truth-gate] verify failed open: ${out.error}`);
                return;
            }
            if (cfg.mode === 'shadow') {
                ctx.logger.info(`[truth-gate] shadow engine_decision=${out.engine_decision}`);
                return;
            }
            if (cfg.mode === 'gate' && out.decision === 'block') {
                const nextBounce = bounce + 1;
                bounceBySession.set(sid, nextBounce);
                if (nextBounce > cfg.loopLimit) {
                    ctx.logger.warn(`[truth-gate] BLOCKED after ${cfg.loopLimit} bounces`);
                    bounceBySession.set(sid, 0);
                    return;
                }
                const followup = out.followup || '[TruthGate] claims failed verification; please correct.';
                agent.steer(tryCreateUserMessage(followup));
                return;
            }
            bounceBySession.set(sid, 0);
        }
        catch (err) {
            ctx.logger.warn(`[truth-gate] turn-stopping: ${String(err)}`);
        }
    });
}
export default { name, inject, Config, apply };
