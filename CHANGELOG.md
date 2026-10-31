# Changelog

## 0.3.0

Two new speeds that answer the first message on the right model, with no stop notice and no wait.

- **`inline` (recommended, macOS):** the bundled Claude Code plugin `autoswitch-inline` (`plugin/`, marketplace in
  `.claude-plugin/`) picks each main-loop turn's model itself, from the installed hook's decision (`hint.py` with
  `AUTOSWITCH_DRY=1`). The hook stops, queues and shows nothing in those chats, and they need no Watcher. The
  plugin marks each chat it runs in (`~/.autoswitch/inline/<chat id>`); chats opened before it was installed fall
  back to `predictive`. Fails open: a router error or a decision slower than 1.5 s leaves the turn's model alone.
- **Budget bar:** with `inline`, a band above the prompt box shows the last turn's model ("↪ Opus 5.5 (kept)",
  "↪ Haiku 5.5 answered") and Save / Balanced / Quality buttons that write `~/.autoswitch/budget`.
- **`predictive` (macOS):** a read-only reader of the prompt box (`hook/typeahead.swift`, started by the Watcher)
  switches the chat while you type. At Enter, a message whose switch has not landed is held like `seamless` only for
  an up-switch or a message of 400+ characters (`AUTOSWITCH_HOLD_CHARS`); otherwise it goes through at once.
- **Same tier, newer model:** a chat on an older version of its tier (Opus 4.7, Sonnet 5, ...) moves to the tier's
  current model, except in long chats (`AUTOSWITCH_UPGRADE_MAX_TOKENS`, 100k), pinned chats and handoffs. New chats
  get the request's model from the plugin (`AUTOSWITCH_CURRENT_MODEL`).
- **`stoptext`:** `echo hide > ~/.autoswitch/stoptext` hides the "↪ Switched to X" text on a held message.
- `autoswitch speed [inline|predictive|instant|seamless]`; `inline` also runs `claude plugin marketplace add` and
  `claude plugin install autoswitch-inline@claude-autoswitch --scope user` (or prints them). `install.sh` copies the
  plugin and builds the typeahead reader; `uninstall.sh` removes both.
- The dry-run decision never writes state: no pin observation, no shadow log, no lasting-switch record, and the
  clipboard check no longer records what it saw.
- Docs: README "Speed" section for all four speeds, the band, the newer-model upgrade; new demo image of the band.

## 0.2.0

v2, everything off by default (`~/.autoswitch/handoff`, `~/.autoswitch/rollover`); with both off, decisions are
exactly v0.1's (tested).

- **Handoff to a new chat** for long chats, built by ACPMH (now bundled in `acpmh/`): v4 from 70k to 300k tokens, v5b
  above (balanced). Below ~70k the chat still switches in place. Only for lasting switches.
- **Budget modes** quality / balanced / save, optional spending limit with pacing, calibration hook.
- **Context-aware scoring** for short messages.
- **Real history size** from the transcript.
- **Exactly-once delivery** of the handed-off message, settings carried over (never more), "please resend" and
  "Autoswitch is paused" notes, one Watcher at a time.
- **Automatic rollover** of chats over 200k into a part 2, with jobs, crash recovery and a loop guard; optional
  `hook/stop.py` Stop hook.
- Benchmark in `docs/BENCHMARK.md`.

## Unreleased

- Instant mode is a first-class setting. `autoswitch speed [instant|seamless]` writes `~/.autoswitch/speed` (no argument prints the current value) and `autoswitch status` shows it. `install.sh` / `install.ps1` write `speed=instant` on a fresh install only (no `mode` file yet) and never overwrite an existing `speed` file; fresh installs still start in shadow mode, and upgrades keep `seamless` unless you change it. `WATCHER.md` and `hook/watch.py` spell out `send: false` (switch only, nothing sent or started) on macOS and Windows. README: new "Speed: instant vs seamless" section; trade-off: with `instant` the first message after a change of need is answered by the current model. Tests for instant with and without a target, pinned chats, new chats and screenshots, and for the CLI on both platforms.
- Faster switches: the Watcher now makes only the two app calls (set model, send) and no longer runs `finish.py done` on success. The request is closed by `finish.py settle` (started by `watch.py`) once the re-sent prompt reaches the hook, or as an error with a "please resend" note after 120 s. `watch.py` polls every 0.05 s (was 0.1 s). Older Watcher prompts keep working.
- Windows support: `install.ps1` / `uninstall.ps1`, a Task Scheduler task for the scorer, `autoswitch.cmd` on the user PATH, screenshots detected via the Windows clipboard (no OCR yet). Paths come from one helper (`hook/paths.py`): `%APPDATA%\Claude` on Windows. The Watcher loop is now cross-platform Python (`hook/watch.py`; `watch.sh` is a one-line wrapper) and `WATCHER.md` is rendered per platform (`python3` on Mac, `py -3`/`python` on Windows). Also: queue files are replaced with `os.replace`, hook input is read as UTF-8, settings.json keeps its line endings. macOS behaviour is unchanged.
- Manual pins: a model change made by hand (the picker or `/model`) pins the chat; pinned chats are never switched. `autoswitch unpin` clears pins.
- The unclear-prompt move to Sonnet only applies to new or short chats; unknown history size never passes a down-switch.

- Switch text is exactly "↪ Switched to Haiku" (Sonnet / Opus likewise). The app adds its own prefix; no hook output avoids it in the CLI.
- New speed setting: `AUTOSWITCH_SPEED=instant` (or the file `~/.autoswitch/speed`). The hook never holds the prompt: the current message answers on the current model and the switch applies from the next one. Default stays `seamless` (hold, switch, re-send). Trade-off: with `instant` the first message after a change of need runs on the old model.
- Watcher polls every 0.1 s and is told to make set-model, re-send and file-record calls in one message (ordering not verified live).

## 0.1.1

- Switching no longer shows "blocked": the hook stops the turn with a plain "Switching to Haiku..." text.
- Leaner Watcher prompt: re-send and file the record in one step.
- A request that cannot be parsed still leaves a "please send it again" note; `finish.py done` no longer crashes on odd request files; the Watcher files `error` when its fallback worker cannot start.

## 0.1.0

First public release.

- Local scorer (haiku / sonnet / opus) trained on public data: synthetic chat prompts plus SWE-bench issues labelled by what actually solved them.
- `UserPromptSubmit` hook with policy: confidence gates, dwell guard for long chats, hand-picked models respected, Sonnet as the default for unclear prompts.
- Watcher flow: hold the prompt, switch the chat's model, re-send; fallbacks and "send it again" notices so nothing is lost silently.
- Screenshot awareness: pasted images are detected, read with macOS Vision and never held.
- Never blocks outside the desktop app, agent-to-agent messages, tool notifications, or when no Watcher is running.
- `./install.sh` (private venv, launch agent, safe idempotent settings edit with backup, shadow mode by default) and `./uninstall.sh`.
- `autoswitch on|off|shadow|hint|status|report|watcher`.
- Hook latency about 70 ms.
