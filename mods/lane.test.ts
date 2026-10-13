import { expect, test } from 'claude-code/testing'

import { doneText, isDispatchCommand, parseItem } from './lane.ts'

const RELAY = 'relay-1'
const HEX = 'd' + 'a'.repeat(32)
const COMMAND = 'hippo dispatch --kind impl --scope "parser fix" -m gpt-6-luna "Fix the parser."'
const OUTPUT = '/tmp/tasks/b1.output'
const RECORD = `/work/.hippo/lanes/${HEX}.json`
const FINAL = `lane ${HEX} exited rc=0 after 2m10s · 2 cmds · parser fix\nreport: /work/.hippo/lanes/${HEX}.out\nraw log: /work/.hippo/lanes/${HEX}.log`

// What the wrapper writes to <id>.events.jsonl (LaneItems, cli/hippo_cli.py).
const ITEMS = [
  { k: 'said', text: 'I will read the parser first.' },
  { k: 'exec', n: 1, cmd: 'sed -n 1,80p src/parser.py', cwd: '/work' },
  { k: 'exec', n: 2, cmd: 'pytest -q', cwd: '/work' },
  { k: 'done', n: 1, rc: 0, ms: 3, out: 'def parse(x):\n    return x', lines: 2 },
  { k: 'done', n: 2, rc: 1, ms: 812, out: 'F.. 1 failed', lines: 1 },
  { k: 'said', text: 'One test fails; fixing it.' },
  { k: 'end', status: 'exited', rc: 0 },
].map((i) => JSON.stringify(i) + '\n')

type Chunk = { kind: string; index?: number; text?: string; id?: string; name?: string; json?: string }

// The relay's one Bash call of a step, run as the engine runs it: through the module's hooks.
async function runBash($, step: { chunks: Chunk[] }) {
  const tool = step.chunks.find((c) => c.kind === 'tool')!
  const input = JSON.parse(step.chunks.find((c) => c.kind === 'input')!.json!)
  expect(tool.name).toBe('Bash')
  await $.tool.call({ tool: 'Bash', tool_use_id: tool.id, agentId: RELAY, ...input } as never)
  return input
}

async function readStep($, index: number, agentId = RELAY) {
  const chunks: Chunk[] = []
  const stream = $.turn.step({ turnId: 'turn-1', index, model: 'claude-sonnet', messageCount: 1, agentId })
  let r = await stream.next()
  for (; r.done !== true; r = await stream.next()) chunks.push(r.value)
  return { chunks, result: r.value }
}

// Claude Code, as far as one relay needs it: the model (counted — it must never be asked), the
// Bash tool (answering with the next text of `bash`), and the lane's files.
function host(on, bash: string[], served: { lines: number }) {
  const asked = { model: 0 }
  on('session.start', () => ({ cwd: '/work' }))
  on('tool.register', () => ({ value: { tool: 'mcp__hippo__codex' } }))
  on('agent.spawn', () => ({ model: 'claude-sonnet', agentId: RELAY }))
  on('turn.step', async function* ($, e) {
    asked.model++
    yield { kind: 'text', index: 0, text: 'model' }
    return { turnId: e.turnId, index: e.index, answer: 'model', toolUses: [], stopReason: 'end_turn', usage: null }
  })
  on('tool.call', ($, e) => {
    const text = bash.shift() ?? ''
    return { result: { stdout: text, stderr: '' }, text }
  })
  on('session.cwd', () => ({ value: '/work' }))
  on('fs.exists', ($, e) => ({ value: e.path === RECORD }))
  on('fs.read', () => ({ value: JSON.stringify({ id: HEX, pid: 4242 }) }))
  on('process.run', ($, e) => ({ value: { exitCode: 0, stdout: e.argv[0] === 'tail' ? ITEMS.slice(served.lines).join('') : '', stderr: '' } }))
  on('process.spawn', async function* ($, e) {
    // `tail -n +K -F <id>.events.jsonl`: the lines from K on, as the wrapper has written them.
    const from = Number(e.argv[2].slice(1)) - 1
    const lines = ITEMS.slice(from, served.lines)
    if (lines.length) yield { stream: 'stdout', text: lines.join('') }
    return { value: { code: 0, signal: null } }
  })
  on('ui.log', () => ({ value: undefined }))
  return asked
}

test('the item grammar the mod reads is the wrapper’s, line by line', () => {
  expect(parseItem(ITEMS[1])).toEqual({ k: 'exec', n: 1, cmd: 'sed -n 1,80p src/parser.py', cwd: '/work' })
  expect(parseItem('{"k": "exe')).toEqual({ k: 'unreadable' })
  expect(doneText({ k: 'done', n: 2, rc: 1, ms: 812, out: 'F.. 1 failed\n', lines: 1 })).toBe('Exit code 1\nF.. 1 failed')
  expect(doneText({ k: 'done', n: 1, rc: 0, ms: 3, out: '', lines: 1 })).toBe('(No output)')
  expect(isDispatchCommand('  hippo dispatch --batch m.yaml')).toBe(true)
  expect(isDispatchCommand('Please run hippo dispatch --batch m.yaml')).toBe(false)
})

