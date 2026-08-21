# Calibration Protocol

How a new user teaches Snap-To-Dictate their snap, their voice, and their room.

Takes about six minutes of the user's time. Produces a `config.json`, a
`config.known-good.json` to fall back to, a recording, and a dated journal
entry so a later calibration can be compared against this one.

---

## Why this exists

Three things vary between one setup and the next, and none of them can be
guessed from a default:

**The room's noise floor.** Across five logged sessions in a single room on a
single machine, the measured floor ranged from **-48.4 dB to -22.8 dB** — a
25 dB swing without changing anything but the time of day. A threshold picked
for the quiet end throws away snaps at the loud end; one picked for the loud
end lets typing through when the room goes quiet.

**The snap itself.** Loudness, brightness and decay depend on finger size, skin
dryness, hand position, and how far the user sits from the microphone. There is
no universal snap.

**The voice.** A finger snap and a plosive consonant overlap on every spectral
feature this tool measures. What separates them is that speech continues and a
snap is followed by quiet — but *how much* quieter, and how loud the speech was
to begin with, is per-person.

The failure this protocol prevents is silent. A gate set slightly too high does
not error; it just quietly ignores some of the user's snaps, and the user
concludes the tool is unreliable. On the developer's own machine, the absolute
floor gate sat **0.3 dB** above the weakest snap it accepted, and was costing
**8 of every 20 deliberate snaps** — discovered only by recording a session and
sweeping the parameter offline.

---

## Before starting

Confirm three things, and stop if any of them is wrong.

| Check | How | If it fails |
|---|---|---|
| The right microphone is selected | `python snap_to_dictate.py --list-devices` | Pick the index and set `device` in `config.json` |
| The target app is running | Task Manager, or `--diagnose` | Start it |
| The activation key is correct for that app | `python snap_to_dictate.py --test-key` | Find the real shortcut before calibrating |

The key check matters more than it looks. `ctrl+d` is dictation in the Claude
desktop app, but it is end-of-input in every terminal — the same keystroke sent
to the wrong window closes it. `target_processes` must name only apps where the
key is safe.

---

## The six passes

Run them in order. Each pass depends on the one before it: the room floor is
needed to interpret the snaps, the snaps are needed to interpret the voice, and
all three are needed before any threshold can be derived.

The whole session is recorded to one WAV. Nothing is decided live — every
threshold is derived afterwards from the recording, so a pass can be re-run
without re-running the others, and two candidate settings can be compared on
identical audio.

### Pass 1 — Room floor · 15 seconds · do nothing

Sit as you normally would and stay quiet. Don't leave the room; the tool is
measuring the room *with you in it*, including your breathing and your chair.

- **Measures:** high-band noise floor, speech-band floor.
- **Records:** `noise_floor_db`, `speech_floor_db`.
- **Fails if:** the floor is above -30 dB (room too loud, or input gain too
  high — turn the gain down and re-run), or below -80 dB (microphone is muted
  or the wrong device is selected).

### Pass 2 — Close snaps · 10 snaps · where you actually sit

The tool prompts once per snap with a short countdown. Snap normally, at your
normal distance, with your normal hand. Don't lean toward the microphone.

- **Measures, per snap:** peak level, onset high-band ratio, tail high-band
  ratio, decay time, attack time.
- **Fails if:** fewer than 8 of 10 register as transients even at the most
  permissive gates. That is a hardware problem, not a tuning problem — the
  microphone is too far away, the gain is too low, or the input is the wrong
  device.

### Pass 3 — Far snaps · 10 snaps · from across the room

Stand up. Snap from the far side of the desk, from the doorway, with your hand
turned away from the microphone. These are deliberately your *weakest* snaps.

This is the pass that stops the absolute floor gate from sitting a fraction of
a decibel above the quietest snap you will ever perform. Without it, the tool
is tuned to snaps made in the best possible position and quietly ignores the
rest.

- **Measures:** the same five features, and specifically the weakest snap that
  should still work.
- **Fails if:** fewer than 6 of 10 register. Below that, the room is too big
  for this microphone and the working range has to be stated honestly to the
  user rather than tuned around.

### Pass 4 — Your voice · 60 seconds · talk, do not snap

Talk naturally and continuously. Read something aloud, or describe your day —
the content does not matter, but it must contain ordinary plosive consonants
(p, t, k, b, d) because those are the sounds that impersonate a snap. Vary your
volume the way you normally would. **Do not snap during this pass.**

Every transient found here is, by definition, a false positive. This is the
negative set the whole stop-side gate is built on.

- **Measures:** every transient that clears permissive gates, and the
  speech-band level 150–300 ms after each one.
- **Records:** the distribution of those levels — this is what sets
  `speech_over_floor_db`.
- **Fails if:** nothing is detected at all. That means the pass was too quiet
  or too short to be a useful negative set; re-run it and talk more.

### Pass 5 — Room noises · 30 seconds · no talking, no snapping

Type on the keyboard. Click the mouse. Shift in the chair. Set a cup down on
the desk. Open and close a drawer. If a door or a fan is part of your room,
include it.

- **Measures:** transients from non-vocal sources.
- **Fails if:** anything here survives the final derived settings. Unlike
  speech, these have no excuse — a keyboard is not a snap on any feature, and
  one getting through means a gate is far too loose.

