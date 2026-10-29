import z from '@deepseek-ai/schemastery'

export const Config = z.object({
  mode: z.union(['off', 'shadow', 'gate']).default('shadow'),
  python: z.string().default('python'),
  ledgerDir: z.string(),
  requireProtocol: z.boolean().default(true),
  loopLimit: z.number().default(3),
  timeoutMs: z.number().default(15_000),
  treeFpOnShell: z.boolean().default(true),
})

export type TruthGateConfig = {
  mode: 'off' | 'shadow' | 'gate'
  python: string
  ledgerDir?: string
  requireProtocol: boolean
  loopLimit: number
  timeoutMs: number
  treeFpOnShell: boolean
}
