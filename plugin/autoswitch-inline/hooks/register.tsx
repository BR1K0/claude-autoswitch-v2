import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { AutoswitchInlineBudget, AutoswitchInlineLast } from '../types'

// The router is the installed claude-autoswitch hook (install.sh copies it to ~/.autoswitch/app/hook/hint.py);
// the mod only asks it, never decides.
const LIMIT_MS = 1500
const BUDGETS: readonly AutoswitchInlineBudget[] = ['save', 'balanced', 'quality']

const isActive = atom({ plugin: 'autoswitch-inline', key: 'isActive' } as const, false)
const budget = atom({ plugin: 'autoswitch-inline', key: 'budget' } as const, 'balanced')
const last = atom({ plugin: 'autoswitch-inline', key: 'last' } as const, null)

type Context = { prompt: string; sessionId?: string; transcriptPath?: string; cwd?: string }
type Hint = { target?: unknown; reason?: unknown; handoff_to?: unknown }

const home = async ($: EngineInterface) => (await $.env.get('HOME')) ?? ''
// State lives in $AUTOSWITCH_DIR, by default ~/.autoswitch (the same folder the hook and `autoswitch` use).
const baseDir = async ($: EngineInterface) => (await $.env.get('AUTOSWITCH_DIR')) || `${await home($)}/.autoswitch`
const sbPath = async ($: EngineInterface, name: string) => `${await baseDir($)}/${name}`
const hintPath = async ($: EngineInterface) => sbPath($, 'app/hook/hint.py')

const readWord = async ($: EngineInterface, name: string): Promise<string> => {
  try {
    const text = await $.fs.read(await sbPath($, name))
    return typeof text === 'string' ? text.trim().toLowerCase() : ''
  } catch {
    return ''
  }
}

const isInline = async ($: EngineInterface) => (await readWord($, 'speed')) === 'inline'

const readBudget = async ($: EngineInterface): Promise<AutoswitchInlineBudget> => {
  const word = await readWord($, 'budget')
  return (BUDGETS as readonly string[]).includes(word) ? (word as AutoswitchInlineBudget) : 'balanced'
}

const refresh = async ($: EngineInterface) => {
  const active = await isInline($)
  const current = await readBudget($)
  await update($, isActive, () => active)
  await update($, budget, () => current)
  return active
}

const setBudget = async ($: EngineInterface, value: AutoswitchInlineBudget) => {
  await $.fs.write(await sbPath($, 'budget'), `${value}\n`)
  await update($, budget, () => value)
}

// claude-haiku-5-5 -> "Haiku 5.5", claude-opus-4-7 -> "Opus 4.7", claude-sonnet-5 -> "Sonnet 5"
const modelName = (id: string) => {
  const m = /(haiku|sonnet|opus|fable|mythos)-(\d+)(?:-(\d+))?/i.exec(id)
  if (!m) return id
  const name = m[1]![0]!.toUpperCase() + m[1]!.slice(1).toLowerCase()
  return m[3] !== undefined ? `${name} ${m[2]}.${m[3]}` : `${name} ${m[2]}`
}

const cap = (word: string) => word[0]!.toUpperCase() + word.slice(1)

const parseHint = (stdout: string): Hint | null => {
  const lines = stdout.split('\n').map(l => l.trim()).filter(Boolean)
  const line = lines.at(-1)
  if (line === undefined) return null
  try {
    const value: unknown = JSON.parse(line)
    return value !== null && typeof value === 'object' ? (value as Hint) : null
  } catch {
    return null
  }
}

/** Asks hint.py (dry run) for the turn's model. Null: leave it alone. */
const ask = async (
  $: EngineInterface,
  ctx: Context,
  model: string,
): Promise<{ target: string | null; reason: string; ms: number }> => {
  const startedAt = await $.clock.now()
  const elapsed = async () => (await $.clock.now()) - startedAt
  try {
    const sessionId = ctx.sessionId ?? (await $.session.id())
    const cwd = ctx.cwd ?? (await $.session.cwd())
    const transcriptPath =
      ctx.transcriptPath ??
      `${await home($)}/.claude/projects/${cwd.replace(/[^A-Za-z0-9]/g, '-')}/${sessionId}.jsonl`
    const hostId = await $.env.get('CLAUDE_CODE_HOST_SESSION_ID')
    const env: Record<string, string> = {
      AUTOSWITCH_DRY: '1',
      // the model this request runs on: lets the router move an older version (Opus 4.7) to the tier's newest
      AUTOSWITCH_CURRENT_MODEL: model,
      CLAUDE_CODE_ENTRYPOINT: 'claude-desktop',
    }
    if (hostId !== undefined) env.CLAUDE_CODE_HOST_SESSION_ID = hostId
    const ran = await $.process.run(['python3', await hintPath($)], {
      env,
      stdin: JSON.stringify({
        prompt: ctx.prompt,
        session_id: sessionId,
        transcript_path: transcriptPath,
        cwd,
      }),
      timeoutMs: LIMIT_MS,
    })
    const ms = await elapsed()
    if (ms > LIMIT_MS) return { target: null, reason: `router too slow (${ms} ms)`, ms }
    if (ran.exitCode !== 0) return { target: null, reason: `router exited ${ran.exitCode}`, ms }
    const hint = parseHint(ran.stdout)
    if (hint === null) return { target: null, reason: 'router gave no decision', ms }
    const reason = typeof hint.reason === 'string' ? hint.reason : ''
    if (hint.handoff_to !== null && hint.handoff_to !== undefined) {
      return { target: null, reason: `handoff (${reason})`, ms }
    }
    const target = typeof hint.target === 'string' && hint.target !== '' ? hint.target : null
    return { target, reason, ms }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    return { target: null, reason: `router failed: ${message.slice(0, 120)}`, ms: await elapsed().catch(() => 0) }
  }
}

