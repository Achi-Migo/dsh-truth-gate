import type { TruthGateConfig } from './config.js';
export type BridgeResult = {
    ok: boolean;
    decision?: 'pass' | 'block';
    engine_decision?: 'pass' | 'block';
    followup?: string;
    error?: string;
    report?: unknown;
    [k: string]: unknown;
};
export declare function callBridge(message: Record<string, unknown>, cfg: TruthGateConfig, signal?: AbortSignal): Promise<BridgeResult>;
