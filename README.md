# Snap-To-Dictate

Snap your fingers to start and stop dictation in the Claude desktop app.

The app has no audio trigger of its own — dictation is bound to a **key**
(Ctrl+D). So the job splits in two:

1. A background listener watches the mic for a finger-snap transient.
2. When it hears one, it synthesises Ctrl+D into the Claude app window.

| Gesture | What happens |
|---|---|
| **Snap** | dictation starts recording |
| **Snap again** | dictation stops. Nothing is sent. |
| **Snap twice, quickly** | dictation stops, then the message is submitted |

The whole cycle is hands-free; the keyboard is never touched. Every state has
exactly one snap leading out of it, so a snap is never ambiguous and you can
never get stuck — see [The three states](#the-three-states).

A logon scheduled task starts the listener, which then stays idle with the
microphone closed until the Claude app is actually running — see
[Autostart at logon](#autostart-at-logon).

---

## How it works

```
mic ──► SnapDetector ──► focus guard ──► classify ──► SendInput ──► Claude app
        (is it a snap?)   (is Claude in    (start, stop   (ctrl+d, and   (record, then
                           front?)          or send?)      then enter)    submit)
```

The focus guard runs *before* `classify`, not after. A snap that lands while
another app is in front has nothing to do with this program, and letting it
reach the trigger gate would burn the cooldown — so the next snap, the real
one, would be swallowed.

### SnapDetector

Every 5.8 ms block of audio is FFT'd and reduced to two numbers: energy in the
1.5–16 kHz band, and that band's share of total energy. Detection runs in two
stages.

**Onset gates** decide whether a block is worth following at all:

| Gate | Check | Rejects |
|---|---|---|
| loud | HF energy above the tracked noise floor *and* `abs_floor_db` | room tone, breathing |
| sharp | HF energy ≫ the previous block's | anything that fades in |
| bright | HF share ≥ `hf_ratio_min` | speech, door thuds, chair scrapes |

**Verify gates** then watch the tail, which is where the real discrimination
happens:

| Gate | Check | Rejects |
|---|---|---|
| not too fast | takes ≥ `min_decay_ms` to fall to 8 % of peak | key clicks, mouth ticks |
| not too slow | gets there within `max_decay_ms` | music, sustained noise, held vowels |
| still bright | HF share at that moment ≥ `tail_hf_ratio_min` | knocks and pops with a bright onset but a low-frequency body |

The noise floor is an exponential moving average updated only on quiet blocks,
so it follows a fan turning on without slowly desensitising itself to snaps.

### Why the tail matters more than the level

The first version gated on level alone, and it was wrong. In a labelled
dry-run log from this machine — 4 real snaps, 13 assorted non-snap noises —
peak level *overlapped*: a non-snap reached 17.8 dB while a real snap sat at
11.9 dB. No level threshold separates those.

The tail does, and it is not close. Across 32 labelled snaps, every one stayed
bright as it faded (`tail_hf` 0.66–0.98) and took 34–76 ms to get there. Among
the non-snaps that lasted long enough to reach the decay gate at all, the
highest `tail_hf` was **0.14**. That is a gap of half the scale, on a feature
that costs one FFT to compute.

Because the tail decides, the level gate does not have to. A second labelled run
— 28 snaps, zero false positives, but roughly 30 % of snaps missed — showed the
level floor was the thing rejecting the quiet ones: the weakest accepted snap
was 5.4 dB against a floor of 4.0. Dropping the floor to −20 dB and
`min_decay_ms` to 20 buys **25 dB** of level headroom — about four doublings of
distance from the mic in free field, less in a live room.

That headroom has a price, and running as a background service is what exposed
it. Two minutes of an idle room with nobody snapping produced six triggers, at
−3 to −12 dB with `tail_hf` between 0.50 and 0.63. They cluster:

| | peak | `tail_hf` | decay |
|---|---|---|---|
| 32 labelled snaps | 5.4 … 28.8 dB | **0.66 … 0.98** | 35 … 76 ms |
| 6 idle-room leaks | −12.5 … −3.1 dB | **0.50 … 0.63** | 35 … 110 ms |

Level separates them too, but level is exactly the gate that caused the 30 %
miss rate, and genuine across-the-room snaps do land below 0 dB. So the fix goes
on the tail instead: `tail_hf_ratio_min` sits at **0.65**, inside the measured
0.63 → 0.66 gap and biased 0.02 toward rejecting. In an always-on listener a
false trigger grabs the microphone mid-sentence, while a miss costs one more
snap — so when the two errors are this close together, prefer the miss.

The margin either side is about 0.02, so this one number is the thing to revisit
first if behaviour changes. Lower it toward 0.55 if snaps start getting missed;
raise it toward 0.70 if noise gets through. `test_detector.py` replays all 32
snaps and all 19 non-snaps on every run and prints the remaining margin, so a
future tightening shows up as a shrinking number.

If false positives still get through, switch to double-snap mode (`--double`).
Two snaps 120–700 ms apart is a much rarer accident.

### The three states

```
                snap                    snap                  snap
    ┌──────┐  ────────►  ┌───────────┐  ────────►  ┌──────────┐  ───────►  ┌──────┐
    │ IDLE │             │ RECORDING │             │ SETTLING │            │ IDLE │
    └──────┘             └───────────┘             └──────────┘            └──────┘
       ▲                    ctrl+d                    ctrl+d                 enter
       │                                                 │
       └─────────────────────────────────────────────────┘
                    no snap within send_window_ms
```

`SETTLING` is the only state this program invented. Dictation is already off;
what is being decided is whether to submit. It lasts `send_window_ms` (1000 ms)
and then lapses back to `IDLE` on its own, so a snap a few seconds after a stop
reads as "start again" rather than "send".

The app shows and sounds dictation starting and stopping, so those two states
need no help from here. `SETTLING` shows nothing, which is a real gap — but an
audible chirp was tried and removed: it added noise during dictation for no
benefit the user wanted. If something fills this gap later it should be visual,
and it should be outside the microphone's world.

### Why only sending needs two snaps

The three actions are not equally cheap to get wrong. A false *start* or *stop*
toggles a microphone — you see it happen and snap again. A false *send* puts a
message in front of Claude that you never wrote and cannot be taken back.

So sending is the only action that needs a second snap, and that snap is also
judged against a stricter tail (`strict_tail_hf_ratio_min` 0.70 vs 0.65). The
pairing is what actually carries the guarantee, and it is worth being clear
about why: **it does not depend on any threshold being right.** A stray
transient has to land inside a one-second window that opens only after a
deliberate stop, while the room is quiet because you just finished speaking.

The price is true negatives — a real send snap is sometimes refused, and you
snap again. That trade was chosen on purpose.

#### What the earlier design got wrong

Stopping used to require the double snap too. On paper that was safer. In
practice it was the single worst failure this program has had, because **a
missed pair left no way out at all.** One logged session:

```
19:18:05  dictation ON
19:18:08  ...13 snaps, none of them 120-700 ms apart...
19:23:54  nothing for 180s; assuming dictation already stopped.
```

Nearly six minutes with the microphone open, thirteen correctly-detected snaps,
and nothing to show for it — the state machine only recovered when a timer
noticed. Every one of those snaps was real; the detector was never the problem.
The gaps were 1.0–1.8 s, because that is the rhythm of snapping twice when the
first one appears to have done nothing.

Two thresholds turned out to be costing recall for nothing, both found by
replaying the logs:

| Setting | Problem | Now |
|---|---|---|
| `strict_min_decay_ms` 30 | Decay is counted in whole 5.8 ms blocks, so it lands only on multiples of 5.8. 30 ms sat between the 5-block (29.0) and 6-block (34.8) steps. | removed |
| `strict_max_decay_ms` 120 | Cut off the long-tail end of real far-field snaps (measured up to 157 ms). | removed |
| `refractory_ms` 220 | Floors to 37 blocks = **214.8 ms** of hard deafness, which silently made the advertised `double_min_ms` of 120 ms unreachable — the bottom 45% of the pairing window did not exist. | 160 |

Between them the strict decay bounds refused **21 real snaps** across two
logged sessions and never once caught a non-snap. Those 21 are now part of
`FIELD_SNAPS` in `test_detector.py`, which is why the labelled set jumped from
37 to 58: they are the far-field, off-axis end of the distribution, exactly the
part a gate tuned on close-mic samples does not know about.

What survives is `strict_tail_hf_ratio_min` 0.70. The tests state its cost
rather than assuming it: it still refuses 9 of the 58 labelled snaps, all
sitting at `tail_hf` 0.65–0.68.

### The mid-sentence cut-off

Dictation sometimes switches itself off while you are still talking. **While
recording, the microphone is full of your voice**, not room tone, and every
threshold here was tuned on a quiet room; some plosive or mouth click clears
the gates and toggles Ctrl+D.

Measured rather than assumed: a dry-run capture of 95 seconds of ordinary
talking, with no snapping at all, produced 24 detections. Roughly one unwanted
stop every four seconds of speech.

#### Why no threshold fixes it

The four features the detector had — peak level, onset brightness, tail
brightness, decay time — do not separate the two classes. Against nine
high-confidence real snaps, **seven had a speech transient that was at least as
snap-like on all four at once**:

```
peak    6.3  onset_hf 0.97  tail_hf 0.99  decay  58.0   <- speech
peak   -3.1  onset_hf 0.98  tail_hf 0.87  decay  87.1   <- a real snap
```

Raising any gate high enough to reject the first also rejects the second. Attack
time was measured next, on the theory that a snap rises in under a millisecond
while speech needs milliseconds to move an airway. It is a better feature than
the other four, but on labelled data it still cost more than half the real snaps
to clear the speech, so it is recorded in the log and not gated on.

#### What does separate them

Not the sound. What comes after it.

A snap is followed by quiet. A plosive is followed by the rest of the sentence.
So a stop is held for a moment and the speech band is read again once the
transient has passed — `speech_window_ms`, 150 to 300 ms after the onset. Loud
means the speaker never stopped, so it was not a snap.

On a labelled recording (`session.wav`, 350 s, split by the 27-second silence
its owner left between talking and snapping):

| | speech transients | deliberate snaps |
|---|---|---|
| n | 7 | 12 |
| speech level 150–300 ms later | 22.2–28.0 dB, plus one at 4.2 | 4.1–10.6 dB |
| verdict at the shipped 14 dB | **6 of 7 refused** | **12 of 12 allowed** |

The survivor read 4.2 dB — quiet on both sides, so by this measure it was not
speech at all but a click in a pause. The test separates transients that
interrupt speech, not every stray noise.

Two details carry the result:

- **Only stopping is checked.** After a deliberate stop the speaker falls
  silent; after a deliberate start they begin talking immediately. The same test
  on a start would reject exactly the snaps it exists to pass.
- **The floor falls fast and rises slowly** (`speech_floor_fall` 0.25,
  `speech_floor_rise` 0.0001). A gated average that only learns from quiet
  blocks can latch onto near-silence at startup and then never move again, which
  it did: every reading came back as hundreds of dB above a floor stuck at zero.
  Sweeping the two rates against the recording moved the gap between the classes
  from 4.1 dB to 11.4 dB.

The threshold sits at 14 dB, above the loudest labelled snap rather than halfway
between the classes. The two mistakes are not equal: a refused stop costs one
more snap, a stop that should have been refused cuts the sentence off.

The cost is latency — a stop now lands about 300 ms after the snap. A snap that
arrives while a stop is being held is not read as a second stop; it is kept as
the send confirmation it was meant to be, so the double-snap gesture is
unchanged.

Rejected transients are still written to `snap.log`, so if it starts happening
there is data to tune against instead of guesswork.

Two further guards limit the damage:

- `min_recording_ms` (700 ms) ignores a stop snap that soon after starting, so
  three fast snaps cannot start, stop and send in under a second — which would
  put whatever was already in the composer in front of Claude.
- `recording_max_s` (180 s) assumes the app stopped on its own if nothing has
  happened for that long, so a dictation that self-terminated on silence cannot
  leave the listener a full cycle out of step.

---

## Setup

### 1. Dependencies

```bash
pip install -r requirements.txt
```

### 2. Leave the allowlist alone

The window in front decides what a snap means. `config.json` carries a list of
`profiles`, and the first one whose process **and** title both match wins:

```json
{"name": "Claude desktop", "process": "claude.exe", "title": null,
 "mode": "dictation", "activate": "ctrl+d", "send": "enter", "enabled": true}
```

A window that matches nothing is ignored, and that narrowness is the safety
mechanism, not an oversight. Ctrl+D is the Claude app's dictation toggle, but it
is also **end-of-input in every terminal**, and it is how the Claude Code CLI
quits — twice within 800 ms and the session is gone. The listener fires on a
*sound*, and a sound has no idea what has focus. So the guard has to be a list
of windows where the key means what we think it means.

Widening it to terminals — as an earlier revision did, back when the key was
Alt+K and harmless — turns a false positive from a no-op into a lost session.
`test_detector.py` asserts a terminal matches no profile.

#### Why the title is not optional

Measured on one machine with every app open at once:

| Window | Process | PID | Title |
|---|---|---|---|
| Claude desktop | `claude.exe` | 19648 | `Claude` |
| ChatGPT | `ChatGPT.exe` | 28928 | `ChatGPT` |
| **Codex** | `ChatGPT.exe` | **28928** | `Codex` |
| Antigravity IDE | `Antigravity IDE.exe` | 7356 | `CODEX - Antigravity IDE` |

The ChatGPT desktop app serves its chat window and its Codex window from **one
process at one PID**. Neither the image name nor the PID separates them; the
title is the only thing that does.

Title patterns are anchored (`^Codex$`) for the reason the last row shows: an
editor sitting on a folder named CODEX has "CODEX" in its title, and an
unanchored substring match would route it to Codex and press Codex's shortcut
into an editor.

#### What is wired

| Window | Matched by | Gesture | Sends |
|---|---|---|---|
| Claude desktop | `claude.exe` | snap on / snap off / snap twice submits | `ctrl+d`, then `enter` |
| Codex | `chatgpt.exe` + `^Codex$` | one snap | `ctrl+b` |
| ChatGPT | `chatgpt.exe` + `^ChatGPT$` | one snap | `ctrl+b` |
| Antigravity | `antigravity.exe` | snap on / snap off / snap twice submits | `ctrl+m`, then `enter` |
| Antigravity IDE | `antigravity ide.exe` | — | nothing, deliberately |
| VS Code | `code.exe` | — | nothing, deliberately |
| Anything else | — | — | ignored |

Antigravity's `Ctrl+M` is dictation, not a conversational agent — it types
into the composer the way Claude's `Ctrl+D` does — so it takes the full gesture
and the stop-side silence check that comes with it.

Antigravity ships as **two separate programs** with two executables, both
installed here. Only the desktop app is wired. The IDE is VS Code based, where
`Ctrl+M` is the accessibility "tab moves focus" toggle rather than anything to
do with voice — so letting it inherit the desktop app's key would fire a real
but unrelated command. Matching is on the whole image name, never a prefix, so
it cannot happen by accident.

#### Two gesture modes

| Mode | Gesture | For |
|---|---|---|
| `dictation` | snap starts, snap stops, second snap submits | anything that transcribes into a composer |
| `oneshot` | one snap presses `activate`, nothing else | a voice agent that listens and replies on its own |

Only `dictation` runs the stop-side silence check, because only `dictation` has
a stop to protect.

#### A profile is not permission

`"enabled": false`, or `"activate": null`, means nothing is ever sent — the log
says what it *would* have done. Unknown shortcuts stay unset rather than
guessed, because a wrong keystroke fired into a window that happens to be a
terminal is exactly what the routing exists to prevent.

To confirm a candidate, focus the window and try it deliberately:

```bash
python snap_to_dictate.py --test-key --key ctrl+m
```

That still requires the window to match a profile, so a candidate key cannot be
fired into an app this tool was never pointed at. Once the app reacts, set
`"enabled": true`.

#### The one window that hosts both

The Claude desktop app shows **Home** (chat) and **Code** (Claude Code) in the
same window and the same process, so the focus guard cannot tell them apart:
`claude.exe` is `claude.exe`. That would be alarming if the Code tab were a
terminal. It is not — the app runs the CLI headless:

```
claude.exe --output-format stream-json --verbose --input-format stream-json
```

There is no TUI on the other end of that pipe and no key handling at all; the
app speaks to it in JSON over stdin/stdout. A stray Ctrl+D on the Code tab
reaches the Electron renderer, exactly as it would on the Home tab. It cannot
exit anything.

### 3. Calibrate against your room and your snap

The shipped `config.json` is already tuned from a labelled log on this machine,
so this step is only needed if you change mic, room, or snapping hand.

```bash
python snap_to_dictate.py --calibrate
```

Three seconds of silence, then five snaps. It rewrites `noise_ratio_thresh` and
`abs_floor_db`, placing the level threshold halfway (in dB) between your noise
floor and your weakest snap. It leaves the tail gates alone.

### 4. Confirm the process name

```bash
python snap_to_dictate.py --whoami
```

Click the window you want to check during the countdown. It reports the process
name, the window title, which profile claims it, and what a snap there would
actually send — including "NOTHING" and why, when the profile matched but has no
confirmed shortcut. If no profile claims it, it prints the `process` and `title`
lines to paste into `config.json`.

### 5. Watch it before letting it type

```bash
python snap_to_dictate.py --dry-run
```

Snap, type, talk, move your chair. Every detection is logged with peak level,
noise floor, HF ratio and decay time, but no key is sent. Tune `config.json`
until only real snaps say `TRIGGER`.

### 6. Run it

```bash
python snap_to_dictate.py
```

Or `run.bat`, which launches it minimised.

---

## Autostart at logon

A scheduled task starts the listener when you log in:

```powershell
$pw  = "C:\Users\valla\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\pythonw.exe"
$act = New-ScheduledTaskAction -Execute $pw -Argument '"D:\CLAUDE\Snap-To-Dictate\autostart.py"' -WorkingDirectory "D:\CLAUDE\Snap-To-Dictate"
$trg = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trg.Delay = "PT20S"
$prn = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName SnapToDictate -Action $act -Trigger $trg -Principal $prn -Force
```

Two properties of that principal are load-bearing, and both fail silently-ish
if you get them wrong:

| Setting | Why |
|---|---|
| `-RunLevel Limited` | Windows blocks synthetic input across integrity levels. The Claude app is a Store package and runs unelevated, so an *elevated* listener could not type into it. |
| `-LogonType Interactive` | A task set to run "whether the user is logged on or not" lands in session 0, which has no desktop. There `GetForegroundWindow` returns NULL and `SendInput` fails with error 5. |

`autostart.py` exists only to give the listener a log file and a detached
parent — a scheduled task cannot redirect stdout, and a task that stays
`Running` for the whole logon session is harder to reason about than one that
fires and finishes. It spawns with `DETACHED_PROCESS` and
`CREATE_BREAKAWAY_FROM_JOB` so the listener is not reaped along with it.

### Why the Claude Code hook was the wrong trigger

An earlier revision started the listener from Claude Code's `SessionStart` hook.
That hook fires only when a *Claude Code* session begins — precisely the product
this is not for. Open the desktop app, stay on the Home tab, and it never runs
at all. The logon task has no such blind spot, and it costs nothing while
waiting because the listener does not open the microphone until Claude appears.

Three mechanisms keep exactly one listener alive for exactly as long as it is
wanted:

| | Mechanism |
|---|---|
| Only one copy | The listener creates the named event `Local\SnapToDictate.stop`. A second copy sees `ERROR_ALREADY_EXISTS` and exits before opening the microphone. |
| Stops on request | `--stop` opens that same event and signals it; the listener notices within 5 s and shuts down cleanly. |
| Sleeps when idle | `--follow` closes the microphone whenever none of the wired apps is running and reopens it when one appears, so the recording indicator is lit only when it could be useful. That list is derived from the profiles, never configured beside them — a second hand-kept list of the same apps is one that drifts. |

A named event was chosen over a PID file because the kernel destroys it when the
last handle closes. A listener that is killed outright leaves nothing stale to
second-guess.

The lock is **unconditional** — there is no flag to waive it. It used to be
opt-in behind `--singleton`, and that default cost a live session: two listeners
ran on one microphone, each sent its own keystroke, and every snap arrived twice
— a dictation toggle turned on and straight back off. Nothing in either log
looked wrong, because from inside either process nothing was. Only the two logs
side by side showed the same timestamp firing twice.

There is no case where a second listener is what anyone wanted. The one reason
that sounds plausible — running two configs to compare them — is what `--replay`
already does, and does better: identical recorded audio, deterministic, no
contention for the microphone.

```bash
python snap_to_dictate.py --replay session.wav
```

`--singleton` is still accepted and does nothing. The scheduled task registered
by `autostart.py` already carries it, and removing the flag would break that
task at the next logon with nothing on screen to explain why.

Under `pythonw` there is no console, so the listener's output goes to
`snap.log` next to the script. That is where to look if a snap does not fire.

```bash
python snap_to_dictate.py --stop
```

## Usage notes

- **Two programs share the mic.** This listener and the Claude app's recorder
  both capture from the same device in WASAPI shared mode. If one of them fails
  to open the device, point this listener at a *different* physical mic with
  `--device N` (`--list-devices` to see them).
- **Elevation has to match.** Windows blocks synthetic input across integrity
  levels, in both directions. The Claude app runs unelevated, so the listener
  must too — which is why the task is registered `-RunLevel Limited`.
- **A snap while another window has focus does nothing.** It is still written to
  `snap.log`, marked `skipped`, along with the process that had focus. That log
  line is the first thing to read when a snap "did not work".
- **A rejected transient costs 12 ms of deafness, not 160.** `refractory_ms`
  applies only after an accepted snap; `reject_refractory_ms` covers rejections,
  so a snap that lands right after a cough or a keystroke is not swallowed.
- **The send snap has to be quick.** Not "snap, then snap" — one gesture, both
  snaps inside `send_window_ms` (1000 ms). Measured double snaps land at
  76–989 ms; the same person snapping twice *casually* lands at 2–7 s, which
  reads as stop-then-start-again and is why the window is where it is.
- **Keep a fallback whenever a tuning works.** `--save-good` copies the current
  `config.json` to `config.known-good.json`; `--restore` puts it back. Tuning by
  hand is cheap to try and expensive to lose.
- **Stopping without sending is just one snap.** Snap to stop, then leave it
  alone. After the window lapses nothing is submitted.
- **`send_delay_ms` is a race, not a certainty.** Ctrl+D stops recording, but
  the final transcript lands a moment later, so Enter waits 1500 ms *from the
  Ctrl+D* — not from the confirming snap. Too short and you submit a
  half-finished message; lengthen it before shortening it.
- **The listener cannot see the app's state.** If dictation stops on its own
  after a silence, the listener still believes it is recording until
  `recording_max_s` (180 s) passes, so the next snap stops something that had
  already stopped. Windows does publish the answer — see
  [Known gaps](#known-gaps).

## Files

| File | Purpose |
|---|---|
| `snap_to_dictate.py` | Detector, trigger gate, focus guard, key injection, lifecycle |
| `autostart.py` | Detached launcher, called by the logon scheduled task |
| `config.json` | Thresholds — regenerated by `--calibrate` |
| `config.known-good.json` | Last tuning confirmed to work; `--save-good` / `--restore` |
| `test_detector.py` | Offline tests: synthetic audio, field-log replay, lifecycle |
| `run.bat` | Minimised launcher, for running it by hand |
| `snap.log` | Where the background listener's output goes |
| `session.wav` | A labelled recording, kept so tuning can be re-run without a mic |

```bash
python test_detector.py
```

## Tuning reference

| Key | Effect | Raise it when |
|---|---|---|
| `tail_hf_ratio_min` | how bright the transient still is when it dies | knocks, pops, mouth noises trigger it |
| `min_decay_ms` | how long it must take to fade | key clicks or ticks trigger it |
| `abs_floor_db` | hard level floor | quiet sounds trigger it |
| `noise_ratio_thresh` | margin above the tracked noise floor | background noise triggers it |
| `hf_ratio_min` | how bright the onset must be | voice or thumps trigger it |
| `attack_ratio` | how abrupt the onset must be | gradual sounds trigger it |
| `require_double` | demand two snaps to *start* | nothing else works |

Stop-side keys — the silence check that decides whether a stop was real:

| Key | Effect |
|---|---|
| `confirm_stop_with_silence` | hold a stop until the room has had its say; turn off to get the old instant stop back |
| `speech_over_floor_db` | how loud the speech band may be afterwards and still count as a snap. **Lower it if dictation still cuts off mid-sentence; raise it if genuine stops are being refused** |
| `speech_window_ms` | when to listen, measured from the onset. Later is a cleaner read but a slower stop |
| `speech_band_hz` | where voiced speech lives; a finger snap has nothing here |
| `speech_floor_fall` / `speech_floor_rise` | how fast the floor follows the room down and up. Swept against `session.wav`; leave alone unless you re-sweep |

Send-side keys — the gate that decides whether a message actually goes out:

| Key | Effect |
|---|---|
| `send_window_ms` | how long after a stop a second snap still means "send" |
| `strict_tail_hf_ratio_min` | how bright that confirming snap must still be as it dies |
| `send_key` | what is pressed to submit |
| `send_delay_ms` | how long after Ctrl+D to wait for the final transcript |
| `min_recording_ms` | how long dictation must run before a stop snap counts |
| `recording_max_s` | assume the app stopped on its own after this long |

Lower `max_decay_ms` when claps or coughs get through.

## Known gaps

Honest list of what this does not do yet.

- **The state machine is open-loop.** Nothing tells the listener whether the app
  is actually recording. Windows does publish it, per-app and in real time, at
  `HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\Claude_*`
  — `LastUsedTimeStop` reads `0` while the mic is open. Reading that would close
  the loop and give `send_delay_ms` a real anchor instead of a fixed timer.
- **No error handling around the audio stream.** An unplugged mic, a device
  claimed in exclusive mode, or a driver reset on sleep/resume will kill the
  listener with a traceback in `snap.log` and no restart.
- **The mic stays open on a locked session.** `SendInput` cannot reach the
  secure desktop anyway, so the listener should release the device on
  `WTS_SESSION_LOCK` and does not.
- **Nothing tells you the send window is open.** The app cannot show it, and an
  audible cue turned out to be worse than the gap it filled.
- **Attack time is measured but not used.** A ring buffer now records the raw
  samples and every detection logs its 10–90% rise and crest factor. On labelled
  data attack time beat all four spectral features, but a gate tight enough to
  clear speech still threw away more than half the real snaps, so it stays a
  column in the log rather than a threshold. Claps are still not separated.
- **The silence check needs the speaker to actually stop.** It catches
  transients that interrupt speech, which is the failure that was happening. A
  stray click in a genuine pause is indistinguishable from a snap by this test,
  and one such event in the labelled set got through.
- **One room, one voice, one session.** The gap between the classes is 11.4 dB
  and the threshold has 3.4 dB of headroom on the snap side, but that is 19
  labelled events from a single recording. A noisy room, a different mic, or a
  quieter talker have not been tried.
- **The false-positive rate is not really known.** Zero false sends over about
  fifteen minutes of exposure supports, by the rule of three, only "fewer than
  12 per hour". A weekend of logging would turn that into a number worth
  printing.
- **Windows only**, and hard-coded to one machine's paths.

Lifecycle keys, used only under `--follow`:

| Key | Effect |
|---|---|
| `watch_grace_s` | how long they must stay gone before the mic is released |
| `watch_poll_s` | how often the process list is checked |

Lower them if genuine snaps are being missed — `--dry-run` prints the measured
value of every gate for each detection, so tune against the numbers, not guesses.

Better still, record once and tune offline as many times as you like:

```bash
python snap_to_dictate.py --dry-run --record session.wav
```
```bash
python snap_to_dictate.py --replay session.wav
```

`--record` saves every block the detector hears; `--replay` runs that file back
through the current `config.json` and prints each detection with its silence
verdict. Two thresholds can then be compared on identical audio, which is the
only comparison that means anything — a fresh performance differs in a dozen
uncontrolled ways. Leaving a clear gap of silence between the sounds you want
detected and the ones you do not makes the recording self-labelling.
