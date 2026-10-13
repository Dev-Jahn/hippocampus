import type { Register } from 'claude-code'

// The hippo:lane relay without a model (Claude Code mods; DESIGN §3.6 "The lane agent").
//
// agents/lane.md is a sonnet relay: it launches one `hippo dispatch …` command in the background,
// greps the lane's id, loops `hippo dispatch --watch <id>` and replies with its final lines. This
// module answers that agent's model requests itself, so the relay makes none, and draws the
// lane's codex items as the agent's own transcript: each command codex runs is a tool row (input
// the command, result its output), each message codex writes is assistant text. Opening the agent
// from the agent selector shows what codex is doing.
//
// The relay's Bash calls stay real and stay lane.md's: the background launch (so x on the row
// still stops the lane and the wrapper's kill trap records it), the id grep (so the status line
// finds the lane in the transcript) and the final `--watch` (whose output is the reply,
// verbatim). The codex rows are calls to `mcp__hippo__codex`, a tool this module registers and
// answers from `.hippo/lanes/<id>.events.jsonl` (the wrapper's reading of codex's log, LaneItems
// in cli/hippo_cli.py), drawn as `Bash(<command>)` / `Update(<files>)`: nothing runs, nothing
// reaches a permission check. A whole batch (`batch:<id>`) shows its progress line once a minute.
//
// Anything off the expected path — a prompt that is no `hippo dispatch` command, no output file,
// no id, no lane record, a watch that fails, an error here — hands the agent back to its model,
// which carries on with lane.md as written. Without mods (Codex host, --safe-mode,
// disableAllHooks, Claude Code's hooks-module rollout switched off) none of this loads and
// lane.md runs exactly as before.

const AGENT = 'hippo:lane'
const TOOL = 'codex'
const TOOL_NAME = 'mcp__hippo__codex' // the plugin is `hippo`
const LAUNCH_TIMEOUT = 2147483647 // lane.md step 1: the host clamps it to its longest deadline
const WATCH_TIMEOUT = 600000
const BATCH_WATCH_FOR = 60 // seconds per batch progress row
const ID_WAIT_SECONDS = 60 // how long the launch may take to print its id
const LIVENESS_EVERY = 5 // seconds between record checks while codex is quiet
const ID_RE = /(batch:b|dispatch:d)[0-9a-f]{32}/
const OUTPUT_RE = /Output is being written to: (\S+?)\.?(?:\s|$)/

type Item =
  | { k: 'exec'; n: number; cmd: string; cwd?: string }
  | { k: 'done'; n: number | null; rc: number | null; ms: number; out: string; lines: number }
  | { k: 'said' | 'thinking' | 'patch'; text: string }
  | { k: 'end'; status: string; rc: number | null }
  | { k: 'unreadable' }

type Phase = 'launch' | 'id' | 'grep' | 'follow' | 'watch' | 'model'

type Relay = {
  command: string
  description: string
  cwd?: string
  phase: Phase
  output?: string // the background shell's output file
  id?: string // `dispatch:d…` or `batch:b…`, as printed
  record?: string // .hippo/lanes/<id>.json
  items: Item[] // <id>.events.jsonl, by line
  dones: Map<number, Item & { k: 'done' }>
  shown: number // items[0, shown) are drawn
  over: boolean // the wrapper is done: its `end` item, or its record says so
  rows: number
  last?: { text: string; isError: boolean } // the result of the relay's own last Bash call
}

const relays = new Map<string, Relay>()

export function isDispatchCommand(prompt: string): boolean {
  return /^hippo dispatch\s/.test(prompt.trim())
}

export function parseItem(line: string): Item {
  try {
    const item = JSON.parse(line)
    return item && typeof item.k === 'string' ? item : { k: 'unreadable' }
  } catch {
    return { k: 'unreadable' }
  }
}

/** What Bash would have shown for the command: its output, `Exit code N` first on a failure. */
export function doneText(done: Item & { k: 'done' }): string {
  const out = done.out.trimEnd() || '(No output)'
  return done.rc ? `Exit code ${done.rc}\n${out}` : out
}

function newRelay(command: string, description: string, cwd?: string): Relay {
  return { command, description, cwd, phase: 'launch', items: [], dones: new Map(), shown: 0, over: false, rows: 0 }
}

