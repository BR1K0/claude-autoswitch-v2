import { expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On, TurnStepResult } from 'claude-code'

const HOME = '/home/test'
const SPEED = `${HOME}/.autoswitch/speed`
const BUDGET = `${HOME}/.autoswitch/budget`
const HINT = `${HOME}/.autoswitch/app/hook/hint.py`
const MARKER = `${HOME}/.autoswitch/inline/cli-session`
const LOG = `${HOME}/.autoswitch/inline.jsonl`

// Windows: no HOME, USERPROFILE and OS=Windows_NT, backslash paths, `py -3` / `python` instead of python3.
const WIN_HOME = 'C:\\Users\\test'
const WIN_BASE = `${WIN_HOME}\\.autoswitch`
const WIN_SPEED = `${WIN_BASE}\\speed`
const WIN_HINT = `${WIN_BASE}\\app\\hook\\hint.py`
const WIN_LOG = `${WIN_BASE}\\inline.jsonl`
const WIN_MARKER = `${WIN_BASE}\\inline\\cli-session`
const WIN_PYCMD = `${WIN_BASE}\\python-cmd`
const WINDOWS = { USERPROFILE: WIN_HOME, OS: 'Windows_NT' }
const HOST_ID = 'host-chat-id'

// The engine resolves a path the host OS calls relative against the working directory before the hooks see it:
// on macOS/Linux `C:\Users\...` is relative, on Windows `/home/test/...` gains the current drive. Undo both, so
// every case reads the same files on every OS the suite runs on.
const key = (path: string) => {
  const drive = path.lastIndexOf('C:\\Users\\')
  if (drive >= 0) return path.slice(drive)
  return path.includes(HOME.replaceAll('/', '\\')) ? path.replace(/^[A-Za-z]:/, '').replaceAll('\\', '/') : path
}

type Router = (stdin: string) => { stdout: string; exitCode?: number } | 'fail' | 'slow'

