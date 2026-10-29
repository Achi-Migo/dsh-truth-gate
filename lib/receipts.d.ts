/** Map DSH tool executions to opc_truth bridge receipt messages. */
export declare function isShellTool(name: string): boolean;
export declare function isEditTool(name: string): boolean;
export declare function shellReceiptFromResult(session: string, name: string, args: unknown, result: {
    isError: boolean;
    value?: unknown;
    content?: unknown;
}, repo?: string, treeFp?: boolean): Record<string, unknown> | null;
export declare function editReceiptFromArgs(session: string, name: string, args: unknown, repo?: string): Record<string, unknown> | null;
export declare function assistantTextFromMessage(message: unknown): string;