### Pass 6 — Double snaps · 10 pairs · your natural rhythm

Snap twice, the way you would if you were confirming something. Don't count it
out or force a rhythm; the point is to capture the timing you actually produce
under no pressure.

- **Measures:** the gap between the two snaps in each pair.
- **Records:** the gap distribution — this sets the pairing window and the
  refractory period.
- **Fails if:** the gaps span more than about 600 ms end to end. A window wide
  enough to catch all of them is wide enough for two unrelated speech
  transients to pair up by accident, and the user should be told that rather
  than given a window that misfires.

---

## Deriving the settings

Stated as rules, not as magic numbers, so any resulting config can be audited
against the recording that produced it.

| Setting | Rule |
|---|---|
| `abs_floor_db` | weakest snap across passes 2 and 3, minus 4 dB — but never below the room floor plus 12 dB |
| `hf_ratio_min` | 5th percentile of snap onset high-band ratio, minus 0.03 |
| `tail_hf_ratio_min` | 5th percentile of snap tail high-band ratio, minus 0.03 |
| `min_decay_ms` / `max_decay_ms` | the snap decay range, widened 25% on each side |
| `speech_over_floor_db` | highest post-snap quiet level, plus 3 dB |
| `double_min_ms` / `double_max_ms` | 5th and 95th percentile of the pass 6 gaps |
| `send_window_ms` | 95th percentile double-snap gap, rounded up to the next 250 ms |
| `refractory_ms` | just under the fastest gap seen in pass 6 |

Two of these deserve their asymmetry spelled out.

**`abs_floor_db` is set from the weakest snap, not the average.** The two
mistakes are not equal in cost. A gate set too low lets in an occasional room
noise, which the later gates then reject. A gate set too high silently drops
real snaps, and nothing in the log says so — the user just sees the tool "not
working sometimes".

**`speech_over_floor_db` is set above the loudest snap, not midway to speech.**
A stop that fires when it should not have cuts the user's sentence in half,
which is the exact failure the gate exists to prevent. A stop that is refused
costs one more snap. Placing the threshold near the snap side buys reliability
at the price of an occasional repeat, which is the right trade.

---

## The acceptance gate

Calibration writes a config only if all five hold. If any fails, it reports
which one and why, and leaves the previous config alone.

| # | Requirement | Source |
|---|---|---|
| 1 | At least 9 of 10 close snaps detected | Pass 2 |
| 2 | At least 8 of 10 far snaps detected | Pass 3 |
| 3 | Zero triggers from room noise | Pass 5 |
| 4 | At most 1 stop survives 60 s of speech | Pass 4 |
| 5 | At least 6 dB between the loudest post-snap quiet and the quietest speech transient | Passes 2–4 |

Requirement 5 is the one that can genuinely be unsatisfiable. If a user's voice
and a user's snap overlap with no margin, no threshold separates them, and the
honest output is to say so and offer the alternatives — move the microphone,
snap closer, or accept a manual stop key — rather than to pick a number and let
them discover the problem mid-sentence.

---

## Live check

Ninety seconds in `--dry-run`, so nothing is actually sent while checking.

1. Talk for 20 seconds. Expect **no** triggers.
2. Snap once. Expect **start**.
3. Talk for 20 seconds. Expect **no** triggers.
4. Snap once, pause, then snap twice. Expect **stop**, then **send**.

If step 1 or step 3 produces a trigger, the calibration passed on the recording
but not in the room — usually because the live room is louder than it was
during pass 1. Re-run pass 1 and pass 4 at the time of day the tool will
actually be used.

---

## The journal

Every calibration writes `calibration/YYYY-MM-DD-HHMM.json`:

```json
{
  "when": "2026-08-21T23:40:12",
  "device": "Microphone Array (Realtek)",
  "room": { "noise_floor_db": -44.1, "speech_floor_db": -51.8 },
  "snaps_close": { "n": 10, "detected": 10, "peak_db": [-8.2, "..."] },
  "snaps_far":   { "n": 10, "detected": 9,  "peak_db": [-19.4, "..."] },
  "speech":      { "seconds": 60, "transients": 7, "levels_db": [28.0, "..."] },
  "noises":      { "seconds": 30, "transients": 0 },
  "doubles":     { "n": 10, "gaps_ms": [210, 260, "..."] },
  "derived":     { "abs_floor_db": -25.0, "speech_over_floor_db": 14.0 },
  "acceptance":  { "passed": true, "margin_db": 8.2 },
  "recording":   "calibration/2026-08-21-2340.wav"
}
```

The recording is kept alongside it. That combination is what makes a later
question answerable: when the tool starts missing snaps six months from now, a
new calibration can be diffed against this one to say whether the room got
louder, the microphone moved, or the snap changed — instead of guessing.

---

## When to recalibrate

- A new microphone, or the existing one moved more than an arm's length.
- A new room, or the same room after furniture changed.
- The measured floor has drifted more than 10 dB from the journal value. The
  tool can watch for this and say so on its own.
- Snaps start being missed and a `--dry-run` confirms they are not registering.

Recalibration is cheap and non-destructive: the previous config stays in
`config.known-good.json`, and `--restore` puts it back.
