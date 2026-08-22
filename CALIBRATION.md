# Calibration Protocol

How a new user teaches Snap-To-Dictate their snap, their voice, and their room.

Takes about five minutes of the user's time. Produces a `config.json`, a
`config.known-good.json` to fall back to, a recording, and a dated journal
entry so a later calibration can be compared against this one.

Run it with `python snap_to_dictate.py --calibrate`. To re-derive from a
recording made earlier, without performing it again, use
`python snap_to_dictate.py --derive calibration/<stamp>.wav`.

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
| The target app is running | Task Manager, or `--whoami` with the app in front | Start it |
| The activation key is correct for that app | `python snap_to_dictate.py --test-key` | Find the real shortcut before calibrating |

The key check matters more than it looks. `ctrl+d` is dictation in the Claude
desktop app, but it is end-of-input in every terminal — the same keystroke sent
to the wrong window closes it. `target_processes` must name only apps where the
key is safe.

---

## The seven passes

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
- **Fails if:** more than two survive the final derived settings.

  This used to demand zero, on the reasoning that a keyboard is not a snap on
  any feature. That reasoning was wrong, and the first real recording disproved
  it: a transient in this pass measured onset_hf **0.99**, tail_hf **0.90**,
  decay **52 ms** — brighter and better shaped than most of the genuine snaps
  in the same session. A mouse click and a finger snap are the same event to
  this detector and no threshold separates them. What the pass still catches is
  a gate that is far too loose; the first derived config let **26** through.

### Pass 6 — Double snaps · 10 pairs · your natural rhythm

Snap twice, the way you would if you were confirming something. Don't count it
out or force a rhythm; the point is to capture the timing you actually produce
under no pressure.

- **Measures:** the gap between the two snaps in each pair.
- **Records:** the gap distribution — this sets the pairing window and the
  paired refractory.
- **Fails if:** the gaps span more than about 600 ms end to end. A window wide
  enough to catch all of them is wide enough for two unrelated speech
  transients to pair up by accident, and the user should be told that rather
  than given a window that misfires.

### Pass 7 — Stop gesture · 8 repetitions · talk, snap, stop

Talk for a few seconds, snap once, and stop talking immediately. Repeat eight
times. This is the only pass that performs the gesture the tool actually has to
judge, and it exists because of a measurement the first six passes could not
make honestly.

`speech_db` reports a level in dB *above a running floor*, and that floor
follows the room. On the first real recording it sat at **-43 dB** through the
snap passes and **-5.4 dB** while talking — a 38 dB difference. Numbers taken
from those two passes therefore have no common baseline, and comparing them
reported a **-3.5 dB** margin for audio actually separated by about 34 dB.

Here both sides come from the same condition: the speaker has been talking, so
the floor is where it will be in real use, and the level after the snap is
measured against it.

- **Measures:** the speech-band level after each stop snap, against the
  talking floor.
- **Records:** that distribution — this is what sets `speech_over_floor_db`.
- **Fails if:** fewer than 6 of the 8 register, or the pass is missing. A
  recording without it cannot answer requirement 5 and no config is written.

---

## Deriving the settings

Stated as rules, not as magic numbers, so any resulting config can be audited
against the recording that produced it.

| Setting | Rule |
|---|---|
| `abs_floor_db` | weakest snap across passes 2 and 3, minus 4 dB — but never below the room floor plus 12 dB |
| `speech_over_floor_db` | highest post-snap quiet level **in pass 7**, plus 3 dB — only if at least 6 of the 8 reps ended in silence |
| `double_min_ms` / `double_max_ms` | 5th percentile of the pass 6 gaps × 0.7, and the 95th × 1.4 |
| `send_window_ms` | 95th percentile double-snap gap, rounded up to the next 250 ms |

### What calibration does not derive

`hf_ratio_min`, `tail_hf_ratio_min`, `min_decay_ms`, `max_decay_ms` and
`pair_refractory_ms` are **left at their shipped values**, and that is a
deliberate reversal of an earlier version that derived all five.

The first four describe the *shape* of a snap — it arrives instantly, stays
bright as it fades, and is over inside a fifth of a second. That is physics, and
it is the same in every room. Only levels and timings change with a room, a
microphone and a person, and those are what the passes measure.