// Everything beneath the plugin: files, env, clock, the router process, the model.
// `env` replaces HOME (Windows cases); `missing` are commands that cannot start (not installed); `exits` start
// but exit with that code (the Windows Store `python` stub).
const world = (
  on: On,
  files: Record<string, string>,
  router: Router,
  { env = { HOME } as Record<string, string>, missing = [] as string[], exits = {} as Record<string, number>, log = LOG } = {},
) => {
  const sent: { turnId: string; index: number; model: string; agentId?: string }[] = []
  const asked: { argv: readonly string[]; stdin: string; env: Record<string, string>; timeoutMs?: number }[] = []
  const tried: string[][] = []
  const clock = mock.clock(on, { now: 1_000_000 })
  mock.env(on, { ...env, CLAUDE_CODE_HOST_SESSION_ID: HOST_ID })

  on('fs.read', (_, e) => {
    const path = key(e.path)
    return path in files ? { value: files[path]! } : { deny: `ENOENT: ${path}` }
  })
  on('fs.write', (_, e) => {
    files[key(e.path)] = e.text
    return { value: undefined }
  })
  on('session.id', () => ({ value: 'cli-session' }))
  on('session.cwd', () => ({ value: '/work' }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('classic.UserPromptSubmit', () => ({}))
  on('turn.start', (_, e) => ({ turnId: e.turnId }))
  on('turn.complete', (_, e) => ({ text: e.answer }))
  on('process.run', async (_, e) => {
    tried.push([...e.argv])
    if (missing.includes(e.argv[0]!)) return { deny: `spawn ${e.argv[0]} ENOENT` }
    const exit = exits[e.argv[0]!]
    if (exit !== undefined) {
      return { value: { exitCode: exit, stdout: '', stderr: 'not found', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    asked.push({ argv: [...e.argv], stdin: e.init?.stdin ?? '', env: { ...(e.init?.env ?? {}) }, timeoutMs: e.init?.timeoutMs })
    const got = router(e.init?.stdin ?? '')
    if (got === 'fail') return { deny: 'command timed out after 1500 ms' }
    if (got === 'slow') {
      await clock.advance(2000)
      return { value: { exitCode: 0, stdout: hint('claude-haiku-5-5'), stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
    }
    return { value: { exitCode: got.exitCode ?? 0, stdout: got.stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('turn.step', async function* (_, e) {
    sent.push({ turnId: e.turnId, index: e.index, model: e.model, agentId: e.agentId })
    const done: TurnStepResult = {
      turnId: e.turnId, index: e.index, answer: 'ok', toolUses: [], stopReason: 'end_turn', usage: null,
    }
    return done
  })
  // the rows the plugin appended to its log, one per decided turn
  const logged = {
    get length() {
      return (files[log] ?? '').split('\n').filter(Boolean).length
    },
    rows: () => (files[log] ?? '').split('\n').filter(Boolean),
  }
  return { sent, asked, tried, logged, files }
}

const hint = (target: string | null, handoff: string | null = null) =>
  `${JSON.stringify({ target, reason: 'router says so', handoff_to: handoff, current: 'claude-opus-5-5', app_id: HOST_ID, label: 'haiku', confidence: 0.9, screenshot: false, new_chat: false })}\n`

const OPUS = 'claude-opus-5-5'

const runStep = async (
  $: Engine,
  input: { turnId: string; index: number; agentId?: string },
) => {
  const stream = $.turn.step({ model: OPUS, messageCount: 1, ...input })
  for await (const _ of stream) {
    // drain
  }
  return stream.result
}

const turn = async ($: Engine, turnId: string, prompt: string, steps = 1) => {
  await $.classic.UserPromptSubmit({ prompt, session_id: 'cli-session', transcript_path: '/t/cli-session.jsonl', cwd: '/work' })
  await $.turn.start({ text: prompt, turnId })
  for (let index = 0; index < steps; index++) await runStep($, { turnId, index })
}

test('inert unless speed is inline', async ($, on) => {
  const w = world(on, { [SPEED]: 'predictive\n' }, () => ({ stdout: hint('claude-haiku-5-5') }))
  await turn($, 't1', 'hi', 2)
  expect(w.asked.length).toBe(0)
  expect(w.sent.map(s => s.model)).toEqual([OPUS, OPUS])
  expect(w.logged.length).toBe(0)
})

test('rewrites the main loop to the router target, same model for every step', async ($, on) => {
  const w = world(on, { [SPEED]: 'inline\n' }, () => ({ stdout: hint('claude-haiku-5-5') }))
  await turn($, 't1', 'what is 2+2', 3)
  expect(w.asked.length).toBe(1)
  expect(w.sent.map(s => s.model)).toEqual(['claude-haiku-5-5', 'claude-haiku-5-5', 'claude-haiku-5-5'])

  const call = w.asked[0]!
  expect(JSON.parse(call.stdin)).toEqual({
    prompt: 'what is 2+2', session_id: 'cli-session', transcript_path: '/t/cli-session.jsonl', cwd: '/work',
  })
  expect(call.env).toEqual({
    AUTOSWITCH_DRY: '1', AUTOSWITCH_CURRENT_MODEL: OPUS, CLAUDE_CODE_ENTRYPOINT: 'claude-desktop',
    CLAUDE_CODE_HOST_SESSION_ID: HOST_ID,
  })
  expect(call.argv).toEqual(['python3', HINT])
  expect((call.timeoutMs ?? Infinity) <= 1500).toBe(true)

  expect(w.logged.length).toBe(1)
  const row = JSON.parse(w.logged.rows()[0]!)
  expect(row).toEqual(expect.objectContaining({ turnId: 't1', from: OPUS, to: 'claude-haiku-5-5', reason: 'router says so' }))
  expect(typeof row.ts).toBe('number')
  expect(typeof row.ms).toBe('number')
})

test('a null target or a handoff leaves the model alone', async ($, on) => {
  let n = 0
  const w = world(on, { [SPEED]: 'inline' }, () =>
    ++n === 1 ? { stdout: hint(null) } : { stdout: hint('claude-haiku-5-5', 'new-chat') },
  )
  await turn($, 't1', 'a', 1)
  await turn($, 't2', 'b', 1)
  expect(w.sent.map(s => s.model)).toEqual([OPUS, OPUS])
})

test('subagent steps are untouched', async ($, on) => {
  const w = world(on, { [SPEED]: 'inline' }, () => ({ stdout: hint('claude-haiku-5-5') }))
  await turn($, 't1', 'go', 1)
  await $.turn.start({ text: 'sub task', turnId: 's1', agentId: 'agent-1' })
  await runStep($, { turnId: 's1', index: 0, agentId: 'agent-1' })
  await runStep($, { turnId: 't1', index: 1 })
  expect(w.asked.length).toBe(1)
  expect(w.sent).toEqual([
    { turnId: 't1', index: 0, model: 'claude-haiku-5-5', agentId: undefined },
    { turnId: 's1', index: 0, model: OPUS, agentId: 'agent-1' },
    { turnId: 't1', index: 1, model: 'claude-haiku-5-5', agentId: undefined },
  ])
})

test('fails open when the router process errors', async ($, on) => {
  const w = world(on, { [SPEED]: 'inline' }, () => 'fail')
  await turn($, 't1', 'x', 2)
  expect(w.sent.map(s => s.model)).toEqual([OPUS, OPUS])
  expect(JSON.parse(w.logged.rows()[0]!).to).toBe(null)
})

test('fails open when the router takes longer than 1.5 s', async ($, on) => {
  const w = world(on, { [SPEED]: 'inline' }, () => 'slow')
  await turn($, 't1', 'x', 1)
  expect(w.sent.map(s => s.model)).toEqual([OPUS])
  expect(JSON.parse(w.logged.rows()[0]!).reason).toMatch(/too slow/)
})

test('fails open on a bad exit or unparsable output', async ($, on) => {
  let n = 0
  const w = world(on, { [SPEED]: 'inline' }, () =>
    ++n === 1 ? { stdout: hint('claude-haiku-5-5'), exitCode: 1 } : { stdout: 'Traceback...\n' },
  )
  await turn($, 't1', 'a', 1)
  await turn($, 't2', 'b', 1)
  expect(w.sent.map(s => s.model)).toEqual([OPUS, OPUS])
})

const BAND = {
  plugin: 'autoswitch-inline',
  component: 'AbovePrompt',
  props: {
    hasSurvey: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  },
} as const

test('the band shows the model the mod sent the last turn to, on every surface', async ($, on) => {
  const files: Record<string, string> = { [SPEED]: 'inline', [BUDGET]: 'balanced\n' }
  world(on, files, () => ({ stdout: hint('claude-haiku-5-5') }))
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  await turn($, 't1', 'hello', 1)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    await ui.press({ key: 'balanced' })
    expect((await ui.find({ type: 'Text', text: /answered/ }))?.text).toBe('↪ Haiku 5.5 answered · Budget: Balanced ')
    expect((await ui.find({ key: 'balanced' }))?.text).toBe('✓ Balanced')
    expect((await ui.find({ key: 'save' }))?.text).toBe('Save')
    await ui.press({ key: 'save' })
    expect((await ui.find({ key: 'save' }))?.text).toBe('✓ Save')
    expect(files[BUDGET]).toBe('save\n')
    await ui.unmount()
  }
})

test('the band writes save, balanced and quality to ~/.autoswitch/budget', async ($, on) => {
  const files: Record<string, string> = { [SPEED]: 'inline' }
  world(on, files, () => ({ stdout: hint(null) }))
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'desktop' })
  expect((await ui.find({ type: 'Text', text: /Budget/ }))?.text).toMatch(/Router inline · Budget: Balanced/)
  for (const value of ['save', 'balanced', 'quality'] as const) {
    await ui.press({ key: value })
    expect(files[BUDGET]).toBe(`${value}\n`)
  }
  await ui.unmount()
})

const engineDraws = (on: On) =>
  on('ui.render', ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })

test('the band yields to a survey', async ($, on) => {
  world(on, { [SPEED]: 'inline' }, () => ({ stdout: hint(null) }))
  engineDraws(on)
  await $.session.start({ cwd: '/work', surface: 'desktop', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, props: { ...BAND.props, hasSurvey: true }, surface: 'desktop' })
  expect((await ui.find({ type: 'Text' }))?.text).toBe('engine')
  await ui.unmount()
})

test('the band is not drawn while inert', async ($, on) => {
  world(on, { [SPEED]: 'predictive' }, () => ({ stdout: hint(null) }))
  engineDraws(on)
  await $.session.start({ cwd: '/work', surface: 'terminal', isInteractive: true })
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await ui.find({ type: 'Text' }))?.text).toBe('engine')
  await ui.unmount()
})

test('marks the chat as inline before the router hook runs', async ($, on) => {
  const files: Record<string, string> = { [SPEED]: 'inline' }
  world(on, files, () => ({ stdout: hint(null) }))
  await turn($, 't1', 'hello', 1)
  expect(files[MARKER]).toBe('1\n')
})

// ---- Windows: the engine reports the platform through OS=Windows_NT; paths, home and Python follow it ----

test('windows: USERPROFILE home, backslash paths and py -3', async ($, on) => {
  const files: Record<string, string> = { [WIN_SPEED]: 'inline\r\n' }
  const w = world(on, files, () => ({ stdout: hint('claude-haiku-5-5') }), { env: WINDOWS, log: WIN_LOG })
  await turn($, 't1', 'what is 2+2', 2)
  expect(w.sent.map(s => s.model)).toEqual(['claude-haiku-5-5', 'claude-haiku-5-5'])
  expect(w.asked.length).toBe(1)
  expect(w.asked[0]!.argv).toEqual(['py', '-3', WIN_HINT])
  expect((w.asked[0]!.timeoutMs ?? Infinity) <= 1500).toBe(true)
  expect(files[WIN_MARKER]).toBe('1\n')
  expect(w.logged.length).toBe(1)
  expect(JSON.parse(w.logged.rows()[0]!)).toEqual(expect.objectContaining({ turnId: 't1', to: 'claude-haiku-5-5' }))
})

test('windows: the transcript path is built under USERPROFILE when the hook gave none', async ($, on) => {
  const w = world(on, { [WIN_SPEED]: 'inline' }, () => ({ stdout: hint(null) }), { env: WINDOWS, log: WIN_LOG })
  await $.turn.start({ text: 'hello', turnId: 't1' })
  await runStep($, { turnId: 't1', index: 0 })
  expect(JSON.parse(w.asked[0]!.stdin).transcript_path).toBe(`${WIN_HOME}\\.claude\\projects\\-work\\cli-session.jsonl`)
})

test('windows: the launcher install.ps1 recorded goes first', async ($, on) => {
  const files = { [WIN_SPEED]: 'inline', [WIN_PYCMD]: 'py -3.12\r\n' }
  const w = world(on, files, () => ({ stdout: hint('claude-haiku-5-5') }), { env: WINDOWS, log: WIN_LOG })
  await turn($, 't1', 'x', 1)
  expect(w.asked[0]!.argv).toEqual(['py', '-3.12', WIN_HINT])
})

test('windows: falls back to python when py is missing, and remembers it', async ($, on) => {
  const w = world(on, { [WIN_SPEED]: 'inline' }, () => ({ stdout: hint('claude-haiku-5-5') }), {
    env: WINDOWS, missing: ['py'], log: WIN_LOG,
  })
  await turn($, 't1', 'a', 1)
  await turn($, 't2', 'b', 1)
  expect(w.sent.map(s => s.model)).toEqual(['claude-haiku-5-5', 'claude-haiku-5-5'])
  expect(w.tried.map(a => a[0])).toEqual(['py', 'python', 'python'])
  expect(w.asked.map(a => a.argv)).toEqual([['python', WIN_HINT], ['python', WIN_HINT]])
})

test('windows: an interpreter that exits non-zero (Store stub) is skipped', async ($, on) => {
  const w = world(on, { [WIN_SPEED]: 'inline' }, () => ({ stdout: hint('claude-haiku-5-5') }), {
    env: WINDOWS, exits: { py: 9009 }, log: WIN_LOG,
  })
  await turn($, 't1', 'a', 1)
  expect(w.tried.map(a => a[0])).toEqual(['py', 'python'])
  expect(w.sent.map(s => s.model)).toEqual(['claude-haiku-5-5'])
})

test('windows: fails open when no Python can start', async ($, on) => {
  const w = world(on, { [WIN_SPEED]: 'inline' }, () => ({ stdout: hint('claude-haiku-5-5') }), {
    env: WINDOWS, missing: ['py', 'python'], log: WIN_LOG,
  })
  await turn($, 't1', 'a', 2)
  expect(w.sent.map(s => s.model)).toEqual([OPUS, OPUS])
  expect(JSON.parse(w.logged.rows()[0]!).to).toBe(null)
})

test('windows: a slow router is not retried with another interpreter', async ($, on) => {
  const w = world(on, { [WIN_SPEED]: 'inline' }, () => 'fail', { env: WINDOWS, log: WIN_LOG })
  await turn($, 't1', 'a', 1)
  expect(w.sent.map(s => s.model)).toEqual([OPUS])
  expect(w.tried.length).toBe(1)
})

test('windows: HOME wins over USERPROFILE when both are set', async ($, on) => {
  const other = 'C:\\Users\\gitbash'
  const files = { [WIN_SPEED]: 'predictive', [`${other}\\.autoswitch\\speed`]: 'inline' }
  const w = world(on, files, () => ({ stdout: hint('claude-haiku-5-5') }), {
    env: { ...WINDOWS, HOME: other }, log: `${other}\\.autoswitch\\inline.jsonl`,
  })
  await turn($, 't1', 'a', 1)
  expect(w.asked.map(a => a.argv)).toEqual([['py', '-3', `${other}\\.autoswitch\\app\\hook\\hint.py`]])
})

test('the log keeps the last 2000 rows', async ($, on) => {
  const old = Array.from({ length: 2500 }, (_, i) => JSON.stringify({ n: i })).join('\n') + '\n'
  const w = world(on, { [SPEED]: 'inline', [LOG]: old }, () => ({ stdout: hint(null) }))
  await turn($, 't1', 'a', 1)
  const rows = w.logged.rows()
  expect(rows.length).toBe(2000)
  expect(JSON.parse(rows[0]!)).toEqual({ n: 501 })
  expect(JSON.parse(rows.at(-1)!).turnId).toBe('t1')
})
