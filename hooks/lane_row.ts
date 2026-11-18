import type { AgentInfo, EngineInterface as Engine, Register } from 'claude-code'

// The agent-panel row of each hippo:lane agent (DESIGN §3.6 "The row"): its codex lane's
// progress — `codex · <scope> · <elapsed> · <cmds> cmds · <last event>` — or, for an agent
// relaying a whole batch, the batch's — `codex batch · <stem> · <elapsed> · <r> running ·
// <d>/<t> done · <f> failed · last: <scope>: <event>`.
//
// A `ui.render` hook on Claude Code's `AgentRow`, matched to `hippo:lane` rows alone: every other
// row never enters this module. Until the lane can be drawn — no id in the agent's transcript
// yet, no `.hippo/`, no record, a record that does not parse — the row is the engine's own,
// untouched. A lane relay may sit in one Bash call for minutes with no prop changing, so while a
// hippo:lane row is listed a 5s timer redraws this plugin's rows; it starts at the first lane
// row and stops at the first tick that finds none, so a session without lanes pays nothing.

const LANE_AGENT = 'hippo:lane'
// The lane agent's transcript names what it watches twice: the grep result (`dispatch:<id>`, or
// `batch:<id>` for a whole batch) and every watch call (`--watch <id>`). The last mention wins —
// the prompt, which comes first, is main's text and could quote an id of some other lane.
const ID_RE = /(?:dispatch:|batch:|--watch\s+)([db][0-9a-f]{32})\b/g
const SAFE_ID = /^[A-Za-z0-9_-]+$/
const SCOPE_CHARS = 48 // a lane's scope in a batch row's `last:`, as the wrapper cuts it
const EVERY_MS = 5000

type Rec = Record<string, any>

/** What a row needs from outside: the time, a record by id, whether a pid still runs. */
export type Io = {
  now: number // ms since the epoch
  record: (id: string) => Promise<Rec | undefined>
  alive: (pid: unknown) => Promise<boolean>
}

