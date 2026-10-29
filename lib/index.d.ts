import { Config, type TruthGateConfig } from './config.js';
export declare const name = "truth-gate";
export declare const inject: string[];
export { Config };
type AnyCtx = {
    on: (event: string, listener: (...args: any[]) => any) => () => void;
    logger: {
        info: (m: string) => void;
        warn: (m: string) => void;
        debug?: (m: string) => void;
    };
    workingDirectory: {
        ensure: (agent: any, signal?: AbortSignal) => Promise<string | undefined>;
        get?: (session: any) => string | undefined;
    };
};
export declare function apply(ctx: AnyCtx, config: TruthGateConfig): void;
declare const _default: {
    name: string;
    inject: string[];
    Config: import("@deepseek-ai/schemastery").default<Schemastery.ObjectS<NoInfer<{
        mode: import("@deepseek-ai/schemastery").default<"off" | "shadow" | "gate", "off" | "shadow" | "gate", "defined">;
        python: import("@deepseek-ai/schemastery").default<string, string, "defined">;
        ledgerDir: import("@deepseek-ai/schemastery").default<string, string, "plain">;
        requireProtocol: import("@deepseek-ai/schemastery").default<boolean, boolean, "defined">;
        loopLimit: import("@deepseek-ai/schemastery").default<number, number, "defined">;
        timeoutMs: import("@deepseek-ai/schemastery").default<number, number, "defined">;
        treeFpOnShell: import("@deepseek-ai/schemastery").default<boolean, boolean, "defined">;
    }>>, Schemastery.ObjectT<NoInfer<{
        mode: import("@deepseek-ai/schemastery").default<"off" | "shadow" | "gate", "off" | "shadow" | "gate", "defined">;
        python: import("@deepseek-ai/schemastery").default<string, string, "defined">;
        ledgerDir: import("@deepseek-ai/schemastery").default<string, string, "plain">;
        requireProtocol: import("@deepseek-ai/schemastery").default<boolean, boolean, "defined">;
        loopLimit: import("@deepseek-ai/schemastery").default<number, number, "defined">;
        timeoutMs: import("@deepseek-ai/schemastery").default<number, number, "defined">;
        treeFpOnShell: import("@deepseek-ai/schemastery").default<boolean, boolean, "defined">;
    }>>, "plain">;
    apply: typeof apply;
};
export default _default;
