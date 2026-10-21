// A routed script whose prompt holds an unescaped backtick, so it does not parse.
export const meta = { name: 'unparseable', description: 'routed, but a prompt holds an unescaped backtick' }
const a = await agent(`open a PR whose base is `feat/x` (never main)`, { label: 'build', model: 'opus', effort: 'high' })
return [a]
