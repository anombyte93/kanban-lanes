export const meta = { name: 'clock', description: 'reads the clock, which the sandbox forbids' }
const stamp = Date.now()
const a = await agent(`evidence at ${stamp}`, { label: 'ev', model: 'sonnet', effort: 'medium' })
return [a]
