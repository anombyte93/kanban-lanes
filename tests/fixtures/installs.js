// rule7 known-answer fixture: line numbers below are asserted by tests/test_lint.sh
export const meta = { name: 'installs', description: 'rule7 known-answer' }
const RULES = `Dependencies: hard-link copy the worktree's node_modules (cp -al), never a fresh install.`
const a = await agent(`Verify the PR. cd $DIR/app && npm ci && npm test`, { label: 'verify:a', model: 'opus', effort: 'xhigh' })
const b = await agent(`${RULES} Then npm install is not needed; run npm test.`, { label: 'verify:b', model: 'opus', effort: 'xhigh' })
const c = await agent(`Do not run npm install; the tree is prepared. Run npm test.`, { label: 'verify:c', model: 'opus', effort: 'xhigh' })
const d = await agent(`pnpm install --frozen-lockfile --offline --store-dir "$STORE" then pnpm test`, { label: 'verify:d', model: 'opus', effort: 'xhigh' })
const e = await agent(`Review step two: yarn install, then yarn test`, { label: 'review:e', model: 'opus', effort: 'xhigh' })
const f = await agent(`Verify in a scratch copy of the tree: run vitest and tsc --noEmit`, { label: 'verify:f', model: 'opus', effort: 'xhigh' })
const g = await agent(`${RULES} Run vitest in the copy.`, { label: 'verify:g', model: 'opus', effort: 'xhigh' })
return [a, b, c, d, e, f, g]
