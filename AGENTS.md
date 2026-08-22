# AGENTS.md — installing and changing Snap-To-Dictate

Written for an agent, not a person. A human setting this up should read
[README.md](README.md); this file is the version that assumes you will run
commands, read exit codes, and edit source. Everything here is checkable — if a
claim below disagrees with the code, the code is right and this file is a bug.

Three documents, three jobs, no overlap:

| File | Answers |
|---|---|
| `README.md` | what it is and how a person uses it |
| `CALIBRATION.md` | why calibration measures what it measures |
| `AGENTS.md` | how to install it, verify it, and change it safely |

---

## The whole thing in one paragraph

A background process listens to the microphone, recognises a finger snap, and
presses a keyboard shortcut into whichever window is in front. One snap toggles
dictation in the app you are looking at; two fast snaps stop dictation and
submit. Which shortcut gets pressed depends on which app has focus, so the same
gesture drives the Claude desktop app, ChatGPT, Codex and an IDE without you
telling it which one you meant. It is Windows-only, because it is built on
`SendInput` and the Win32 foreground-window API.

---

## Install

Windows, Python 3.8 or newer. Two dependencies, both wheels.

```bash
pip install -r requirements.txt
```

Then confirm the install rather than assuming it:

```bash
python snap_to_dictate.py --verify
```

**`--verify` is the contract between you and this repository.** It exits `0`
when the tool can work and non-zero when it cannot, so branch on the exit code,
not on the text. Add `--json` for a parseable report:

```bash
python snap_to_dictate.py --verify --json
```

```json
{
  "checks": [{"status": "OK", "check": "numpy importable", "detail": "2.4.4"}],
  "failed": []
}
```

`status` is one of three values and they mean different things:

- **`FAIL`** — the tool cannot work as installed. `failed` lists these by name.
  Exit code is 1 whenever this list is non-empty.
- **`WARN`** — it can work, but something a person chose is worth surfacing: no
  listener running, no wired app open, a profile left unconfigured. Never
  affects the exit code.
- **`OK`** — checked and good. Never means assumed.

`--verify` presses no keys and holds the microphone for under half a second. It
is safe to run on a machine mid-session, including one where a listener is
already running — it detects that and reports it rather than disturbing it. The
test suite asserts all three of those properties by reading the source, because
asserting them by running it would need a microphone.

### Start it

```bash
python autostart.py
```

That spawns a detached listener writing to `snap.log`, and exits immediately.
For a logon-triggered scheduled task, see *Autostart at logon* in README.md.

To stop one:

```bash
python snap_to_dictate.py --stop
```

---

## Architecture

Audio arrives in 256-sample blocks at 44.1 kHz — **5.805 ms per block**, the
unit every timing in this program is quantised to. A decay of "52.2 ms" is nine
blocks; there are no intermediate values, and a threshold set between two
multiples of 5.805 behaves exactly like the lower one.

```
microphone
    │  256-sample blocks
    ▼
SnapDetector.push()                  snap_to_dictate.py:236
    │  per-block rFFT, high band 1500-16000 Hz vs an EMA noise floor
    │  ONSET gates  ─► VERIFY gates ─► one event with five features
    ▼
event {peak_db, onset_hf, tail_hf, decay_ms, attack_ms, crest}
    │
    ▼
TriggerGate.offer()                  snap_to_dictate.py:507
    │  refractory, single-vs-double pairing, the send window
    ▼
listen() state machine               IDLE / RECORDING / SETTLING
    │  resolve_profile(exe, title) decides WHICH key
    ▼
send_key_if_focused()                re-reads focus, then SendInput
```

The one non-obvious thing about the detector: **`push()` already groups blocks
into events.** It returns `None` for most blocks and a single event dict at the
end of the verify stage. Code that counts blocks where something was loud is
counting the wrong thing; the event is the unit.

### Where to change what

| You want to change | Go to |
|---|---|
| what counts as a snap | `SnapDetector` gates, `snap_to_dictate.py:236` |
| single vs double, pairing windows | `TriggerGate`, `snap_to_dictate.py:507` |
| which key goes to which app | `resolve_profile`, `snap_to_dictate.py:737` |
| the dictation on/off/submit flow | `listen()` and `resolve_pending` |
| what calibration measures | `CAL_PASSES` and `derive()`, and CALIBRATION.md with it |
| install checks | `cmd_verify`, `snap_to_dictate.py` |

---

## Configuration

`config.json` sits beside the script. `load_config` merges it over `DEFAULTS`,
so **any key you omit falls back to a tuned default** and any key you misspell
is accepted in silence while the real setting keeps its old value. That failure
mode is why `--verify` reports unrecognised keys as a `FAIL`.

There are 36 settings. README.md's *Tuning reference* explains the ones worth
touching. The rest are described where they are defined, in `DEFAULTS`.

### Profiles

A profile says: when *this* window is in front, press *this* key.

