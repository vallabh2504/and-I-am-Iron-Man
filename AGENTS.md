# AGENTS.md, installing and changing Snap-To-Dictate

Written for an agent, not a person. A human setting this up should read
[README.md](README.md). This file is the version that assumes you will run
commands, read exit codes, and edit source. Everything here is checkable. If a
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
dictation in the app you are looking at. Two fast snaps stop dictation and
submit. Which shortcut gets pressed depends on which app has focus, so the same
gesture drives the Claude desktop app, ChatGPT, Codex and an IDE without you
telling it which one you meant. An app with no profile of its own falls through
to Windows' built-in voice typing, so the gesture is the same everywhere except
in a short list of windows it deliberately refuses to touch. It is Windows only,
because it is built on `SendInput` and the Win32 foreground-window API.

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
when the tool can work and non-zero when it cannot, so branch on the exit code
rather than on the text. Add `--json` for a parseable report:

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

- **`FAIL`** means the tool cannot work as installed. `failed` lists these by
  name. Exit code is 1 whenever that list is non-empty.
- **`WARN`** means it can work, but something a person chose is worth
  surfacing, such as no listener running, no wired app open, or a profile left
  unconfigured. It never affects the exit code.
- **`OK`** means checked and good. It never means assumed.

`--verify` presses no keys and holds the microphone for under half a second. It
is safe to run on a machine mid-session, including one where a listener is
already running, because it detects that and reports it rather than disturbing
it. The test suite asserts all three of those properties by reading the source,
because asserting them by running it would need a microphone.

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

## What you can finish alone, and what you cannot

Somebody has been handed this repository and has handed it to you. Most of the
setup you can complete without troubling them. The rest genuinely cannot be
done without them, and the reason is always the same one: the step needs a
person in the room, in front of a window, making a noise. Say which of the two
you are at. Do not stall on a human step and do not pretend you performed it.

| Step | Who does it | What you do |
|---|---|---|
| `pip install -r requirements.txt` | agent alone | Run it. Two wheels, no build step, nothing beyond the package index. |
| `python snap_to_dictate.py --verify` | agent alone | Run it and branch on the exit code, not the text. `--json` gives you the same report parsed. A `WARN` for no listener running and no wired app open is expected on a fresh machine and is not yours to fix. |
| `python test_detector.py` | agent alone | Run it before you change anything, so you know what the baseline was, and again before you call a change fine. |
| Editing `config.json` | agent alone, within limits | The device index, the thresholds and the scaffolding of a new profile are yours to write. `"enabled": true` on a shortcut nobody has confirmed is not. See *Never guess a shortcut* below. |
| Registering the logon scheduled task | agent alone, one condition | Run the PowerShell from *Autostart at logon* in README.md. It reads the repository path, the interpreter path, `$env:USERNAME` and `$env:COMPUTERNAME` from the environment, so it needs no editing, but it has to run from inside the repository **in the human's own interactive session** and register for that same user. `-RunLevel Limited` and `-LogonType Interactive` are load-bearing and both fail in ways that are hard to trace. |
| Choosing the microphone | agent asks the human one thing | Run `--list-devices` yourself, show them the list, and ask which index is the microphone they actually talk into. You cannot hear the room. |
| Finding an app's dictation shortcut | human | It is in that app's own settings or keyboard shortcut list. It is not in this repository and cannot be derived from it. Ask the human for the key by name. A shortcut you found anywhere other than their app is a guess. |
| Confirming the catch-all key | human | `ctrl+space` ships enabled and reaches **every** app that has no profile of its own, so it is the one default that is armed before anybody on this machine has confirmed it. Windows' own documented shortcut for voice typing is `win+h`. Ask the human to focus an ordinary window, run `--test-key --key ctrl+space`, and say whether the dictation bar appeared. If it did not, change `fallback.activate` or turn the catch-all off. Do not leave a key firing into every app on a guess. |
| `python snap_to_dictate.py --whoami` | human | It counts down and then reads whichever window is focused at the end of the count. The human has to click the window they want identified. If you run it, it identifies your own terminal and tells you nothing. Ask them to run it and paste back the process, title and profile lines. |
| `python snap_to_dictate.py --test-key --key <candidate>` | human | The target window has to be focused when the count ends, and somebody has to watch whether the app reacted. `SendInput` reporting success only means the keystroke left this process. Ask them what the app did, and treat "nothing happened" as a wrong key rather than a wrong tool. |
| `python snap_to_dictate.py --calibrate` | human | All seven passes, and every one of them is a performance. They sit quiet, snap ten times from where they sit, snap ten times from across the room, talk for a minute without snapping, type and click and shift in the chair, snap ten pairs, then talk-snap-stop eight times. 237 seconds of recording and about five minutes of their time. Hand them CALIBRATION.md and wait. |
| The `--dry-run` live check | human | Ninety seconds of talking and snapping, per *Live check* in CALIBRATION.md. Same reason. Nothing is sent during it, so it is the safe last step. |