function quote(s: string): string {
  return `'${s.replaceAll("'", `'\\''`)}'`
}

// One step's response, streamed as the engine expects a model's.
function answered(e, text: string, toolUses: { name: string; input: unknown }[]) {
  return {
    turnId: e.turnId,
    index: e.index,
    answer: text,
    toolUses,
    stopReason: toolUses.length ? 'tool_use' : 'end_turn',
    usage: null,
  } as const
}

async function* reply(e, text: string) {
  yield { kind: 'text', index: 0, text } as const
  yield { kind: 'stop', stopReason: 'end_turn', usage: null } as const
  return answered(e, text, [])
}

const rowId = (e, relay: Relay) => `toolu_hippo_lane_${String(e.turnId).replace(/[^A-Za-z0-9_-]/g, '').slice(-12)}_${relay.rows++}`

async function* call(e, relay: Relay, block: number, text: string, name: string, input: Record<string, unknown>) {
  yield { kind: 'tool', index: block, id: rowId(e, relay), name } as const
  yield { kind: 'input', index: block, json: JSON.stringify(input) } as const
  yield { kind: 'stop', stopReason: 'tool_use', usage: null } as const
  return answered(e, text, [{ name, input }])
}

const watchCall = (relay: Relay) => ({
  command: `hippo dispatch --watch ${relay.id!.slice(relay.id!.indexOf(':') + 1)}${relay.id!.startsWith('batch:') ? ` --for ${BATCH_WATCH_FOR}` : ''}`,
  description: relay.id!.startsWith('batch:') ? 'Watch the codex batch' : 'Collect the lane\'s final lines',
  timeout: WATCH_TIMEOUT,
})

// The lane record, found as hippo finds `.hippo/`: walking up from where the agent runs.
async function findRecord($, relay: Relay): Promise<string | undefined> {
  const hex = relay.id!.slice(relay.id!.indexOf(':') + 1)
  let dir = relay.cwd ?? (await $.session.cwd())
  for (;;) {
    const path = `${dir}/.hippo/lanes/${hex}.json`
    if (await $.fs.exists(path)) return path
    const up = dir.slice(0, dir.lastIndexOf('/'))
    if (!up || up === dir) return undefined
    dir = up
  }
}

// The wrapper is done when its record says so, or when it is gone (`hippo dispatch --watch`'s
// watch_state: `ended`, else a live pid, else a recorded status).
async function wrapperDone($, relay: Relay): Promise<boolean> {
  let rec
  try {
    rec = JSON.parse(await $.fs.read(relay.record!))
  } catch {
    return false // mid-replace: the next check reads it
  }
  if (rec.ended || rec.status) return true
  if (typeof rec.pid !== 'number') return false
  return (await $.process.run(['kill', '-0', String(rec.pid)])).exitCode !== 0
}

function take(relay: Relay, line: number, text: string) {
  if (relay.items[line]) return
  const item = parseItem(text)
  relay.items[line] = item
  if (item.k === 'done' && item.n != null) relay.dones.set(item.n, item)
  if (item.k === 'end') relay.over = true
}

const TICK = Symbol('tick')

// Reads the lane's items from where this relay has read to, until `enough()` or the wrapper is
// done. Every wait here is a `$` call — a `tail -F` piece or a `sleep` — so none of it is the
// hook's own time. Readers that overlap (two codex rows at once) each read the same lines and
// keep the first copy of each.
async function follow($, relay: Relay, enough: () => boolean, signal?: AbortSignal) {
  const path = relay.record!.replace(/\.json$/, '.events.jsonl')
  const pause = () => $.process.run(['sleep', String(LIVENESS_EVERY)]).then(() => TICK)
  let tick = pause()
  while (!enough() && !signal?.aborted) {
    let line = relay.items.length
    const tail = $.process.spawn({ argv: ['tail', '-n', `+${line + 1}`, '-F', path] })[Symbol.asyncIterator]()
    let piece = tail.next()
    let buf = ''
    let done = false
    try {
      while (!enough() && !signal?.aborted) {
        const got = await Promise.race([piece, tick])
        if (got === TICK) {
          tick = pause()
          if ((done = await wrapperDone($, relay))) break
          continue
        }
        if (got.done) break // tail itself ended: start it again where this left off
        if (got.value.stream === 'stdout') {
          buf += got.value.text
          for (let nl = buf.indexOf('\n'); nl >= 0; nl = buf.indexOf('\n')) {
            take(relay, line++, buf.slice(0, nl))
            buf = buf.slice(nl + 1)
          }
        }
        piece = tail.next()
      }
    } finally {
      await tail.return?.()
    }
    if (done) {
      // The wrapper is done: what it wrote after the last piece read is read whole, once.
      const rest = await $.process.run(['tail', '-n', `+${relay.items.length + 1}`, path])
      for (const text of rest.stdout.split('\n').slice(0, -1)) take(relay, relay.items.length, text)
      relay.over = true
    }
  }
}

// One step of a relay that follows its lane: codex's messages stream as text while it works;
// the step ends on its next command(s) — one tool row each — or, once the lane is over, on the
// final `--watch`.
async function* step($, e, relay: Relay, signal: AbortSignal) {
  let block = -1
  let text = ''
  for (;;) {
    await follow($, relay, () => relay.shown < relay.items.length || relay.over, signal)
    if (signal.aborted) throw new Error('stopped')
    while (relay.shown < relay.items.length) {
      const item = relay.items[relay.shown]
      if (item.k === 'said' || item.k === 'thinking') {
        relay.shown++
        block++
        if (item.k === 'said') text += (text ? '\n\n' : '') + item.text
        yield { kind: item.k === 'said' ? 'text' : 'thinking', index: block, text: item.text } as const
        continue
      }
      if (item.k === 'exec' || item.k === 'patch') {
        // Commands codex started together are rows of one step, as parallel tool calls are.
        const uses: { name: string; input: Record<string, unknown> }[] = []
        for (; relay.shown < relay.items.length; relay.shown++) {
          const it = relay.items[relay.shown]
          if (it.k === 'done' || it.k === 'unreadable') continue
          if (it.k !== 'exec' && it.k !== 'patch') break
          const input = it.k === 'exec'
            ? { command: it.cmd, ...(it.cwd ? { cwd: it.cwd } : {}), item: relay.shown }
            : { patch: patchFiles(it.text), item: relay.shown }
          block++
          yield { kind: 'tool', index: block, id: rowId(e, relay), name: TOOL_NAME } as const
          yield { kind: 'input', index: block, json: JSON.stringify(input) } as const
          uses.push({ name: TOOL_NAME, input })
        }
        yield { kind: 'stop', stopReason: 'tool_use', usage: null } as const
        return answered(e, text, uses)
      }
      relay.shown++ // `done` (its row answers it), `end`, an unreadable line
    }
    if (relay.over) {
      relay.phase = 'watch'
      return yield* call(e, relay, block + 1, text, 'Bash', watchCall(relay))
    }
  }
}

// A command as a Bash row shows it: its first two lines, the rest elided.
export function shortCommand(command: string): string {
  const lines = command.split('\n')
  return lines.length > 2 ? `${lines.slice(0, 2).join('\n')} …` : command
}

function patchFiles(text: string): string {
  const files = text.split('\n').filter((l) => l.startsWith('/'))
  const names = files.map((f) => f.slice(f.lastIndexOf('/') + 1))
  return names.length ? names.join(', ') : 'patch'
}

async function* drive($, e, relay: Relay, signal: AbortSignal) {
  if (relay.phase === 'launch') {
    if (!isDispatchCommand(relay.command)) return undefined // the model reads the prompt
    relay.phase = 'id'
    return yield* call(e, relay, 0, '', 'Bash', {
      command: relay.command.trim(),
      description: relay.description || 'Run the codex lane',
      run_in_background: true,
      timeout: LAUNCH_TIMEOUT,
    })
  }
  if (relay.phase === 'id') {
    const out = OUTPUT_RE.exec(relay.last?.text ?? '')?.[1]
    if (!out) return undefined // not in the background: lane.md has the rest
    relay.output = out
    // The launch prints its id within a second or two; the grep then reads it the first time.
    await $.process.run(['sh', '-c', `i=0; while [ $i -lt ${ID_WAIT_SECONDS * 5} ]; do grep -qE '${ID_RE.source}' "$1" && exit 0; i=$((i+1)); sleep 0.2; done; exit 1`, 'sh', out], {
      timeoutMs: (ID_WAIT_SECONDS + 10) * 1000,
    })
    relay.phase = 'grep'
    return yield* call(e, relay, 0, '', 'Bash', {
      command: `grep -m1 -oE '${ID_RE.source}' ${quote(out)}`,
      description: 'Find the lane id',
    })
  }
  if (relay.phase === 'grep') {
    const id = ID_RE.exec(relay.last?.text ?? '')?.[0]
    if (!id) return undefined
    relay.id = id
    relay.record = await findRecord($, relay)
    if (!relay.record) return undefined // no `.hippo/`: lane.md waits on the command itself
    if (id.startsWith('batch:')) {
      relay.phase = 'watch'
      return yield* call(e, relay, 0, '', 'Bash', watchCall(relay))
    }
    relay.phase = 'follow'
  }
  if (relay.phase === 'follow') return yield* step($, e, relay, signal)
  if (relay.phase === 'watch') {
    const last = relay.last
    if (last && !last.isError) {
      relays.delete(e.agentId) // done: a later message to this agent is its model's
      return yield* reply(e, last.text.replace(/\n+$/, ''))
    }
    // Exit 3: the window passed with the lane (or batch) still running — the same call again.
    if (last && /^Exit code 3\b/.test(last.text)) return yield* call(e, relay, 0, '', 'Bash', watchCall(relay))
    return undefined
  }
  return undefined
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await $.tool.register({
      name: TOOL,
      description:
        'One command a codex lane ran, with its output, drawn inside a hippo:lane relay. It runs nothing; never call it.',
      inputSchema: {
        type: 'object',
        properties: {
          command: { type: 'string' },
          cwd: { type: 'string' },
          patch: { type: 'string' },
          item: { type: 'integer' },
        },
        required: ['item'],
      },
      isDeferred: true,
    })
    return started
  })

  on('agent.spawn', { subagentType: AGENT }, async ($, e, next) => {
    const started = await next(e)
    if (started.agentId) relays.set(started.agentId, newRelay(e.prompt, e.description, e.cwd))
    return started
  })

  // Only a subagent's requests reach this hook (the matcher is checked before the plugin is
  // entered: main's never do); of those, only a hippo:lane relay's are answered.
  on('turn.step', { agentId: /./ }, async function* ($, e, next) {
    const relay = relays.get(e.agentId!)
    if (!relay || relay.phase === 'model') return yield* next(e)
    let yielded = false
    try {
      const it = drive($, e, relay, next.signal)
      for (let r = await it.next(); ; r = await it.next()) {
        if (r.done) {
          if (r.value) return r.value
          break
        }
        yielded = true
        yield r.value
      }
    } catch (err) {
      relay.phase = 'model'
      // Stopped (x on the row, Esc): the agent ends here, and no model is asked for anything.
      if (next.signal.aborted) return answered(e, '', [])
      $.ui.log(`hippo: the lane relay's model takes over (${err})`, { to: 'debug' })
      if (yielded) {
        // Half a response is out: end it on a watch call, which lane.md's loop knows how to read.
        if (!relay.id) throw err
        yield { kind: 'tool', index: 99, id: rowId(e, relay), name: 'Bash' } as const
        yield { kind: 'input', index: 99, json: JSON.stringify(watchCall(relay)) } as const
        yield { kind: 'stop', stopReason: 'tool_use', usage: null } as const
        return answered(e, '', [{ name: 'Bash', input: watchCall(relay) }])
      }
    }
    relay.phase = 'model'
    return yield* next(e)
  })

  // The relay's own Bash calls (launch, grep, watch) carry this module's ids, so they alone reach
  // this hook: each result is what the relay's next step reads. (`$.session.messages` would
  // copy the whole transcript per step, and serves no Workflow agent's.)
  on('tool.call', { tool: 'Bash', tool_use_id: /^toolu_hippo_lane_/ }, async ($, e, next) => {
    const result = await next(e)
    const relay = e.agentId ? relays.get(e.agentId) : undefined
    if (relay) relay.last = { text: result.deny ?? result.text ?? '', isError: !!(result.deny || result.isError) }
    return result
  })

  on('tool.call', { tool: TOOL_NAME }, async ($, e) => {
    const relay = e.agentId ? relays.get(e.agentId) : undefined
    const item = relay?.items[(e as { item?: number }).item ?? -1]
    if (!relay?.record || !item) return { deny: 'This tool only draws a codex lane inside a hippo:lane relay; nothing ran.' }
    if (item.k === 'patch') return { result: item.text }
    if (item.k !== 'exec') return { deny: 'No codex command at that item.' }
    await follow($, relay, () => relay.dones.has(item.n) || relay.over)
    const done = relay.dones.get(item.n)
    if (!done) return { result: 'No result: the lane ended before codex printed one.' }
    return done.rc ? { isError: true, result: doneText(done) } : { result: doneText(done) }
  })

  // A codex row is drawn as its native twin: a command as `⏺ Bash(<command>)`, a patch as
  // `⏺ Update(<files>)`, the bullet coloured as the engine colours it. Only the drawing changes;
  // the transcript keeps the mcp__hippo__codex call. (Rewriting the props' `tool` to `Bash`
  // keeps the generic `name(key: "value")` row; this tree is the one way to the native look.)
  on('ui.render', { component: 'ToolUse', props: { tool: TOOL_NAME } }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const { command, patch } = e.props.input as { command?: string; patch?: string }
    const bullet = e.props.isErrored ? 'error' : e.props.isRunning ? 'inactive' : 'success'
    return Box({
      flexDirection: 'row',
      children: [
        Text({ color: bullet, children: ['⏺ '] }),
        Text({ bold: true, children: [patch === undefined ? 'Bash' : 'Update'] }),
        Text({ children: [`(${patch ?? shortCommand(command ?? '')})`] }),
      ],
    })
  })
}