Deriving them was tried and was worse on two counts. It is circular: the snap
set is picked using the very features being fitted to it, so the gate can only
ever loosen. And the measurement is unsound — `onset_hf` is read at the onset
block, which sits *before* the transient whenever the attack is slow, so it
reports the room rather than the snap. Every far snap has a slow measured attack,
because at that distance the level climbs through reflections instead of
arriving at once; one real far snap read `onset_hf` 0.16 alongside a `tail_hf`
of 0.98. Fitting to that pulled `hf_ratio_min` from 0.45 down to 0.162.

`pair_refractory_ms` is detector mechanics, and one 34-second pass cannot
outvote the measurement it already rests on. Sweeping it from 220 ms down to
30 ms across a 350-second recording moved the detection count by exactly one,
while double snaps on this machine were measured as fast as **76 ms** — so the
value has to sit below 76 whatever a single pass happens to contain. One
recording's fastest pair was 331 ms, and the old rule returned 120 ms, which
would have swallowed the second snap of every fast double.

The shipped values are tuned against a labelled field log with confirmed ground
truth, which is evidence a 24-second unlabelled pass cannot offer. If they do
not fit a given room, requirements 1 and 2 fail and nothing is written — the
honest inverse of loosening a gate until it fits.

Two of the derived rules deserve their asymmetry spelled out.

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

Calibration writes a config only if all six hold. If any fails, it reports
which one and why, and leaves the previous config alone.

| # | Requirement | Source |
|---|---|---|
| 1 | At least 9 of 10 close snaps detected | Pass 2 |
| 2 | At least 8 of 10 far snaps detected | Pass 3 |
| 3 | No triggers at all from the quiet room | Pass 1 |
| 4 | At least 6 of 10 double snaps actually send | Pass 6 |
| 5 | At most 1 stop survives 60 s of speech | Pass 4 |
| 6 | At least 6 dB between post-snap quiet and still-talking | Pass 7 |

Requirement 3 used to read *at most 2 triggers from 30 s of room noise*, and no
config can pass it. Sweeping `abs_floor_db` from -30 to -6 never brought that
pass below 9 without also losing the snaps, because the levels fully overlap:
the deliberate noises measured -20 to +3 dB and the far snaps -14 to +18, so a
hard keystroke is louder than half of them. Shape does not separate them either
— one noise-pass transient measured `onset_hf` 0.99, `tail_hf` 0.90, decay 52 ms,
better shaped than most genuine snaps. The shipped config, which works in daily
use, scores 16 on the same pass. A bar nothing can clear asserts nothing, so the
requirement now measures what the tool does for most of its life: sit in a quiet
room without firing. The noise count is still printed and journalled, and README
carries the limitation it represents.

Requirement 4 exists because the double snap is the action the whole tool is
for, and nothing checked it. It is measured end to end through the trigger gate
rather than by comparing gaps to the window, because pairing also depends on the
refractory and on both snaps surviving detection. The first version of the
pairing window passed every gap check on paper and sent five times out of ten.

Requirement 6 is the one that can genuinely be unsatisfiable. If a user's voice
and a user's snap overlap with no margin, no threshold separates them, and the
honest output is to say so and offer the alternatives — move the microphone,
snap closer, or accept a manual stop key — rather than to pick a number and let
them discover the problem mid-sentence.

Both sides of it must come from **pass 7 alone**, never from two different
passes. See pass 7 for why: `speech_db` reports dB above a *running* floor, and
that floor sat at -43 dB in the snap passes against -5 dB while talking. An
earlier version took the quiet side from pass 7 and the talking side from pass 4
and reported -2.7 dB for audio that pass 7 by itself separates by 11.6.

The split within pass 7 is not chosen by a threshold. The pass instructs *talk,
snap once, stop talking*, so the snap is the transient with quiet on the far side
of it — a label the user performed. Sorting the pass by post-transient level
lands it in two obvious groups, and the widest step between them is both the cut
and the margin. On the first recording to carry the pass that read 1.7 / 3.9 /
5.0 / 7.3 dB against 18.9 and up: four clean gestures, ten mid-sentence
transients, an 11.6 dB canyon between them. If every rep ends in silence there is
no canyon and none is invented — the whole pass counts as clean.

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
