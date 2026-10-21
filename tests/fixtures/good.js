// A routed script: every agent call names a model, or an agentType whose definition pins one.
export const meta = { name: 'good', description: 'routed' }
const a = await agent(`evidence (with parens) and 'quotes' ${x}`, { label: 'ev', model: 'sonnet', effort: 'medium', schema: S })
const b = await agent(`judge`, { label: 'judge:x', model: 'opus', effort: 'high' })
const c = await agent(`watch the cards`, { label: 'watch', agentType: 'kanban-watcher' })
return [a, b, c]