### The shortest path

1. `pip install -r requirements.txt`
2. `python snap_to_dictate.py --verify`. Fix every `FAIL`. If you cannot, stop
   and report which one, because nothing below this line will work.
3. `python test_detector.py`, to know the baseline passed before you touched
   anything.
4. Ask the human which apps they want to drive and what each one's dictation
   shortcut is. This is the single question the whole setup turns on, and it is
   the one thing you cannot look up. An app they do not name is not left out of
   the tool; it falls through to the catch-all in step 8.
5. Ask them to run `--whoami` once per app, clicking that app's window during
   the countdown, and to paste back what it printed.
6. Write a profile per app into `config.json` using those process and title
   values and the shortcut they gave you, with `"enabled": false`.
7. Ask them to focus each window and run `--test-key --key <their key>`, and to
   tell you whether the app reacted. Set `"enabled": true` only for the ones
   that did.
8. Ask them to focus an ordinary window, one no profile claims, run
   `--test-key --key ctrl+space`, and say whether Windows' dictation bar
   appeared. That is the catch-all, and it is the one key that ships armed,
   so it gets confirmed after the fact rather than before. If nothing
   happened, set `fallback.activate` to the key that does work on their
   machine, or `fallback.enabled` to `false`.
9. `python snap_to_dictate.py --verify` again. The terminal check and the
   refusal-list check should both still be `OK`. The waiting-profile note is
   `OK` whenever the catch-all is on, so it will not tell you which profiles
   you enabled. Read the routing line the listener prints at startup for that.
10. Ask whether they want it running at logon. If they do, register the
    scheduled task from *Autostart at logon* in README.md.
11. Leave calibration alone unless snaps are being missed or false ones are
    getting through. If they are, hand them CALIBRATION.md and ask them to run
    `--calibrate`.

### Permissions

All of that needs you to run shell commands, `pip`, `python` and PowerShell, and
to write `config.json`. In Claude Code that is either a confirmation prompt for
each command or a permission mode that stops prompting for each one. Prompting
is slower and every command is read by a person before it runs. Not prompting is
faster and gives that up, on a tool whose entire job is to synthesise keystrokes
into other applications.

**That trade is the human's to make, not yours.** Tell them which commands you
need to run and let them choose how they want to approve them. Do not change
permission settings yourself.

### Never guess a shortcut

Never invent a dictation shortcut and set `"enabled": true` on a profile the
human has not confirmed. A plausible key is not a confirmed key, and a live
config is not the place to find out which it was. A wrong key fired into the
wrong window is exactly what invariant 1 exists to prevent, and enabling a
profile is the act that arms it.

`"enabled": false` with `"activate": null` is a legitimate, tested state. It
means the app is wired up and waiting for somebody to find its shortcut.
`profile_ready()` refuses to send for it and `--verify` reports it as a `WARN`
rather than a `FAIL`, precisely so you can leave it there honestly instead of
filling it in to make a warning go away.

The catch-all is the one exception, and it is worth naming as an exception
rather than letting you find it. `fallback.activate` ships as `ctrl+space`,
enabled, and it reaches **every** app that has no profile of its own. Nobody on
the machine you are setting up has confirmed it. It ships that way because the
alternative, an unwired app doing nothing at all, is the exact thing the feature
exists to remove, and because `ctrl+space` is comparatively cheap to be wrong
about: where it is not Windows voice typing it is usually nothing, an
input-method toggle, or a completion popup. Cheap is not free. In Excel it
selects the whole column.

That reasoning covers `activate` and does not extend to `send`. The catch-all
presses `enter` after a stop, into whatever is in front, and an `enter` in the
wrong window runs a command line or clicks Yes on a prompt. `NEVER_FALLBACK` is
what bounds that, which is why the next section says to add to that list and
never cut from it. Confirming `ctrl+space` with `--test-key` is a step you owe
the human, not one you skip because no warning is showing.

---

## Architecture

Audio arrives in 256-sample blocks at 44.1 kHz, so **5.805 ms per block**. That
is the unit every timing in this program is quantised to. A decay of "52.2 ms"
is nine blocks. There are no intermediate values, and a threshold set between
two multiples of 5.805 behaves exactly like the lower one.