test('a hippo:lane relay asks no model: launch, id, codex rows, final lines', async ($, on) => {
  const bash: string[] = []
  const served = { lines: 3 }
  const asked = host(on, bash, served)
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await $.agent.spawn({ prompt: COMMAND, description: 'parser fix', subagentType: 'hippo:lane' } as never)

  // 1. The command verbatim, in the background, with the longest deadline the host allows.
  let step = await readStep($, 0)
  bash.push(`Command running in background with ID: b1. Output is being written to: ${OUTPUT}. You will be notified.`)
  expect(await runBash($, step)).toEqual({ command: COMMAND, description: 'parser fix', run_in_background: true, timeout: 2147483647 })

  // 2. The id, read off the output file by lane.md's own grep.
  step = await readStep($, 1)
  bash.push(`dispatch:${HEX}`)
  expect((await runBash($, step)).command).toBe(`grep -m1 -oE '(batch:b|dispatch:d)[0-9a-f]{32}' '${OUTPUT}'`)

  // 3. codex's message as text, its two commands (started together) as two rows of one step.
  step = await readStep($, 2)
  expect(step.chunks.filter((c) => c.kind === 'text').map((c) => c.text)).toEqual(['I will read the parser first.'])
  const rows = step.chunks.filter((c) => c.kind === 'tool')
  expect(rows.map((c) => c.name)).toEqual(['mcp__hippo__codex', 'mcp__hippo__codex'])
  const inputs = step.chunks.filter((c) => c.kind === 'input').map((c) => JSON.parse(c.json!))
  expect(inputs.map((i) => i.command)).toEqual(['sed -n 1,80p src/parser.py', 'pytest -q'])
  expect(step.result.stopReason).toBe('tool_use')

  // Each row's result is codex's output; a failed command reads as Bash's would.
  served.lines = ITEMS.length
  const first = await $.tool.call({ tool: 'mcp__hippo__codex', agentId: RELAY, ...inputs[0] } as never)
  expect(first.result).toBe('def parse(x):\n    return x')
  const second = await $.tool.call({ tool: 'mcp__hippo__codex', agentId: RELAY, ...inputs[1] } as never)
  expect(second.result).toBe('Exit code 1\nF.. 1 failed')

  // 4. The lane is over: its last message, then lane.md's own final call.
  step = await readStep($, 3)
  expect(step.chunks.filter((c) => c.kind === 'text').map((c) => c.text)).toEqual(['One test fails; fixing it.'])
  bash.push(FINAL + '\n')
  expect((await runBash($, step)).command).toBe(`hippo dispatch --watch ${HEX}`)

  // 5. Its output is the reply, verbatim.
  step = await readStep($, 4)
  expect(step.result.answer).toBe(FINAL)
  expect(step.result.stopReason).toBe('end_turn')
  expect(asked.model).toBe(0)
})

test('every other agent, and a lane prompt that is no dispatch command, is the model’s', async ($, on) => {
  const asked = host(on, [], { lines: 0 })
  await $.session.start({ surface: 'terminal', isInteractive: true, cwd: '/work' })
  await readStep($, 0, 'some-other-agent')
  expect(asked.model).toBe(1)
  await $.agent.spawn({ prompt: 'Please launch the parser lane.', description: 'x', subagentType: 'hippo:lane' } as never)
  const step = await readStep($, 0)
  expect(step.result.answer).toBe('model')
  expect(asked.model).toBe(2)
  const denied = await $.tool.call({ tool: 'mcp__hippo__codex', command: 'ls', item: 0 } as never)
  expect(denied.deny).toMatch(/nothing ran/)
})

test('a codex row is drawn as its native twin: Bash for a command, Update for a patch', async ($, on) => {
  const row = (input: Record<string, unknown>) => ({
    plugin: 'hippo',
    component: 'ToolUse',
    requestId: 'toolu_x',
    surface: 'terminal',
    viewport: { columns: 120, rows: 40 },
    props: { tool_use_id: 'toolu_x', tool: 'mcp__hippo__codex', input, isRunning: false, isErrored: false, isInterrupted: false },
  }) as const
  let ui = await $.ui.mount(row({ command: "cat > f <<'EOF'\nprint(1)\nprint(2)\nEOF", item: 3 }) as never)
  expect(await ui.find({ type: 'Text', text: 'Bash' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: "(cat > f <<'EOF'\nprint(1) …)" })).toBeDefined()
  await ui.unmount()
  ui = await $.ui.mount(row({ patch: 'parser.py', item: 8 }) as never)
  expect(await ui.find({ type: 'Text', text: 'Update' })).toBeDefined()
  expect(await ui.find({ type: 'Text', text: '(parser.py)' })).toBeDefined()
})
