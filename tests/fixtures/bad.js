export const meta = { name: 'bad', description: 'no routing' }
const a = await agent(`evidence (with parens) and 'quotes'`, { label: 'ev', schema: S })
const b = await agent(`judge`, { label: 'judge:x', model: 'fable' })
const c = await agent(`verify`, { label: 'verify:y', model: 'fable' })
return [a, b, c]