const log = async ($: EngineInterface, row: Record<string, unknown>) => {
  try {
    await $.process.run(['/usr/bin/tee', '-a', await sbPath($, 'inline.jsonl')], {
      stdin: `${JSON.stringify(row)}\n`,
      timeoutMs: 1000,
    })
  } catch {
    // a log that cannot be written never holds the turn up
  }
}

export const register: Register = on => {
  // Per-turn caches: lost on a reload, which only means a turn is left alone.
  let pending: Context | null = null
  const prompts = new Map<string, Context>()
  const models = new Map<string, string | null>()

  on('session.start', async ($, e, next) => {
    const started = await next(e)
    await refresh($)
    return started
  })

  on('prompt.submit', ($, e, next) => {
    if (e.command === undefined && e.isMeta !== true) pending = { ...pending, prompt: e.text }
    return next(e)
  }).catch(($, e, next) => next(e))

  // The settings hook's own input carries the CLI session id and transcript path.
  on('classic.UserPromptSubmit', async ($, e, next) => {
    if (e.agent_id === undefined) {
      // Tells the router's hook (which runs inside next(e)) that this chat has the mod: without the marker
      // (a chat opened before the mod was installed) the hook falls back to the predictive speed.
      try {
        await $.fs.write(await sbPath($, `inline/${e.session_id}`), '1\n')
      } catch {
        // no marker: the hook falls back, nothing breaks
      }
      pending = {
        prompt: e.prompt,
        sessionId: e.session_id,
        transcriptPath: e.transcript_path || undefined,
        cwd: e.cwd,
      }
    }
    return next(e)
  }).catch(($, e, next) => next(e))

  on('turn.start', async ($, e, next) => {
    if (e.agentId === undefined) {
      const text = e.text !== '' ? e.text : (pending?.prompt ?? '')
      prompts.set(e.turnId, { ...pending, prompt: text })
      pending = null
    }
    return next(e)
  })

  on('turn.step', async function* ($, e, next) {
    if (e.agentId !== undefined) return yield* next(e)

    if (e.index === 0 && !models.has(e.turnId)) {
      const ctx = prompts.get(e.turnId)
      prompts.delete(e.turnId)
      let chosen: string | null = null
      if ((await refresh($)) && ctx !== undefined && ctx.prompt !== '') {
        const got = await ask($, ctx, e.model)
        chosen = got.target !== null && got.target !== e.model ? got.target : null
        const row: AutoswitchInlineLast = { turnId: e.turnId, from: e.model, to: chosen, reason: got.reason }
        await update($, last, () => row)
        await log($, { ts: (await $.clock.now()) / 1000, turnId: e.turnId, from: e.model, to: chosen, reason: got.reason, ms: got.ms })
      }
      models.set(e.turnId, chosen)
      while (models.size > 50) models.delete(models.keys().next().value!)
    }

    const model = models.get(e.turnId) ?? null
    return yield* next(model === null ? e : { ...e, model })
  })

  on('turn.complete', ($, e, next) => {
    if (e.agentId === undefined) {
      models.delete(e.turnId)
      prompts.delete(e.turnId)
    }
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || !(await read($, isActive))) return next(e)

    const { Box, Button, Text } = $.ui.resolve(e)
    const turn = await read($, last)
    const current = await read($, budget)

    const said =
      turn === null
        ? '↪ Router inline'
        : turn.to !== null
          ? `↪ ${modelName(turn.to)} answered`
          : `↪ ${modelName(turn.from)} (kept)`

    return (
      <Box flexDirection="row">
        <Text dimColor>{said} · Budget: {cap(current)} </Text>
        {BUDGETS.map(value => (
          <Button
            key={value}
            label={value === current ? `✓ ${cap(value)}` : cap(value)}
            variant={value === current ? 'primary' : 'secondary'}
            dimColor={value !== current}
            onPress={() => setBudget($, value)}
          />
        ))}
      </Box>
    )
  })
}