export function elapsed(seconds: number): string {
  const s = Math.max(0, Math.trunc(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.trunc(s / 60)}m${String(s % 60).padStart(2, '0')}s`
  return `${Math.trunc(s / 3600)}h${String(Math.trunc((s % 3600) / 60)).padStart(2, '0')}m`
}

const ISO_RE = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/

/** Seconds from `start` to `end` (else now); 0 when a stamp is not the CLI's `now_iso()`. */
export function secondsSince(start: unknown, end: unknown, now: number): number {
  if (typeof start !== 'string' || !ISO_RE.test(start)) return 0
  if (end && (typeof end !== 'string' || !ISO_RE.test(end))) return 0
  return ((end ? Date.parse(end as string) : now) - Date.parse(start)) / 1000
}

/** One line: runs of whitespace (newlines too) are one space, cut to `limit` characters. */
export function oneLine(text: unknown, limit: number): string {
  const s = [...String(text ?? '').trim().split(/\s+/).join(' ')]
  return s.length <= limit ? s.join('') : s.slice(0, limit - 1).join('') + '…'
}

function ending(rec: Rec): string {
  const how = rec.signal ? `killed by ${rec.signal}` : rec.status
  const rc = rec.rc == null ? '' : ` rc=${rec.rc}` // none: SIGKILLed before codex exited
  return `${how}${rc}`
}

/** The batch's newest event — its own, or a running lane's — as `<scope>: <event>`. Only the
 * lanes the batch record names as running are read: never every file in the directory, on a
 * redraw that comes every 5s. */
export async function batchLast(rec: Rec, io: Io): Promise<string> {
  let best = rec.last
  let at: string = rec.last_at || ''
  for (const id of rec.running_ids || []) {
    if (typeof id !== 'string' || !SAFE_ID.test(id)) continue
    const lane = await io.record(id)
    if (lane?.last && (lane.last_at || '') >= at) {
      best = `${oneLine(lane.scope, SCOPE_CHARS)}: ${lane.last}`
      at = lane.last_at || ''
    }
  }
  return best || 'no event yet'
}

export async function batchRow(rec: Rec, io: Io): Promise<string> {
  const head = `codex batch · ${rec.stem}`
  const counts = `${rec.running ?? 0} running · ${rec.done ?? 0}/${rec.todo ?? 0} done · ${rec.failed ?? 0} failed`
  const took = elapsed(secondsSince(rec.started, rec.ended || rec.last_at, io.now))
  if (rec.status) return `${head} · ${ending(rec)} · ${took} · ${counts}`
  if (!(await io.alive(rec.pid))) return `${head} · lost, its process is gone · ${took} · ${counts}`
  return `${head} · ${elapsed(secondsSince(rec.started, null, io.now))} · ${counts} · last: ${await batchLast(rec, io)}`
}

export async function laneRow(rec: Rec, io: Io): Promise<string> {
  if ('manifest' in rec) return batchRow(rec, io)
  const head = `codex · ${rec.scope}`
  const cmds = `${rec.cmds ?? 0} cmds`
  const last = rec.last || 'no event yet'
  const took = elapsed(secondsSince(rec.started, rec.ended || rec.last_at, io.now))
  if (rec.status) return `${head} · ${ending(rec)} · ${took} · ${cmds}`
  // A dead lane's timer must not keep counting.
  if (!(await io.alive(rec.pid))) return `${head} · lost, its wrapper is gone · ${took} · ${cmds} · ${last}`
  return `${head} · ${elapsed(secondsSince(rec.started, null, io.now))} · ${cmds} · ${last}`
}

/** The last lane or batch id the agent's transcript names, or undefined before it names one. */
export function dispatchId(transcript: string): string | undefined {
  let found: string | undefined
  for (const m of transcript.matchAll(ID_RE)) found = m[1]
  return found
}

async function isDir($: Engine, path: string): Promise<boolean> {
  return (await $.fs.stat(path).catch(() => undefined))?.kind === 'dir'
}

async function realPath($: Engine, path: string): Promise<string> {
  return (await $.fs.stat(path, { resolve: true }).catch(() => undefined))?.realPath ?? path
}

const parentOf = (d: string) => d.slice(0, d.lastIndexOf('/')) || '/'

/** `rel` against directory `base` (an absolute `rel` replaces it), `.` and `..` folded. */
function joinPath(base: string, rel: string): string {
  const parts: string[] = []
  for (const p of (rel.startsWith('/') ? rel : `${base}/${rel}`).split('/')) {
    if (p === '..') parts.pop()
    else if (p && p !== '.') parts.push(p)
  }
  return `/${parts.join('/')}`
}

/** `.hippo/` as the CLI's `find_hippo()` finds it, from `cwd` in place of the process's:
 * `$HIPPO_DIR` when it names a directory, else `walkHippo` from the resolved `cwd`. */
export async function findHippoFrom($: Engine, cwd: string): Promise<string | undefined> {
  const env = await $.env.get('HIPPO_DIR')
  if (env && (await isDir($, env))) return env
  const home = await $.env.get('HOME')
  return walkHippo($, await realPath($, cwd), home ? await realPath($, home) : undefined)
}

/** A port of cli/hippo_cli.py `walk_hippo` — the rule DESIGN §3.3 states and hooks/lib.sh
 * `project_root` also implements; the three must agree. A `.git` directory ends the walk; a
 * `.hippo/` at or below a `.git` file (a linked worktree) is a checked-out copy, never the state,
 * so the walk continues in the main worktree (a submodule's `.git` file, with no commondir,
 * continues upward); `$HOME` ends the walk unless it is `start` itself. */
async function walkHippo($: Engine, start: string, home: string | undefined): Promise<string | undefined> {
  let found: string | undefined
  for (let d = start; ; d = parentOf(d)) {
    if (d === home && d !== start) return found
    if (found === undefined && (await isDir($, `${d}/.hippo`))) found = `${d}/.hippo`
    const git = await $.fs.stat(`${d}/.git`).catch(() => undefined)
    if (git?.kind === 'dir') return found
    if (git) {
      const main = await mainWorktree($, d)
      if (main !== undefined) return (await isDir($, `${main}/.hippo`)) ? `${main}/.hippo` : undefined
      found = undefined
    }
    if (d === home || parentOf(d) === d) return found
  }
}

/** A port of `main_worktree`: the main worktree of the linked worktree rooted at `wt` (its
 * `.git` file → gitdir → commondir, which is `<main>/.git`), read from disk with no git spawn;
 * undefined for a submodule (no commondir) or a bare repository. */
async function mainWorktree($: Engine, wt: string): Promise<string | undefined> {
  try {
    const line = (await $.fs.read(`${wt}/.git`)).split('\n')[0] ?? ''
    if (!line.startsWith('gitdir:')) return undefined
    const gitdir = joinPath(wt, line.slice('gitdir:'.length).trim())
    const rel = (await $.fs.read(`${gitdir}/commondir`)).trim()
    const common = await realPath($, joinPath(gitdir, rel))
    return common.endsWith('/.git') ? parentOf(common) : undefined
  } catch {
    return undefined // unreadable, or no commondir: a submodule
  }
}

async function readRecord($: Engine, path: string): Promise<Rec | undefined> {
  try {
    const rec = JSON.parse(await $.fs.read(path))
    return rec && typeof rec === 'object' && !Array.isArray(rec) ? rec : undefined
  } catch {
    return undefined // missing, or not JSON: the caller draws nothing of it
  }
}

/** The wrapper's pid still runs — the check `hippo dispatch --watch` makes (watch_state). */
async function alive($: Engine, pid: unknown): Promise<boolean> {
  if (!Number.isInteger(pid) || (pid as number) <= 1) return false
  const r = await $.process.run(['kill', '-0', String(pid)])
  return r.exitCode === 0 || !/no such process/i.test(r.stderr) // EPERM: it runs, as another user
}

/** The row's text, or undefined where the engine's own row stands. */
async function rowBody($: Engine, props: { id: string; cwd: string }): Promise<string | undefined> {
  if (!SAFE_ID.test(props.id)) return undefined
  const { transcriptPath } = await $.session.info()
  if (!transcriptPath?.endsWith('.jsonl')) return undefined
  const transcript = `${transcriptPath.slice(0, -'.jsonl'.length)}/subagents/agent-${props.id}.jsonl`
  const id = dispatchId(await $.fs.read(transcript).catch(() => ''))
  if (!id) return undefined
  const hippo = await findHippoFrom($, props.cwd)
  if (!hippo) return undefined
  const lanes = `${hippo}/lanes`
  const rec = await readRecord($, `${lanes}/${id}.json`)
  if (!rec) return undefined
  return laneRow(rec, {
    now: await $.clock.now(),
    record: (other) => readRecord($, `${lanes}/${other}.json`),
    alive: (pid) => alive($, pid),
  })
}

let ticking: { cancel: () => void } | undefined

/** Redraw this plugin's rows every 5s while a hippo:lane row is listed; stop at the first tick
 * that finds none. The engine asks a row again only as its props change, and a relay waiting in
 * one `--watch` call changes none once its 16 token samples are equal: measured (2.1.327), the
 * row froze 80s into such a wait. A reload starts the module over and the engine stops its old
 * timers. */
function keepFresh($: Engine) {
  if (ticking) return
  $.ui.log('hippo: lane rows redrawn every 5s', { to: 'debug' })
  const timer = $.clock.every(EVERY_MS, () => {
    void $.agent.list().then((agents: AgentInfo[]) => {
      if (agents.some((a) => a.type === LANE_AGENT && a.hasRow)) return $.ui.invalidate('ui.render')
      timer.cancel()
      if (ticking === timer) ticking = undefined
      $.ui.log('hippo: no lane row left; redraws stopped', { to: 'debug' })
    },(err: unknown) => $.ui.log(`hippo: lane rows not redrawn this tick (${err})`, { to: 'debug' }))
  })
  ticking = timer
}

export const register: Register = (on) => {
  on('ui.render', { component: 'AgentRow', props: { type: LANE_AGENT } }, async ($, e, next) => {
    keepFresh($)
    let body: string | undefined
    try {
      body = await rowBody($, e.props)
    } catch (err) {
      // An ask superseded by a newer one is aborted mid-call (measured, 2.1.327): no failure.
      if (!next.signal.aborted) $.ui.log(`hippo: lane row ${e.props.id} left as drawn (${err})`, { to: 'debug' })
    }
    // The engine cuts the body at the row's edge with an ellipsis, wide characters counted
    // (measured, 2.1.327), so only line breaks are folded here.
    if (body === undefined) return next(e)
    return next({ ...e, props: { ...e.props, body: oneLine(body, Infinity) } })
  })
}
