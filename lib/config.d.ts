import z from '@deepseek-ai/schemastery';
export declare const Config: z<Schemastery.ObjectS<NoInfer<{
    mode: z<"off" | "shadow" | "gate", "off" | "shadow" | "gate", "defined">;
    python: z<string, string, "defined">;
    ledgerDir: z<string, string, "plain">;
    requireProtocol: z<boolean, boolean, "defined">;
    loopLimit: z<number, number, "defined">;
    timeoutMs: z<number, number, "defined">;
    treeFpOnShell: z<boolean, boolean, "defined">;
}>>, Schemastery.ObjectT<NoInfer<{
    mode: z<"off" | "shadow" | "gate", "off" | "shadow" | "gate", "defined">;
    python: z<string, string, "defined">;
    ledgerDir: z<string, string, "plain">;
    requireProtocol: z<boolean, boolean, "defined">;
    loopLimit: z<number, number, "defined">;
    timeoutMs: z<number, number, "defined">;
    treeFpOnShell: z<boolean, boolean, "defined">;
}>>, "plain">;
export type TruthGateConfig = {
    mode: 'off' | 'shadow' | 'gate';
    python: string;
    ledgerDir?: string;
    requireProtocol: boolean;
    loopLimit: number;
    timeoutMs: number;
    treeFpOnShell: boolean;
};
