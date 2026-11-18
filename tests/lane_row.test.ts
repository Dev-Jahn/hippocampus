import { expect, mock, test } from 'claude-code/testing'

// hooks/lane_row.ts through the engine's own AgentRow site, every `$` call it makes answered by a
// small fake machine: files, directories, live pids, the clock. Run by tests/test_lane_row.py.

const SESSION = '/home/u/.claude/projects/p/s1'
const ROW = 'alane'
const DID = 'd' + 'ab'.repeat(16)
const NOW = Date.parse('2026-10-02T02:00:15Z')

type Machine = { files: Record<string, string>; dirs: string[]; live: number[]; env?: Record<string, string> }

// Registers the fake machine beneath the plugin and returns what the module drew: the body each
// row was handed on with (undefined: left as the engine draws it), and the clock.
// `any`: the fakes answer only the fields the module reads.
function machine(on: any, m: Machine) {
  const missing = (path: string) => ({ deny: `ENOENT: ${path}` })
  const clock = mock.clock(on, { now: NOW })
  const env = m.env ?? { HOME: '/home/u' }
  on('env.get', ($: unknown, e: { name: string }) => ({ value: env[e.name] }))
  const drawn: (string | undefined)[] = []
  const lanes = { listed: true }
  on('ui.log', () => ({ value: undefined }))
  on('agent.list', () => ({ value: lanes.listed ? [{ id: ROW, type: 'hippo:lane', hasRow: true }] : [] }))
  on('session.info', () => ({ value: { transcriptPath: `${SESSION}.jsonl` } }))
  on('fs.read', ($: unknown, e: { path: string }) => (e.path in m.files ? { value: m.files[e.path] } : missing(e.path)))
  on('fs.stat', ($: unknown, e: { path: string }) => {
    const kind = m.dirs.includes(e.path) ? 'dir' : e.path in m.files ? 'file' : undefined
    return kind ? { value: { kind, size: 0, mtimeMs: 0, isLink: false, realPath: e.path } } : missing(e.path)
  })
  on('process.run', ($: unknown, e: { argv: string[] }) => {
    const isLive = m.live.includes(Number(e.argv[2]))
    return { value: { exitCode: isLive ? 0 : 1, stdout: '', stderr: isLive ? '' : 'kill: No such process',
      isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('ui.render', { component: 'AgentRow' }, ($: unknown, e: any, next: any) => { drawn.push(e.props.body); return next(e) })
  return { drawn, clock, lanes }
}

const PROPS = { id: ROW, description: 'pass2', type: 'hippo:lane', status: 'running', kind: 'local_agent',
  taskStatus: 'running', label: 'pass2', startTime: 0, cwd: '/w', tokenCount: 0, tokenSamples: [],
  hasRow: true, rowIds: [ROW], isHidden: false }

// The body the engine is handed for one row, or undefined where the row is left as it came.
async function body($: any, seen: { drawn: (string | undefined)[] }, props: Record<string, unknown>) {
  const ui = await $.ui.mount({ plugin: 'hippo', surface: 'terminal', component: 'AgentRow',
    props: { ...PROPS, ...props } as never })
  await ui.unmount()
  return seen.drawn.at(-1)
}

const transcript = (text: string): Record<string, string> => ({ [`${SESSION}/subagents/agent-${ROW}.jsonl`]: text })
const lane = (over: Record<string, unknown> = {}) => JSON.stringify({
  id: DID, scope: 'pass2 tensorize', pid: 4242, started: '2026-10-02T00:00:00Z', cmds: 14,
  last: 'exec: pytest -q tests/test_pass2.py', last_at: '2026-10-02T02:00:10Z', ...over })
const relayed = transcript(`{"prompt": "hippo dispatch …"}\n{"result": "dispatch:${DID}"}\n`)

test('a lane row shows its lane: scope, elapsed, commands, last event', async ($, on) => {
  const w = machine(on, { files: { ...relayed, [`/w/.hippo/lanes/${DID}.json`]: lane() }, dirs: ['/w', '/w/.hippo'], live: [4242] })
  expect(await body($, w, {})).toBe('codex · pass2 tensorize · 2h00m · 14 cmds · exec: pytest -q tests/test_pass2.py')
})

test('every other agent type is never entered', async ($, on) => {
  const w = machine(on, { files: { ...relayed, [`/w/.hippo/lanes/${DID}.json`]: lane() }, dirs: ['/w', '/w/.hippo'], live: [4242] })
  expect(await body($, w, { type: 'general-purpose' })).toBeUndefined()
})

test('the last id the agent named wins, the record is found walking up, an end is shown', async ($, on) => {
  const other = 'd' + 'cd'.repeat(16)
  const w = machine(on, {
    files: {
      ...transcript(`prompt quoting dispatch:${other}\n--watch ${DID}\n`),
      [`/w/.hippo/lanes/${DID}.json`]: lane({ status: 'killed', signal: 'SIGTERM', rc: -15, ended: '2026-10-02T00:03:05Z' }),
      [`/w/.hippo/lanes/${other}.json`]: lane({ scope: 'someone else' }),
    },
    dirs: ['/w', '/w/.hippo', '/w/deep', '/w/deep/er'],
    live: [4242],
  })
  expect(await body($, w, { cwd: '/w/deep/er' })).toBe('codex · pass2 tensorize · killed by SIGTERM rc=-15 · 3m05s · 14 cmds')
})

test('a lane whose wrapper died stops counting: no rc on record, or lost', async ($, on) => {
  const files = { ...relayed, [`/w/.hippo/lanes/${DID}.json`]: lane({ status: 'killed', signal: 'SIGTERM', last_at: '2026-10-02T00:00:16Z' }) }
  const w = machine(on, { files, dirs: ['/w', '/w/.hippo'], live: [] })
  expect(await body($, w, {})).toBe('codex · pass2 tensorize · killed by SIGTERM · 16s · 14 cmds')
  files[`/w/.hippo/lanes/${DID}.json`] = lane({ last_at: '2026-10-02T00:00:16Z' })
  expect(await body($, w, {})).toBe(
    'codex · pass2 tensorize · lost, its wrapper is gone · 16s · 14 cmds · exec: pytest -q tests/test_pass2.py')
})

test('until the lane can be drawn the row is the engine’s: no id, no .hippo/, no record, a torn record', async ($, on) => {
  const files: Record<string, string> = { ...transcript('{"prompt": "hippo dispatch …"}\n') }
  const dirs = ['/w']
  const w = machine(on, { files, dirs, live: [4242] })
  expect(await body($, w, {})).toBeUndefined() // no id yet
  Object.assign(files, relayed)
  expect(await body($, w, {})).toBeUndefined() // no .hippo/
  dirs.push('/w/.hippo')
  expect(await body($, w, {})).toBeUndefined() // no record
  files[`/w/.hippo/lanes/${DID}.json`] = '{"id": "d'
  expect(await body($, w, {})).toBeUndefined() // not JSON
  files[`/w/.hippo/lanes/${DID}.json`] = lane()
  expect(await body($, w, {})).toMatch(/^codex · pass2 tensorize · /)
})

test('.hippo/ is looked for as walk_hippo looks: a .git directory ends the walk, $HOME too unless it is the start', async ($, on) => {
  const files = { ...relayed, [`/s/.hippo/lanes/${DID}.json`]: lane() }
  const dirs = ['/s', '/s/.hippo', '/s/repo', '/s/repo/.git', '/s/repo/src', '/s/home', '/s/home/proj', '/s/plain']
  const env: Record<string, string> = { HOME: '/s/home' }
  const w = machine(on, { files, dirs, live: [4242], env })
  expect(await body($, w, { cwd: '/s/repo/src' })).toBeUndefined()
  expect(await body($, w, { cwd: '/s/home/proj' })).toBeUndefined()
  expect(await body($, w, { cwd: '/s/plain' })).toMatch(/^codex · pass2 tensorize · /)
  // ~/.hippo is the project of a session whose cwd is home, never of every directory under it.
  dirs.push('/s/home/.hippo')
  files[`/s/home/.hippo/lanes/${DID}.json`] = lane({ scope: 'home lane' })
  expect(await body($, w, { cwd: '/s/home' })).toMatch(/^codex · home lane · /)
  expect(await body($, w, { cwd: '/s/home/proj' })).toBeUndefined()
  // $HIPPO_DIR, when it is a directory, wins.
  env.HIPPO_DIR = '/elsewhere/.hippo'
  dirs.push('/elsewhere/.hippo')
  expect(await body($, w, { cwd: '/s/plain' })).toBeUndefined()
})

test('a .hippo/ in a linked worktree is a copy: the main worktree’s is the state', async ($, on) => {
  const files: Record<string, string> = {
    ...relayed,
    [`/m/.hippo/lanes/${DID}.json`]: lane({ scope: 'the real lane' }),
    [`/m/.claude/worktrees/w/.hippo/lanes/${DID}.json`]: lane({ scope: 'a checked-out copy' }),
    '/m/.claude/worktrees/w/.git': 'gitdir: /m/.git/worktrees/w\n',
    '/m/.git/worktrees/w/commondir': '../..\n',
    '/away/wt/.git': 'gitdir: ../../m/.git/worktrees/away\n', // a relative gitdir, outside the repo
    '/m/.git/worktrees/away/commondir': '../..\n',
    '/s/sub/.git': 'gitdir: ../.git/modules/sub\n', // a submodule: no commondir
    [`/s/sub/.hippo/lanes/${DID}.json`]: lane({ scope: 'submodule copy' }),
    [`/s/.hippo/lanes/${DID}.json`]: lane({ scope: 'above the submodule' }),
  }
  const dirs = ['/m', '/m/.git', '/m/.hippo', '/m/.claude', '/m/.claude/worktrees', '/m/.claude/worktrees/w',
    '/m/.claude/worktrees/w/.hippo', '/m/.claude/worktrees/w/src', '/away', '/away/wt', '/s', '/s/.hippo', '/s/sub',
    '/s/sub/.hippo']
  const w = machine(on, { files, dirs, live: [4242] })
  expect(await body($, w, { cwd: '/m/.claude/worktrees/w/src' })).toMatch(/^codex · the real lane · /)
  expect(await body($, w, { cwd: '/m/.claude/worktrees/w' })).toMatch(/^codex · the real lane · /)
  expect(await body($, w, { cwd: '/away/wt' })).toMatch(/^codex · the real lane · /)
  expect(await body($, w, { cwd: '/s/sub' })).toMatch(/^codex · above the submodule · /)
  // A main worktree without .hippo/: the copy is still never the state.
  dirs.splice(dirs.indexOf('/m/.hippo'), 1)
  expect(await body($, w, { cwd: '/m/.claude/worktrees/w/src' })).toBeUndefined()
})

const BID = 'b' + '12'.repeat(16)
const DECOY = 'd' + 'cd'.repeat(16)
const batch = (over: Record<string, unknown> = {}) => JSON.stringify({
  id: BID, manifest: '/w/wide.yaml', stem: 'wide', pid: 4242, started: '2026-10-02T00:00:00Z', total: 3,
  todo: 3, skipped: 0, launched: 2, running: 1, ok: 1, failed: 0, done: 1, running_ids: [DID],
  last: 'lane a: exited rc=0', last_at: '2026-01-01T00:00:00Z', journal: '/w/wide.journal.jsonl', harvest: null, ...over })

test('a batch row reads the batch and only the lanes it names as running', async ($, on) => {
  const files = {
    ...transcript(`hippo dispatch --batch wide.yaml\nbatch:${BID}\n--watch ${BID}\n`),
    [`/w/.hippo/lanes/${BID}.json`]: batch(),
    [`/w/.hippo/lanes/${DID}.json`]: lane({ cmds: 4, last: 'exec: pytest -q', last_at: '2026-10-02T02:00:00Z' }),
    // Newer still, but not one of the batch's running lanes: never read.
    [`/w/.hippo/lanes/${DECOY}.json`]: lane({ id: DECOY, scope: 'someone else', last: 'exec: rm', last_at: '2099-01-01T00:00:00Z' }),
  }
  const w = machine(on, { files, dirs: ['/w', '/w/.hippo'], live: [4242] })
  expect(await body($, w, {})).toBe(
    'codex batch · wide · 2h00m · 1 running · 1/3 done · 0 failed · last: pass2 tensorize: exec: pytest -q')
  files[`/w/.hippo/lanes/${BID}.json`] = batch({ last_at: '2026-10-02T02:00:09Z' })
  expect(await body($, w, {})).toBe(
    'codex batch · wide · 2h00m · 1 running · 1/3 done · 0 failed · last: lane a: exited rc=0')
  files[`/w/.hippo/lanes/${BID}.json`] = batch({ running: 0, running_ids: [], done: 3, ok: 2, failed: 1,
    status: 'exited', rc: 1, ended: '2026-10-02T00:12:03Z' })
  expect(await body($, w, {})).toBe('codex batch · wide · exited rc=1 · 12m03s · 0 running · 3/3 done · 1 failed')
})

test('a batch says how it ended: lost with no end on record, killed by a signal', async ($, on) => {
  const files = { ...transcript(`batch:${BID}\n`), [`/w/.hippo/lanes/${BID}.json`]: batch({ pid: 0, todo: 4, running: 2, last_at: '2026-10-02T00:01:00Z' }) }
  const w = machine(on, { files, dirs: ['/w', '/w/.hippo'], live: [] })
  const counts = '2 running · 1/4 done · 0 failed'
  expect(await body($, w, {})).toBe(`codex batch · wide · lost, its process is gone · 1m00s · ${counts}`)
  files[`/w/.hippo/lanes/${BID}.json`] = batch({ todo: 4, running: 2, status: 'killed', signal: 'SIGTERM', rc: 143,
    ended: '2026-10-02T00:02:00Z' })
  expect(await body($, w, {})).toBe(`codex batch · wide · killed by SIGTERM rc=143 · 2m00s · ${counts}`)
})

test('a line break in the record never makes the row taller', async ($, on) => {
  const w = machine(on, { files: { ...relayed, [`/w/.hippo/lanes/${DID}.json`]: lane({ last: 'said: one\ntwo' }) }, dirs: ['/w', '/w/.hippo'], live: [4242] })
  expect(await body($, w, {})).toBe('codex · pass2 tensorize · 2h00m · 14 cmds · said: one two')
})

test('rows are redrawn every 5s while a lane row is listed, and no longer once none is', async ($, on) => {
  const files = { ...relayed, [`/w/.hippo/lanes/${DID}.json`]: lane() }
  const w = machine(on, { files, dirs: ['/w', '/w/.hippo'], live: [4242] })
  const ui = await $.ui.mount({ plugin: 'hippo', surface: 'terminal', component: 'AgentRow', props: PROPS as never })
  expect(w.drawn.at(-1)).toBe('codex · pass2 tensorize · 2h00m · 14 cmds · exec: pytest -q tests/test_pass2.py')
  // Nothing in the props changes; the record does, and the time passes.
  files[`/w/.hippo/lanes/${DID}.json`] = lane({ cmds: 15, last: 'exec: ruff check .' })
  await w.clock.advance(5000)
  expect(w.drawn.at(-1)).toBe('codex · pass2 tensorize · 2h00m · 15 cmds · exec: ruff check .')
  w.lanes.listed = false
  await w.clock.advance(5000) // this tick finds no lane row and stops
  const asked = w.drawn.length
  await w.clock.advance(60000)
  expect(w.drawn.length).toBe(asked)
  await ui.unmount()
})