```json
{
  "name": "Claude desktop",
  "process": "claude.exe",
  "title": null,
  "mode": "dictation",
  "activate": "ctrl+d",
  "send": "enter",
  "enabled": true
}
```

| Field | Meaning |
|---|---|
| `process` | executable name, lower-case; `null` to match on title alone |
| `title` | regex against the window title; `null` to match on process alone |
| `mode` | `dictation` — snap toggles on/off. `oneshot` — snap fires once, no stop |
| `activate` | key that starts or stops dictation |
| `send` | key that submits after a stop; `null` for none |
| `enabled` | `false` parks a profile without deleting it |

Matching is **most specific first**: a profile with both `process` and `title`
beats one with only `process`. That is not a nicety. The ChatGPT desktop app
serves its chat window and its Codex window from one executable at one PID, so
only the anchored title `^Codex$` separates them. For the same reason, all focus
comparisons elsewhere in the program are done by **profile name, never by
process** — two profiles can share an executable.

A disabled profile with `"activate": null` is a legitimate placeholder: the app
is wired up and waiting for somebody to find its dictation shortcut.
`profile_ready()` refuses to send for it, and `--verify` reports it as a `WARN`.

---

## Invariants

Break one of these and the tool becomes dangerous rather than merely broken.
Each has a test, so you will find out — run `python test_detector.py` before
concluding a change is fine.

**1. No profile may ever match a terminal.** `ctrl+d` is dictation in the Claude
desktop app and *end-of-input in every shell*. A stop that lands in a terminal
closes it, and when that terminal is running an agent, the agent dies mid-task.
The program carries a `TERMINALS` list; `--verify` checks every entry against
the live config, and the test suite checks it against the shipped one.

**2. A delayed keystroke must re-read focus immediately before pressing.** Three
sends happen later than the focus check that authorised them: a held stop waits
up to `PENDING_TIMEOUT_MS` for the room to go quiet, a submit waits
`send_delay_ms` for the transcript to land, and a manual submit waits for
confirmation. All three go through `send_key_if_focused()`, which re-reads the
foreground window and drops the keystroke if focus moved. Any new delayed send
must use it too — a test counts the call sites and fails if a bare `send_key()`
appears on a deferred path.

**3. One listener at a time.** The named event `Local\SnapToDictate.stop` is
both the singleton lock and the shutdown channel. A second copy exits before it
opens the microphone. Tests use a private event name so they never shut down a
live listener; do the same in anything new.

**4. Documentation and code must agree.** README.md and CALIBRATION.md once
described commands that did not exist — `--diagnose` was in the setup table for
a while, and CALIBRATION.md described a seven-pass protocol against code that
recorded three seconds and five snaps. The test suite now asserts that every
flag named in either document parses, that the documented pass list matches
`CAL_PASSES` exactly, and that each acceptance criterion appears in both places.
**If you change one, change the other in the same commit**, or the suite fails.

---

## Calibration

Full reasoning is in [CALIBRATION.md](CALIBRATION.md). The operational summary:

```bash
python snap_to_dictate.py --calibrate
```

Seven passes: 237 seconds of recording, about five minutes of wall clock once the
prompts between passes are counted. It lands as one WAV
plus a `.passes.json` sidecar marking where each pass starts and ends. It then
derives a config and applies an acceptance gate. **A config is written only if
all six checks pass**; otherwise `config.json` is left exactly as it was and the
recording is kept, so a fixed derivation can be re-run without asking the user
to perform it again:

```bash
python snap_to_dictate.py --derive calibration/<stamp>.wav
```

Calibration derives **levels and timings only** — `abs_floor_db`,
`speech_over_floor_db`, the pairing window, `send_window_ms`. It deliberately
leaves the shape gates (`hf_ratio_min`, `tail_hf_ratio_min`, the decay bounds)
and `pair_refractory_ms` alone. Those describe the physics of a snap and the
mechanics of the detector, not a property of a room, and fitting them to one
unlabelled take made both worse. CALIBRATION.md, *What calibration does not
derive*, has the measurements.

Do not "fix" a failing acceptance check by loosening it. One check was relaxed
in this repository's history and it took three independent measurements to
justify — the reasoning is written into the code beside the check.

---

## Tests

```bash
python test_detector.py
```

No microphone, no network, no fixtures to download. Exit code is non-zero on any
failure. Three kinds of check live in there:

1. **Synthetic audio** through the full detector — snaps, thumps, speech, ticks.
2. **A replay of a labelled field log** against the shipped `config.json`, so a
   tuning change that breaks real ground truth is caught immediately.
3. **Structural assertions** — the invariants above, and the documentation
   agreement described in invariant 4.

---

## Known limits

README.md, *Known gaps*, is the honest list. The one most likely to surprise you:
**a hard keystroke near the microphone measures the same as a snap.** Levels
overlap and shape does not separate them, so this is not a tuning problem and no
threshold fixes it. A quiet room fires nothing; typing right beside the mic can
occasionally send.