```
microphone
    │  256-sample blocks
    ▼
SnapDetector.push()                  snap_to_dictate.py:258
    │  per-block rFFT, high band 1500-16000 Hz vs an EMA noise floor
    │  ONSET gates  ─► VERIFY gates ─► one event with five features
    ▼
event {peak_db, onset_hf, tail_hf, decay_ms, attack_ms, crest}
    │
    ▼
TriggerGate.offer()                  snap_to_dictate.py:529
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
counting the wrong thing. The event is the unit.

### Where to change what

| You want to change | Go to |
|---|---|
| what counts as a snap | `SnapDetector` gates, `snap_to_dictate.py:258` |
| single vs double, pairing windows | `TriggerGate`, `snap_to_dictate.py:529` |
| which key goes to which app | `resolve_profile`, `snap_to_dictate.py:823` |
| what an app with no profile gets | `fallback_profile`, `snap_to_dictate.py:902` |
| which windows are never typed into | `is_named_window` and `NEVER_FALLBACK`, `snap_to_dictate.py:855` |
| which dictation a snap belongs to | `session_of`, `snap_to_dictate.py:880` |
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
| `process` | executable name, lower-case. `null` to match on title alone |
| `title` | regex against the window title. `null` to match on process alone |
| `mode` | `dictation` means a snap toggles on and off. `oneshot` means a snap fires once, with no stop |
| `activate` | key that starts or stops dictation |
| `send` | key that submits after a stop. `null` for none |
| `enabled` | `false` parks a profile without deleting it |

Matching is **most specific first**. A profile with both `process` and `title`
beats one with only `process`. That is not a nicety. The ChatGPT desktop app
serves its chat window and its Codex window from one executable at one PID, so
only the anchored title `^Codex$` separates them. For the same reason, all focus
comparisons elsewhere in the program are done by **profile name, never by
process**, because two profiles can share an executable.

A disabled profile with `"activate": null` is a legitimate placeholder. The app
is wired up and waiting for somebody to find its dictation shortcut. It does not
sit idle: as of the catch-all below it falls through to Windows' own dictation
instead, and `--verify` says so.

### The catch-all

`fallback_profile` (`snap_to_dictate.py:902`) builds a profile on the spot for
any window that no entry in `profiles` claimed. It ships enabled, so an app
nobody wired up behaves like one that was, using Windows' own voice typing.

```json
"fallback": {
  "name": "Windows voice typing",
  "mode": "dictation",
  "activate": "ctrl+space",
  "send": "enter",
  "enabled": true
}
```

The fields mean what they mean in a profile. There is no `process` or `title`,
because not matching is the point. Three things about it are load-bearing rather
than cosmetic, and each has tests.

**It refuses 17 processes and every window it cannot identify.**
`NEVER_FALLBACK` is the `TERMINALS` list plus seven more: `explorer.exe`,
`consent.exe`, `logonui.exe`, `credentialuibroker.exe`, `taskmgr.exe`,
`lsass.exe`, `winlogon.exe`. Terminals are there for invariant 1. The rest are
there because `enter` in those windows opens the selected icon, clicks Yes on a
UAC prompt, submits a password box or ends a task. `fallback_profile` also
refuses any window it cannot name, and that is wider than an empty string:
`foreground_window` reports its three failures as descriptions rather than
`None`, so the log can say which failure it was, and `<no foreground window>`,
`<pid N: access denied, likely elevated>` and `<pid N: name unavailable>` are
not image names. `is_named_window` rejects them on the angle brackets, which
Win32 forbids in a filename. Before that check existed the catch-all accepted
all three, so a snap while nothing was in front pressed the system key into
whatever Windows routed it to, and the elevated one produced the SendInput
error 5 crashes below. `--verify` re-derives the whole
refusal by calling `fallback_profile` on every name in the list, so it checks
the refusal instead of trusting it. **Add to this list. Do not cut from it.**

**Each window gets its own profile name**, `Windows voice typing [chrome.exe]`.
That is the safety property, not a display detail. `send_key_if_focused`
re-checks focus by comparing profile **names**, so a gesture begun in Chrome and
finished after an alt-tab to Notepad is dropped rather than completed in the
wrong app. One shared name across every unwired app would have removed that
protection silently, because everything would have compared equal.

**Every unwired window shares one session.** `session_of` returns the profile
name for a named app and the shared `fallback` name for the catch-all, and the
state machine's focus-moved reset compares sessions rather than names. Windows
voice typing is one panel for the whole desktop, so alt-tabbing between two
unwired apps is not a new dictation. Comparing names there made it look like
one, and the next snap pressed `ctrl+space` to "start" a panel that was already
open, which closes it. Everything after that is inverted. Keep the two
questions apart: the **name** answers may this keystroke land in this window,
the **session** answers is this the same dictation.

**A profile that cannot press anything falls through to it.** `resolve_profile`
breaks out of its loop when the matched profile fails `profile_ready()`, rather
than returning it, so the parked `Antigravity IDE` and `VS Code` entries now
work through Windows. Two consequences. Enabling a parked profile changes
*which* key is sent, not *whether* one is; and what runs in that window is the
catch-all whole, including its `mode`, so a parked `oneshot` profile still gives
the snap-on, snap-off, double-snap-to-send gesture.

**A keystroke Windows refuses is logged, not fatal.** `SendInput` fails with
error 5 when the foreground window runs at a higher integrity level, and the
catch-all made that reachable from any window rather than only from an app
somebody wired up on purpose. `send_key_guarded` catches it, prints `refused`,
and returns False; every send inside `listen` goes through it or through
`send_key_if_focused`, and a test asserts no bare `send_key` is left on those
paths. Callers treat False as "the app was not touched" and leave state where
it was, so a refused start does not put the loop into `RECORDING` waiting to
stop a dictation that never began.

Set `"enabled": false` on `fallback`, or its `activate` to `null`, and every
window without a profile of its own goes back to being ignored.

---

## Invariants

Break one of these and the tool becomes dangerous rather than merely broken.
Each has a test, so you will find out. Run `python test_detector.py` before
concluding a change is fine.

**1. No profile may ever match a terminal.** `ctrl+d` is dictation in the Claude
desktop app and *end of input in every shell*. A stop that lands in a terminal
closes it, and when that terminal is running an agent, the agent dies mid-task.
The program carries a `TERMINALS` list. `--verify` checks every entry against
the live config, and the test suite checks it against the shipped one.

The catch-all matches every window by construction, so it would have driven
straight through this invariant if nothing stopped it. `fallback_profile`
refuses `NEVER_FALLBACK`, which is `TERMINALS` plus seven more, and refuses any
window whose executable it cannot read. That is checked the same two ways.

**2. A delayed keystroke must re-read focus immediately before pressing.** Three
sends happen later than the focus check that authorised them. A held stop waits
up to `PENDING_TIMEOUT_MS` for the room to go quiet, a submit waits
`send_delay_ms` for the transcript to land, and a manual submit waits for
confirmation. All three go through `send_key_if_focused()`, which re-reads the
foreground window and drops the keystroke if focus moved. Any new delayed send
must use it too. A test counts the call sites and fails if a bare `send_key()`
appears on a deferred path.

**3. One listener at a time.** The named event `Local\SnapToDictate.stop` is
both the singleton lock and the shutdown channel. A second copy exits before it
opens the microphone. Tests use a private event name so they never shut down a
live listener. Do the same in anything new.

**4. Documentation and code must agree.** README.md and CALIBRATION.md once
described commands that did not exist. `--diagnose` was in the setup table for a
while, and CALIBRATION.md described a seven-pass protocol against code that
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

Seven passes: 237 seconds of recording, about five minutes of wall clock once
the prompts between passes are counted. It lands as one WAV plus a
`.passes.json` sidecar marking where each pass starts and ends. It then derives
a config and applies an acceptance gate. **A config is written only if all six
checks pass.** Otherwise `config.json` is left exactly as it was and the
recording is kept, so a fixed derivation can be re-run without asking the user
to perform it again:

```bash
python snap_to_dictate.py --derive calibration/2026-08-22-0436.wav
```

Calibration derives **levels and timings only**, meaning `abs_floor_db`,
`speech_over_floor_db`, the pairing window, and `send_window_ms`. It
deliberately leaves the shape gates (`hf_ratio_min`, `tail_hf_ratio_min`, the
decay bounds) and `pair_refractory_ms` alone. Those describe the physics of a
snap and the mechanics of the detector, not a property of a room, and fitting
them to one unlabelled take made both worse. CALIBRATION.md, *What calibration
does not derive*, has the measurements.

Do not "fix" a failing acceptance check by loosening it. One check was relaxed
in this repository's history and it took three independent measurements to
justify. The reasoning is written into the code beside the check.

---

## Tests

```bash
python test_detector.py
```

No microphone, no network, no fixtures to download. Exit code is non-zero on any
failure. Three kinds of check live in there:

1. **Synthetic audio** through the full detector, so snaps, thumps, speech and
   ticks.
2. **A replay of a labelled field log** against the shipped `config.json`, so a
   tuning change that breaks real ground truth is caught immediately.
3. **Structural assertions**, meaning the invariants above and the documentation
   agreement described in invariant 4.

---

## Known limits

README.md, *Known gaps*, is the honest list. The one most likely to surprise
you: **a hard keystroke near the microphone measures the same as a snap.**
Levels overlap and shape does not separate them, so this is not a tuning problem
and no threshold fixes it. A quiet room fires nothing, but typing right beside
the mic can occasionally send.
