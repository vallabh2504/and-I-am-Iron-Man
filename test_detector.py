"""Offline tests for SnapDetector. No microphone needed.

Two halves:
  1. Synthetic audio pushed through the full detector, to check the state
     machine and the onset gates.
  2. A replay of a real labelled dry-run log through SnapDetector.accepts(),
     to check the shipped config.json against ground truth.
"""
import inspect
import json
import io
import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from snap_to_dictate import (CONFIG_PATH, DEFAULTS, IDLE, RECORDING, SETTLING,
                             SnapDetector, TriggerGate, claim_instance,
                             classify, cmd_run, cmd_stop, db, legacy_profile,
                             load_config, parse_key, profile_ready,
                             resolve_profile, running_exes, stop_requested,
                             watch_set,
                             strict_profile)

NL_ = chr(10)
SR = 44100
N = 256
rng = np.random.default_rng(0)


# ------------------------------------------------------------------ fixtures

def silence(seconds, level=1e-4):
    return rng.normal(0, level, int(SR * seconds)).astype(np.float32)


def bandpass(x, lo, hi):
    """Zero-phase FFT brickwall - good enough for making test fixtures."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / SR)
    spec[(f < lo) | (f > hi)] = 0
    return np.fft.irfft(spec, n=len(x))


def snap(amp=0.6, decay_ms=30):
    """Finger snap: instant attack, energy peaked 2-5 kHz, ~60 ms tail.

    decay_ms is the envelope time constant; the detector measures roughly
    2x that before the tail crosses 8% of peak, matching the 58-70 ms seen
    in the field log.
    """
    n = int(SR * 0.2)
    t = np.arange(n) / SR
    env = np.exp(-t / (decay_ms / 1000.0))
    burst = bandpass(rng.normal(0, 1, n), 1800, 7000)
    burst /= np.max(np.abs(burst))
    return (amp * env * burst).astype(np.float32)


def thump(amp=0.6):
    """Low-frequency knock: loud but not bright."""
    n = int(SR * 0.15)
    t = np.arange(n) / SR
    env = np.exp(-t / 0.04)
    return (amp * env * np.sin(2 * np.pi * 120 * t)).astype(np.float32)


def speech(amp=0.25, seconds=1.2):
    """Sustained voiced sound: loud, low-frequency, does not decay."""
    n = int(SR * seconds)
    t = np.arange(n) / SR
    sig = sum(np.sin(2 * np.pi * f * t) / (i + 1)
              for i, f in enumerate([180, 360, 540, 900, 1800]))
    env = 0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)
    return (amp * env * sig / 3).astype(np.float32)


def tick(amp=0.6):
    """Bright but far too short - a key click or a mouth tick."""
    return snap(amp=amp, decay_ms=4)


# --------------------------------------------------------------------- harness

results = []


def run(signal, cfg=None):
    cfg = dict(DEFAULTS, **(cfg or {}))
    det = SnapDetector(cfg)
    gate = TriggerGate(cfg, det.block_ms)
    snaps, triggers = 0, 0
    for i in range(0, len(signal) - N, N):
        ev = det.push(signal[i:i + N])
        if ev:
            snaps += 1
            if gate.offer(ev):
                triggers += 1
    return snaps, triggers


def case(name, signal, expect_triggers, cfg=None):
    _, t = run(signal, cfg)
    ok = t == expect_triggers
    print("  %-40s triggers=%d expected=%d  %s"
          % (name, t, expect_triggers, "PASS" if ok else "FAIL"))
    results.append(ok)


def check(name, got, want):
    ok = got == want
    print("  %-40s %s" % (name, "PASS" if ok else "FAIL got=%r want=%r" % (got, want)))
    results.append(ok)


# ----------------------------------------------------------- synthetic audio

print("synthetic audio, default config")
lead, tail = silence(1.5), silence(1.0)

case("silence only", silence(4.0), 0)
case("one snap", np.concatenate([lead, snap(), tail]), 1)
case("three snaps, 1s apart",
     np.concatenate([lead, snap(), silence(1.0), snap(), silence(1.0),
                     snap(), tail]), 3)
case("low thump", np.concatenate([lead, thump(), tail]), 0)
case("sustained speech", np.concatenate([lead, speech(), tail]), 0)
case("loud but too-short tick", np.concatenate([lead, tick(), tail]), 0)
case("quiet key click", np.concatenate([lead, tick(amp=0.12), tail]), 0)

print("\ndouble-snap mode")
dbl = {"require_double": True}
case("2 snaps @300ms -> one trigger",
     np.concatenate([lead, snap(), silence(0.30), snap(), tail]), 1, dbl)
case("1 lone snap -> nothing",
     np.concatenate([lead, snap(), tail]), 0, dbl)
case("2 snaps @2s -> too slow, nothing",
     np.concatenate([lead, snap(), silence(2.0), snap(), tail]), 0, dbl)

# ------------------------------------------------- real labelled field log
# Captured 2026-08-21 via --dry-run on this machine. The operator labelled
# which detections were actual finger snaps. (peak_db, tail_hf, decay_ms)

FIELD_SNAPS = [
    # run 1
    (11.9, 0.79, 58.0), (12.7, 0.79, 63.9),
    (20.3, 0.86, 63.9), (19.8, 0.72, 69.7),
    # run 2: 28 consecutive confirmed snaps, no false positives in the log
    (7.6, 0.94, 40.6), (7.4, 0.75, 46.4), (9.3, 0.97, 52.2), (18.9, 0.76, 40.6),
    (18.5, 0.70, 34.8), (12.7, 0.66, 46.4), (18.1, 0.81, 46.4), (27.2, 0.97, 34.8),
    (5.4, 0.90, 52.2), (18.1, 0.93, 46.4), (13.6, 0.79, 40.6), (12.0, 0.96, 58.0),
    (11.6, 0.75, 52.2), (28.8, 0.98, 40.6), (17.0, 0.79, 58.0), (14.4, 0.96, 75.5),
    (20.0, 0.97, 52.2), (14.7, 0.90, 69.7), (18.4, 0.96, 52.2), (10.3, 0.72, 52.2),
    (14.6, 0.95, 40.6), (22.1, 0.96, 34.8), (16.1, 0.91, 52.2), (6.0, 0.88, 63.9),
    (6.5, 0.94, 63.9), (11.6, 0.78, 52.2), (26.5, 0.88, 46.4), (7.4, 0.76, 58.0),
    # run 3: confirmed working end to end against the desktop app, and the
    # first data showing genuine snaps well below 0 dB from across the room.
    (2.1, 0.82, 58.0), (9.5, 0.83, 75.5), (2.6, 0.76, 52.2),
    (-1.9, 0.80, 52.2), (-10.6, 0.70, 46.4),
    # run 4, harvested from the live logs rather than a labelling session:
    # every event the old strict gate refused to accept as a send. The user was
    # snapping deliberately throughout the window these come from, and each one
    # had already cleared the normal gate, so they are real snaps. They are the
    # far-field, off-axis end of the distribution - exactly the part a gate
    # tuned on close-mic samples does not know about.
    (-3.1, 0.83, 23.2), (-8.1, 0.86, 23.2), (-13.1, 0.81, 23.2),
    (-7.4, 0.96, 23.2), (-17.3, 0.75, 29.0), (5.6, 0.98, 29.0),
    (-17.6, 0.83, 29.0), (-15.6, 0.66, 40.6), (-14.3, 0.68, 46.4),
    (-4.0, 0.68, 46.4), (-9.1, 0.67, 52.2), (0.7, 0.68, 63.9),
    (1.4, 0.66, 69.7), (7.4, 0.67, 81.3), (2.0, 0.65, 98.7),
    (0.7, 0.79, 127.7), (-9.4, 0.88, 127.7), (-0.2, 0.73, 139.3),
    (21.5, 0.99, 145.1), (-1.6, 0.78, 145.1), (-3.0, 0.83, 156.7),
]
FIELD_NON_SNAPS = [
    (-4.6, 0.91, 5.8), (-0.0, 0.07, 11.6), (0.8, 0.14, 17.4),
    (6.8, 0.43, 5.8), (-6.9, 0.14, 34.8), (9.1, 0.01, 87.1),
    (5.6, 0.00, 127.7), (-1.9, 0.12, 17.4), (13.4, 0.04, 69.7),
    (3.9, 0.31, 11.6), (17.8, 0.03, 81.3), (-2.9, 0.06, 17.4),
    (12.4, 0.00, 98.7),
    # Idle-room leaks: six triggers logged by the background listener over two
    # minutes during which nobody snapped. Quiet and only half-bright at the
    # tail - the cluster that sat just under the old 0.50 gate.
    (-9.4, 0.56, 52.2), (-10.1, 0.58, 110.3), (-11.4, 0.62, 98.7),
    (-3.1, 0.63, 34.8), (-6.3, 0.56, 40.6), (-12.5, 0.50, 63.9),
]

print("\nfield log replay against %s" % CONFIG_PATH.name)
det = SnapDetector(load_config(CONFIG_PATH))
kept = [r for r in FIELD_SNAPS if det.accepts(*r)]
leaked = [r for r in FIELD_NON_SNAPS if det.accepts(*r)]
check("real snaps accepted", len(kept), len(FIELD_SNAPS))
check("non-snaps rejected", len(leaked), 0)
for r in leaked:
    print("    LEAK peak %.1f dB  tail_hf %.2f  decay %.1f ms" % r)

cfg = det.cfg
print("    margin to weakest real snap: level %.1f dB, tail_hf %.2f, decay %.1f ms"
      % (min(r[0] for r in FIELD_SNAPS) - cfg["abs_floor_db"],
         min(r[1] for r in FIELD_SNAPS) - cfg["tail_hf_ratio_min"],
         min(r[2] for r in FIELD_SNAPS) - cfg["min_decay_ms"]))

# ---------------------------------------------------------------- key specs

print("\nkey parsing")
for spec, want in [("ctrl+d", ([0x11], ord("D"))),
                   ("enter", ([], 0x0D)),
                   ("alt+k", ([0x12], ord("K"))),
                   ("ctrl+shift+u", ([0x11, 0x10], ord("U"))),
                   ("space", ([], 0x20)),
                   ("f13", ([], 0x7C))]:
    check("parse_key(%r)" % spec, parse_key(spec), want)

# ------------------------------------------------------------ target safety
# Ctrl+D is the Claude desktop app's dictation toggle, but it is also
# end-of-input in every terminal emulator and the way the Claude Code CLI
# exits. A snap that landed on a terminal would therefore close a session
# instead of starting dictation, so the allowlist must stay narrow. This guards
# the config against a well-meaning future edit that widens it back.

print(NL_ + "target safety")
live = load_config(CONFIG_PATH)
from snap_to_dictate import TERMINALS
targets = set(t.lower() for t in live["target_processes"])
check("key is the app's dictation toggle", live["key"], "ctrl+d")
check("no terminal can receive it", sorted(TERMINALS & targets), [])

# ----------------------------------------------------------- the cycle
# Starting and stopping dictation are cheap to get wrong - a stray Ctrl+D
# toggles a mic and one more snap undoes it. Sending is not: it puts a message
# in front of Claude and cannot be taken back. So sending is the only action
# that needs a second snap, and that second snap is judged by a stricter
# profile as well.

print(NL_ + "strict profile (the send confirmation)")
strict = SnapDetector(strict_profile(live))
kept_s = [r for r in FIELD_SNAPS if strict.accepts(*r)]
leak_s = [r for r in FIELD_NON_SNAPS if strict.accepts(*r)]
check("strict profile leaks no non-snap", len(leak_s), 0)
check("strict profile keeps most real snaps",
      len(kept_s) >= 0.8 * len(FIELD_SNAPS), True)
print("    keeps %d/%d labelled snaps; the normal gate keeps %d"
      % (len(kept_s), len(FIELD_SNAPS), len(FIELD_SNAPS)))
# The old strict gate also narrowed the decay window to 30-120 ms. Decay is
# counted in whole 5.8 ms blocks, so 30 ms fell between the 5-block (29.0) and
# 6-block (34.8) steps; in the field it threw away a quarter of all stop snaps
# and never once caught a non-snap. This pins that it stays gone.
check("the old strict decay window is gone", "strict_min_decay_ms" in live, False)
tight = SnapDetector(dict(strict_profile(live),
                          min_decay_ms=30.0, max_decay_ms=120.0))
lost = [r for r in FIELD_SNAPS if strict.accepts(*r) and not tight.accepts(*r)]
print("    it would still be costing %d of the %d labelled snaps"
      % (len(lost), len(FIELD_SNAPS)))
# What the surviving strictness costs, stated rather than assumed. These are
# real snaps that will not confirm a send. The user has to snap again, which
# is a true negative and cheap - unlike a false send, which is not.
dull = [r for r in FIELD_SNAPS if not strict.accepts(*r)]
print("    tail >= %.2f still refuses %d real snaps, all at tail %.2f-%.2f"
      % (live["strict_tail_hf_ratio_min"], len(dull),
         min(r[1] for r in dull), max(r[1] for r in dull)))

print(NL_ + "hands-free cycle")
bm = SnapDetector(live).block_ms
start_gate = TriggerGate(live, bm)

def event(spec, block):
    peak, tail, decay = spec
    return {"peak_db": peak, "tail_hf": tail, "decay_ms": decay, "block": block}

GOOD = (18.0, 0.90, 50.0)   # a solid snap
DULL = (-8.0, 0.60, 50.0)   # the idle-room leak profile, and a plausible
                            # speech transient: loud enough, not bright enough

check("idle + snap starts dictation",
      classify(IDLE, event(GOOD, 100), 0.0, live, strict, start_gate)[0],
      "start")
check("a snap too soon after starting is ignored",
      classify(RECORDING, event(GOOD, 120), 200.0, live, strict, start_gate)[0],
      None)
check("recording + snap stops dictation, without sending",
      classify(RECORDING, event(GOOD, 300), 5000.0, live, strict, start_gate)[0],
      "stop")
check("settling + second snap sends",
      classify(SETTLING, event(GOOD, 340), 232.0, live, strict, start_gate)[0],
      "send")
check("settling + dull transient does not send",
      classify(SETTLING, event(DULL, 340), 232.0, live, strict, start_gate)[0],
      None)
check("settling + a late snap is not a send",
      classify(SETTLING, event(GOOD, 500), 1400.0, live, strict, start_gate)[0],
      None)
# The whole point of the redesign: a missed pair no longer traps the user.
# Whatever happens, one snap always changes the state.
check("every state has an exit",
      sorted(set(classify(st, event(GOOD, 100 + 200 * i), 5000.0,
                          live, strict, TriggerGate(live, bm))[0]
                 for i, st in enumerate((IDLE, RECORDING)))),
      ["start", "stop"])
start_gate.reset()
check("reset forgets a half-finished double", start_gate.pending_block, None)

print(NL_ + "timing arithmetic")
# refractory_ms floors to whole blocks, and that floor is the real shortest
# gap between two accepted snaps. It used to sit above double_min_ms, which
# made the bottom half of the advertised pairing window unreachable.
det_live = SnapDetector(live)
floor_ms = det_live.refractory_blocks * det_live.block_ms
check("the send window is reachable at all", floor_ms < live["send_window_ms"], True)
print("    shortest possible gap between two snaps: %.1f ms; "
      "send window is %.0f ms" % (floor_ms, live["send_window_ms"]))
check("a stop cannot fire before the mic is open",
      live["min_recording_ms"] > floor_ms, True)

# ------------------------------------------------- the silence check on a stop
# The one thing that separates a snap from a mouth click or a plosive is not
# the sound - every spectral feature here overlaps - but what follows it. These
# check the measurement itself, then check the shipped threshold against the
# levels measured on a labelled recording.

print(NL_ + "silence check behind a stop")


def feed(det, signal):
    """Push a signal through a detector, returning the events it produced."""
    out = []
    for i in range(0, len(signal) - N, N):
        ev = det.push(signal[i:i + N])
        if ev:
            out.append(ev)
    return out


def speech_after(signal, tail):
    """Level in the speech band after the first detected onset, in dB over the
    floor. Returns None if nothing was detected.

    Asks the moment the window has arrived, exactly as the listener does. Asking
    at the end of the stream instead would be a different question: by then the
    onset has scrolled out of the history and the honest answer is None.
    """
    det = SnapDetector(live)
    stream = np.concatenate([silence(1.5), signal, tail])
    lo_ms, hi_ms = live["speech_window_ms"]
    onset = None
    for i in range(0, len(stream) - N, N):
        ev = det.push(stream[i:i + N])
        if ev and onset is None:
            onset = ev["onset_block"]
        if onset is not None:
            level = det.speech_db(onset, lo_ms, hi_ms)
            if level is not None:
                return level
    return None


# A stream that opens on digital silence used to latch the floor at zero, after
# which nothing was ever quiet enough to move it and every later reading came
# back as hundreds of dB above the floor.
det_zero = SnapDetector(live)
feed(det_zero, np.zeros(int(SR * 1.0), dtype=np.float32))
feed(det_zero, silence(2.0, level=1e-3))
check("a silent start does not freeze the floor",
      det_zero.speech_floor > 0.0, True)
quiet_ref = SnapDetector(live)
feed(quiet_ref, silence(3.0, level=1e-3))
# Not 0 dB: a floor that falls fast settles near the minimum of a fluctuating
# band, not its mean, and block-to-block energy in a 256-point bin swings by
# about 6 dB. What matters is that an empty room sits far below the threshold.
steady = quiet_ref.speech_db(quiet_ref.block_index - 100, 0.0, 200.0)
check("a steady room reads far below the threshold",
      steady is not None and steady < live["speech_over_floor_db"] - 5.0, True)
print("    an empty room reads %.1f dB over its own floor" % steady)

check("no verdict before the window has arrived",
      SnapDetector(live).speech_db(10, *live["speech_window_ms"]), None)

quiet_level = speech_after(snap(), silence(1.0))
talk_level = speech_after(snap(), speech(seconds=1.0))
check("a snap into silence reads quiet",
      quiet_level is not None and quiet_level < live["speech_over_floor_db"],
      True)
check("the same snap into speech does not",
      talk_level is not None and talk_level >= live["speech_over_floor_db"],
      True)
print("    snap then silence: %.1f dB over floor; snap then speech: %.1f dB"
      % (quiet_level, talk_level))

# Levels measured by --replay over session.wav, a recording labelled by the
# 27-second silence its owner left between talking and snapping. Twelve
# deliberate snaps, seven transients thrown off while speaking.
SESSION_SNAPS = [10.6, 10.4, 10.0, 9.3, 9.0, 8.9, 7.5, 6.6, 6.5, 5.6, 5.2, 4.1]
SESSION_SPEECH = [28.0, 25.8, 24.9, 23.7, 22.4, 22.2, 4.2]
T = live["speech_over_floor_db"]
kept = [x for x in SESSION_SNAPS if x < T]
refused = [x for x in SESSION_SPEECH if x >= T]
check("the threshold lets every labelled snap stop",
      len(kept), len(SESSION_SNAPS))
check("and refuses the speech transients",
      len(refused), len(SESSION_SPEECH) - 1)
print("    threshold %.1f dB: %.1f dB clear of the loudest snap, "
      "%.1f dB clear of the quietest speech"
      % (T, T - max(SESSION_SNAPS), min(refused) - T))
# One speech-phase event still gets through. It was quiet on both sides, so by
# this measure it is indistinguishable from a snap - a click in a pause rather
# than a word being cut off. That is a limit of the test, stated rather than
# hidden: it separates transients that interrupt speech, not every stray noise.
check("the survivor is indistinguishable from a snap",
      min(SESSION_SPEECH) <= max(SESSION_SNAPS), True)
print("    it reads %.1f dB, inside the %.1f-%.1f dB spread of real snaps"
      % (min(SESSION_SPEECH), min(SESSION_SNAPS), max(SESSION_SNAPS)))

# --------------------------------------------- the send confirmation window

# The refractory has two jobs that only look like one, and after a stop they
# pull opposite ways. Covering a snap's own decaying tail needs roughly the
# decay time; stopping three gestures in a second needs far longer. 220 ms is
# set for the second job, and while a send may follow it is actively wrong:
# the confirming snap arrives as fast as fingers move, so the detector was deaf
# for the whole of it and the second snap of a double was never heard at all.
_pair = SnapDetector(load_config(CONFIG_PATH))

check("the pair refractory is shorter than the full one",
      _pair.pair_refractory_blocks < _pair.refractory_blocks, True)

_pair._rearm(True)
_full_cooldown = _pair.cooldown
_pair.expect_pair()
check("expecting a pair cuts the deaf time short",
      (_full_cooldown, _pair.cooldown),
      (_pair.refractory_blocks, _pair.pair_refractory_blocks))

# min(), not assignment. A transient that was merely rejected already has a
# much shorter cooldown, and lengthening it here would swallow the genuine snap
# that follows a cough - which is the case reject_refractory_ms exists for.
_pair._rearm(False)
_rejected = _pair.cooldown
_pair.expect_pair()
check("...but never lengthens one", _pair.cooldown, _rejected)

# How short it can safely be was measured, not guessed. Sweeping the refractory
# from 220 ms to 30 ms over a 350-second recording moved the detection count by
# exactly one, stable the whole way down - a decaying tail never presents the
# rise the onset logic looks for, so it cannot re-fire. The binding constraint
# is therefore the other end: double snaps measured on this machine run
# 76-989 ms, and anything at or above 76 ms would still eat the fastest of them.
_pair_ms = _pair.pair_refractory_blocks * _pair.block_ms
check("it admits the fastest double snap measured here", _pair_ms < 76.0, True)
check("...without going to nothing at all", _pair_ms >= 30.0, True)


# ------------------------------------------------------- per-app routing
# One snap has to mean different things depending on which window is in front.
# The cases below are not hypothetical: every (process, title) pair here was
# read off this machine with all the apps open at once.

print(NL_ + "per-app routing")

ROUTING = {"profiles": DEFAULTS["profiles"]}       # no catch-all configured
CATCHALL = {"profiles": DEFAULTS["profiles"],      # ...and with one
            "fallback": DEFAULTS["fallback"]}


def route(exe, title):
    return resolve_profile(exe, title, ROUTING)


def route_all(exe, title=""):
    return resolve_profile(exe, title, CATCHALL)


check("the Claude desktop window routes to dictation",
      (route("claude.exe", "Claude") or {}).get("mode"), "dictation")

# The load-bearing case. The ChatGPT desktop app serves its chat window and its
# Codex window from one process at one pid, so the image name cannot separate
# them and the title is the only thing that can.
check("ChatGPT and Codex share a process",
      route("chatgpt.exe", "ChatGPT")["process"],
      route("chatgpt.exe", "Codex")["process"])
check("...but the title still splits them",
      (route("chatgpt.exe", "Codex") or {}).get("name"), "Codex")
check("...in both directions",
      (route("chatgpt.exe", "ChatGPT") or {}).get("name"), "ChatGPT")

# And the reason those title patterns are anchored. An Antigravity window with
# a folder called CODEX open has "CODEX" in its title; an unanchored substring
# match would hand it to the Codex profile and press Codex's shortcut into an
# editor.
check("an editor open on a folder named CODEX is not Codex",
      (route("antigravity ide.exe", "CODEX - Antigravity IDE") or {}).get("name"),
      None)
check("...and is still not Codex once a catch-all exists",
      "Codex" in (route_all("antigravity ide.exe",
                            "CODEX - Antigravity IDE") or {}).get("name", ""),
      False)
check("nor is a browser tab that mentions Codex",
      route("msedge.exe", "Codex - Profile 1 - Microsoft Edge"), None)

# The dangerous window. Ctrl+D is end-of-input in every shell, so a terminal
# must match nothing at all rather than fall through to a default.
check("a terminal matches no profile",
      route("windowsterminal.exe", "Windows PowerShell"), None)

# Matching is not permission. Every app whose shortcut nobody has confirmed
# stays unable to receive a keystroke, so wiring an app up and knowing its
# shortcut are separate steps and the second one cannot be skipped by accident.
check("only apps with a confirmed shortcut may send",
      [p["name"] for p in DEFAULTS["profiles"] if profile_ready(p)],
      ["Claude desktop", "Codex", "ChatGPT", "Antigravity"])
check("an app whose own shortcut is unknown gets nothing on its own",
      route("code.exe", "x - Visual Studio Code"), None)
check("...and reaches the system-wide key instead when one is configured",
      (route_all("code.exe", "x - Visual Studio Code") or {}).get("activate"),
      "ctrl+space")

# The two Antigravity programs are separate installs with separate
# executables, and only the desktop app is wired. Matching is on the whole
# image name, so the IDE cannot inherit the desktop app's key - in the IDE
# Ctrl+M is the VS Code "tab moves focus" toggle, a real command that has
# nothing to do with voice.
check("the Antigravity desktop app is wired",
      (route("antigravity.exe", "Antigravity") or {}).get("activate"), "ctrl+m")
check("the Antigravity IDE is not",
      profile_ready(route("antigravity ide.exe", "CODEX - Antigravity IDE")),
      False)

# Ctrl+M in the Antigravity desktop app is dictation, not a conversational
# agent - it types into the composer the way Claude's Ctrl+D does. That makes
# it the full gesture, and it is the mode, not the key, that decides whether
# the stop-side silence check runs at all.
check("Antigravity dictates rather than one-shots",
      (route("antigravity.exe", "Antigravity") or {}).get("mode"), "dictation")
check("...so it carries a submit key",
      (route("antigravity.exe", "Antigravity") or {}).get("send"), "enter")

# The invariant behind both halves: a dictation profile that cannot submit
# would strand the user mid-transcript with no way to finish the gesture.
check("every dictation profile can submit",
      [p["name"] for p in DEFAULTS["profiles"]
       if p["mode"] == "dictation" and not p["send"]], [])

# A voice agent answers on its own, so a oneshot profile must not carry a
# submit key. Holding one would mean a second snap could put something in
# front of the model that nobody typed.
check("no oneshot profile can submit",
      [p["name"] for p in DEFAULTS["profiles"]
       if p["mode"] == "oneshot" and p["send"]], [])
# --follow closes the microphone while no wired app is running. If that list
# is kept by hand beside the routing table, the two drift and snaps in the app
# that only one of them knows about do nothing - with no error, because the
# stream was never open to hear them.
check("the watch list covers every app that can send",
      watch_set(ROUTING),
      set(p["process"] for p in DEFAULTS["profiles"] if profile_ready(p)))
check("...and holds the mic for all three wired apps",
      sorted(watch_set(ROUTING)),
      ["antigravity.exe", "chatgpt.exe", "claude.exe"])
check("...but not for apps with no confirmed shortcut",
      "code.exe" in watch_set(ROUTING), False)

check("Codex and ChatGPT share one key across two windows",
      route("chatgpt.exe", "Codex")["activate"],
      route("chatgpt.exe", "ChatGPT")["activate"])

# Configs written before profiles existed carry target_processes/key/send_key.
# They have to keep working, because --restore can put one back at any time.
legacy = {"target_processes": ["claude.exe"], "key": "ctrl+d",
          "send_key": "enter"}
check("a pre-profiles config still routes",
      (resolve_profile("claude.exe", "Claude", legacy) or {}).get("activate"),
      "ctrl+d")
check("...as a dictation profile",
      legacy_profile(legacy)[0]["mode"], "dictation")

check("case in the image name does not matter",
      (route("CLAUDE.EXE", "Claude") or {}).get("name"), "Claude desktop")


# ------------------------------------------ focus checked at the moment of send
# Three sends do not happen in the same breath as the focus check that
# authorised them: a held stop waits on the silence check, and a submit waits
# for the transcript to land. Focus can move during either wait. These assert
# that the window is re-read at the instant of the press, because a stop that
# arrives late in a terminal sends ctrl+d to a shell and closes it.

import snap_to_dictate as _s

_sent = []


def _with_focus(exe, title, prof_name, what="the stop"):
    """Run send_key_if_focused against a pretended foreground window."""
    real_fg, real_send = _s.foreground_window, _s.send_key
    _s.foreground_window = lambda: (exe, title)
    _s.send_key = lambda m, k: _sent.append((m, k))
    prof = next(p for p in ROUTING["profiles"] if p["name"] == prof_name)
    try:
        before = len(_sent)
        ok = _s.send_key_if_focused((), 0x20, prof, ROUTING, what)
        return ok, len(_sent) - before
    finally:
        _s.foreground_window, _s.send_key = real_fg, real_send


check("a stop lands when focus never moved",
      _with_focus("claude.exe", "Claude", "Claude desktop"), (True, 1))

# The headline failure. ctrl+d is dictation in the Claude desktop app and
# end-of-input in every shell, so this exact case is what the routing table,
# the anchored titles, and this guard all exist to prevent.
check("a stop is dropped, not sent, into a terminal",
      _with_focus("windowsterminal.exe", "Windows PowerShell", "Claude desktop"),
      (False, 0))
check("...and into an editor that is not wired",
      _with_focus("code.exe", "CODEX", "Claude desktop"), (False, 0))

# Same executable, same PID, different window. Matching on the process would
# pass this and submit into the wrong ChatGPT window; matching on the profile
# name catches it.
check("a submit is dropped when focus moved within one process",
      _with_focus("chatgpt.exe", "Codex", "ChatGPT", "the submit"), (False, 0))
check("...and honoured when it is the same window",
      _with_focus("chatgpt.exe", "ChatGPT", "ChatGPT", "the submit"), (True, 1))

check("a stop is dropped when focus moved to another wired app",
      _with_focus("antigravity.exe", "Antigravity", "Claude desktop"),
      (False, 0))

# The guard is only worth having if every delayed send actually calls it. A
# direct send_key on any of these paths is the bug coming back.
_src = inspect.getsource(_s)
_deferred = _src[_src.index("def resolve_pending"):]
_deferred = _deferred[:_deferred.index("def listen")]
check("no delayed send in resolve_pending bypasses the guard",
      "send_key(" in _deferred.replace("send_key_if_focused(", ""), False)
check("the guard is reached from all three delayed sends",
      _src.count("send_key_if_focused("), 4)   # 1 definition + 3 call sites

# Windows refuses SendInput with error 5 when the foreground window runs at a
# higher integrity level, and the catch-all made that reachable from any window
# rather than only from an app somebody wired up on purpose. Twice on
# 2026-08-23 the unhandled OSError ended the listener mid-session.
def _refusing(fn, *a):
    """Run fn with send_key replaced by one that fails the way Windows does."""
    real = _s.send_key

    def boom(mod_vks, key_vk):
        raise OSError("SendInput sent 0/4, error 5.")

    _s.send_key = boom
    try:
        return fn(*a)
    finally:
        _s.send_key = real


_CLAUDE = next(p for p in ROUTING["profiles"] if p["name"] == "Claude desktop")
check("a keystroke Windows refuses does not raise",
      _refusing(_s.send_key_guarded, (), 0x20, _CLAUDE, "the start"), False)

_real_fg = _s.foreground_window
_s.foreground_window = lambda: ("claude.exe", "Claude")
try:
    check("...and the delayed guard reports it as not sent, rather than dying",
          _refusing(_s.send_key_if_focused, (), 0x20, _CLAUDE, ROUTING,
                    "the stop"), False)
finally:
    _s.foreground_window = _real_fg

# Every send inside the listen loop has to go through one of the two guarded
# forms, or one refused keystroke ends the process for every other window too.
_listen = _src[_src.index("def listen"):]
_listen = _listen[:_listen.index(NL_ + "def ", 10)]
check("no send inside the listen loop bypasses the guard",
      "send_key(" in _listen.replace("send_key_guarded(", "")
      .replace("send_key_if_focused(", ""), False)


# ------------------------------------------- calibration matches its document
# CALIBRATION.md once described a six-pass protocol with an acceptance gate and
# a journal, while the code ran three seconds of quiet and five snaps. An agent
# handed this repo read the document, followed it, and the commands were not
# there. These assert the two cannot drift apart again silently.

from snap_to_dictate import (CAL_PASSES, SNAPS_PER_PASS, STOP_GESTURES,
                             STOP_QUIET_DB, permissive, snap_set,
                             stop_snaps_from)

_DOC = (Path(__file__).resolve().parent / "CALIBRATION.md").read_text(
    encoding="utf-8")
README_ = (Path(__file__).resolve().parent / "README.md").read_text(
    encoding="utf-8")

check("the document describes as many passes as the code runs",
      _DOC.count(NL_ + "### Pass "), len(CAL_PASSES))
for _i, _p in enumerate(CAL_PASSES, 1):
    check("...pass %d is %s in both" % (_i, _p["title"]),
          ("### Pass %d: %s" % (_i, _p["title"])) in _DOC, True)

# The gate is the part a user relies on to not be handed a broken config, so
# each criterion has to survive in both places or in neither.
check("the gate measures the quiet room, not the noisy one",
      "No triggers at all from the quiet room" in _DOC, True)
check("...and the code agrees",
      inspect.getsource(_s.derive).count("no triggers at all from the quiet"), 1)
check("the primary action is what the gate checks",
      inspect.getsource(_s.derive).count("double snaps actually send"), 1)
check("the margin is measured within the stop gesture, not across passes",
      "never from two different" in _DOC, True)
check("...and the code takes it from the gap the pass was cut at",
      "margin = stop_gap" in inspect.getsource(_s.derive), True)
check("the doc says which settings are left alone",
      "What calibration does not derive" in _DOC, True)

# --verify runs on a machine nobody has inspected yet, so what it must NOT do
# matters as much as what it checks. These are read off the source because the
# alternative - actually running it - needs a microphone.
_verify = inspect.getsource(_s.cmd_verify)
check("--verify presses no keys", "send_key(" in _verify, False)
check("--verify never signals the stop event", "SetEvent" in _verify, False)
check("...and releases the singleton it tests for",
      "CloseHandle" in _verify, True)
check("--verify exits non-zero when something failed",
      "return 1 if failed else 0" in _verify, True)
# A window title is the only text in this program that comes from outside it,
# and one of them ended a listener that had run for hours. An app put a
# zero-width space in its title, the log line carrying that title could not be
# encoded to the pipe's cp1252, and UnicodeEncodeError came out of print() on
# the normal per-snap path. Two layers now stand between a title and a dead
# process, so both are asserted, and the title is the real one.
_BAD_TITLE = "Claude" + chr(0x200B)

_pipe = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
try:
    _pipe.write(_BAD_TITLE)
    _pipe.flush()
    _still_crashes = False
except UnicodeEncodeError:
    _still_crashes = True
check("the title that killed the listener still breaks a raw cp1252 stream",
      _still_crashes, True)

check("...but loggable() takes the character out",
      chr(0x200B) in _s.loggable(_BAD_TITLE), False)
check("...leaving the readable part of the title alone",
      _s.loggable(_BAD_TITLE).startswith("Claude"), True)
check("...and printable text is passed through untouched",
      _s.loggable("Codex - Antigravity IDE"), "Codex - Antigravity IDE")

_fixed = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
_fixed.reconfigure(encoding="utf-8", errors="replace")
try:
    _fixed.write(_BAD_TITLE)
    _fixed.flush()
    _survives = True
except UnicodeEncodeError:
    _survives = False
check("...and a reconfigured stream survives even an unstripped title",
      _survives, True)

# The layers have to be wired in, not merely present. Matching must NOT be
# sanitised: resolve_profile has to see what Windows actually reported, or a
# pattern would match a window whose real title is something else.
check("main() fixes the output encoding before anything prints",
      "utf8_output()" in inspect.getsource(_s.main), True)
check("...and does it without letting the attempt itself raise",
      "except (AttributeError, ValueError, OSError)"
      in inspect.getsource(_s.utf8_output), True)
check("the listener logs titles through loggable()",
      "loggable(title)" in inspect.getsource(_s.listen), True)
check("...but resolves profiles against the raw title",
      "resolve_profile(exe, title" in inspect.getsource(_s.listen), True)

# --- the stop guard records what it does not yet use -------------------------
# Four numbers were measured against a real session and none of them separates
# a stop the user wanted from one they undid: attack, crest, tail_hf, decay,
# peak and recording length all overlap, and the level the guard itself
# measures had the same median, 6 dB, in both cases. The level BEFORE the
# transient is the one question never asked. It is logged now and gates
# nothing, and these checks hold that line: recorded, and still not acted on.
_rp = inspect.getsource(_s.resolve_pending)
check("the stop guard records the level before the transient",
      "det.speech_db(pending[\"ev\"][\"onset_block\"], -800, -100)" in _rp, True)
check("...on the stop it allows",
      _rp.count("heard += was") == 1, True)
check("...and on the stop it rejects",
      "not a stop%s" in _rp, True)
# Counted over code lines only, because the phrase "if before is None" in the
# formatting ternary is not a decision about the stop and a substring search
# reads it as one.
_uses = [l.strip() for l in _rp.splitlines()
         if "before" in l and not l.strip().startswith("#")]
check("...and 'before' is only ever measured and formatted, never tested",
      _uses, ['before = det.speech_db(pending["ev"]["onset_block"], -800, -100)',
              'was = "" if before is None else "  [before %.0f dB]" % before'])

# README quotes concrete numbers at the reader, and those numbers are promises
# about what the tool will do. They came adrift once already: calibration wrote
# send_window_ms 750, double_min_ms 228 and double_max_ms 911 into config.json,
# while README went on saying 1000 and "120 to 700 ms" because that prose had
# been written against DEFAULTS. Both were internally consistent, so no test
# noticed, and anyone using the repository as shipped was reading the wrong
# numbers. The doc-agreement checks above only covered flags, pass titles and
# criterion wording, never values.
#
# Each number has to appear near the key it belongs to, not merely somewhere in
# the file, or a value like 5 or 14 would match by accident and assert nothing.
_SHIPPED = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
_NEAR = 260

def _quoted_near(key, value):
    """Does README state this value within a couple of sentences of this key?"""
    text = ("%d" % value) if float(value).is_integer() else ("%g" % value)
    mark = "`%s`" % key
    at = README_.find(mark)
    while at != -1:
        if text in README_[max(0, at - _NEAR):at + _NEAR]:
            return True
        at = README_.find(mark, at + 1)
    return False

# Only the settings README actually states a number for. Others, such as
# abs_floor_db, are named and described without a value, which is fine: there
# is nothing to drift.
_STATED = ["send_window_ms", "double_min_ms", "double_max_ms",
           "tail_hf_ratio_min", "strict_tail_hf_ratio_min",
           "speech_over_floor_db", "pair_refractory_ms", "send_delay_ms",
           "min_recording_ms", "recording_max_s", "reject_refractory_ms",
           "refractory_ms", "min_decay_ms",
           "speech_floor_fall", "speech_floor_rise"]

_adrift = [k for k in _STATED if not _quoted_near(k, _SHIPPED[k])]
check("every number README states matches the config that ships", _adrift, [])

# The same drift in the other direction: DEFAULTS is what a reader gets if they
# delete a key, so where README quotes one it has to say so rather than let it
# read as the live value.
check("...and where README quotes a built-in default it labels it as one",
      README_.count("built-in default") >= 2, True)

check("the terminal list covers the common shells",
      {"cmd.exe", "powershell.exe", "pwsh.exe",
       "windowsterminal.exe"} <= set(TERMINALS), True)
AGENTS_ = (Path(__file__).resolve().parent / "AGENTS.md").read_text(
    encoding="utf-8")
check("the agent guide exists and names the verify command",
      "--verify" in AGENTS_, True)

# AGENTS.md points at source lines, and line numbers rot the moment anything is
# inserted above them. Every citation names a symbol on the same line, so the
# citation can be checked against what is actually there.
_SRC_LINES = (Path(__file__).resolve().parent
              / "snap_to_dictate.py").read_text(encoding="utf-8").split(NL_)
_stale = []
for _line in AGENTS_.split(NL_):
    for _n in re.findall(r"snap_to_dictate\.py:(\d+)", _line):
        _at = _SRC_LINES[int(_n) - 1]
        _names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", _line))
        if not any(nm in _at for nm in _names):
            _stale.append("%s -> %r" % (_n, _at.strip()[:40]))
check("every source line AGENTS.md cites still holds what it says",
      _stale, [])

# CALIBRATION.md once told a reader to run --diagnose, which has never existed.
# A command named in a document is a promise, so every flag either parses or
# this fails. --restore is the odd one: it is real, and named in prose only.
_FLAGS = set(re.findall(r"`(--[a-z][a-z-]+)", _DOC + README_))
_KNOWN = set(re.findall(r'"(--[a-z][a-z-]+)"', _src))
check("every flag the documents name actually exists",
      sorted(_FLAGS - _KNOWN), [])

# The gates calibration opens are the level gates. Opening the shape gates too
# turns the detector into a transient counter - it found 31 events in a pass
# containing 10 snaps, and every derived threshold collapsed.
_loose = permissive(load_config(CONFIG_PATH))
check("calibration opens the level gates all the way",
      _loose["abs_floor_db"] <= -70.0, True)
check("...but keeps the shape gates meaningful",
      _loose["tail_hf_ratio_min"] >= 0.5, True)

# A snap is bright at the tail and performed alone; the things mistaken for one
# are dull, or arrive in bursts. Neither test is the count - a pass that asks
# for ten and gets twelve real snaps must keep all twelve.
def _ev(t_s, tail):
    return {"t_s": t_s, "onset_block": int(t_s * 172), "tail_hf": tail,
            "peak_db": 0.0}


_kept, _note = snap_set([_ev(0.5, 0.94), _ev(2.4, 0.08), _ev(4.3, 0.90),
                         _ev(6.2, 0.16), _ev(8.1, 0.96)], 3)
check("dull transients are dropped whatever the count says",
      [e["t_s"] for e in _kept], [0.5, 4.3, 8.1])
check("...and the reason is reported", _note, "2 too dull")

# Twelve snaps on a two-second cadence is what the real recording held, and
# ranking down to ten discarded two of them, then called the discards junk.
_twelve = [_ev(0.7 + 1.95 * i, 0.65 + 0.02 * i) for i in range(12)]
check("snapping past the asked-for count keeps every snap",
      len(snap_set(_twelve, 10)[0]), 12)

# A snap heard twice off a wall should cost the reflection, not the snap.
_echo, _note2 = snap_set([_ev(1.0, 0.93), _ev(1.12, 0.66), _ev(3.0, 0.88)], 2)
check("a reflection loses to the snap that caused it",
      [e["tail_hf"] for e in _echo], [0.93, 0.88])
check("...and that reason is reported too",
      _note2, "1 too close to a neighbour")

# The stop pass labels itself: the snap is the transient with quiet after it.
def _st(after):
    return {"speech_after_db": after}


_quiet, _gap = stop_snaps_from([_st(v) for v in
                                (1.7, 25.4, 3.9, 26.1, 5.0, 29.8, 7.3, 18.9)])
check("the stop pass is cut at its widest gap",
      [e["speech_after_db"] for e in _quiet], [1.7, 3.9, 5.0, 7.3])
check("...and the gap is the margin the gate checks", round(_gap, 1), 11.6)
check("every rep going quiet is a clean pass, not a missing gap",
      stop_snaps_from([_st(v) for v in (1.0, 2.0, 9.0, 3.0)])[0].__len__(), 4)
check("...and the quiet ceiling is what decides that", STOP_QUIET_DB, 12.0)
check("the stop-gesture count is what the document asks for",
      (SNAPS_PER_PASS, STOP_GESTURES), (10, 8))


# ------------------------------------------------------------- lifecycle
# The autostart path: a listener must recognise that another one already owns
# the microphone, must notice --stop, and must be able to tell whether Claude
# Code is still running. Uses a private event name so a live listener that
# happens to be running right now is left alone.


# --- the catch-all, and the windows it must refuse ---------------------------
# Every other profile names the window it may type into. This one does not, so
# it is the only profile whose safety is a list rather than a match, and these
# checks are that list. The send is what makes it dangerous: a double snap
# presses Enter, and Enter runs the command line in a shell, opens the selected
# icon on the desktop, and answers Yes on a UAC prompt.
check("explorer.exe is no longer refused by process alone",
      "explorer.exe" in _s.NEVER_FALLBACK, False)
for _exe in sorted(_s.NEVER_FALLBACK):
    check("the catch-all refuses %s" % _exe, route_all(_exe), None)

check("a window with no identifiable process gets nothing", route_all(""), None)

# foreground_window() names its three failures instead of returning None, so
# that a skipped snap says in the log which failure it was. Those strings are
# descriptions, not image names, and the catch-all was handed them: a snap
# while nothing was in front pressed the system dictation key into whatever
# Windows routed it to. Pulled out of the source rather than retyped, so a
# fourth diagnostic added later is covered the day it is added.
_fg = _src[_src.index("def foreground_window"):]
_fg = _fg[:_fg.index(NL_ + "def ", 10)]
_DIAGS = re.findall(r'return "(<[^"]*)"', _fg)
check("foreground_window still reports its failures as <...> strings",
      len(_DIAGS), 3)
for _d in _DIAGS:
    _name = _d.replace("%d", "1234")
    check("the catch-all refuses %s" % _name, route_all(_name), None)
    check("...and is_named_window says why", _s.is_named_window(_name), False)
check("a real image name is still a name", _s.is_named_window("chrome.exe"),
      True)

# Windows voice typing is one panel for the whole desktop, not one dictation
# per app. Every unwired window therefore gets its own NAME, so a delayed
# keystroke cannot migrate between apps, but one shared SESSION, so alt-tabbing
# does not read as a fresh dictation and press the key a second time - which
# would close the panel while the log claimed it had opened one.
check("two unwired apps share one dictation session",
      _s.session_of(route_all("chrome.exe"))
      == _s.session_of(route_all("notepad.exe")), True)
check("...while still carrying different names",
      route_all("chrome.exe")["name"] == route_all("notepad.exe")["name"],
      False)
check("a wired app is its own session",
      _s.session_of(route_all("claude.exe", "Claude")), "Claude desktop")
check("...and does not share it with the catch-all",
      _s.session_of(route_all("claude.exe", "Claude"))
      == _s.session_of(route_all("chrome.exe")), False)

# The state machine has to ask session_of, not the profile name, or the two
# above stop meaning anything the moment focus moves.
check("the focus-moved reset compares sessions, not names",
      "session_of(prof) != session_of(active)" in _src, True)

# ...unless the app it belongs to says otherwise. Windows voice typing is cloud
# speech recognition: the words arrive after the panel closes, and a person
# waits to see them before deciding to send. Every send that failed in snap.log
# on 2026-08-23 was snapped 1 to 2 seconds after the stop and read as "start
# dictating again" instead, which reopened the panel rather than sending.
_CATCH = dict(DEFAULTS["fallback"])
check("the global send window is the tighter one",
      _s.send_window_for(None, live), live["send_window_ms"])
check("the catch-all gets a wider one, for the cloud round trip",
      _s.send_window_for(_CATCH, live) > live["send_window_ms"], True)
check("a named profile does not, because its transcript is already local",
      _s.send_window_for(next(p for p in DEFAULTS["profiles"]
                              if p["name"] == "Claude desktop"), live),
      live["send_window_ms"])
check("...so the same late snap does send in Windows voice typing",
      classify(SETTLING, event(GOOD, 500), 1400.0, live, strict, start_gate,
               _CATCH)[0], "send")
check("...but not one later than even that window",
      classify(SETTLING, event(GOOD, 700), 9000.0, live, strict, start_gate,
               _CATCH)[0], None)
check("...and the shape gate still applies inside the wider window",
      classify(SETTLING, event(DULL, 500), 1400.0, live, strict, start_gate,
               _CATCH)[0], None)

# The expiry inside listen() has to read the same number, or the state would be
# thrown away before the snap that classify would have accepted ever arrives.
check("the settling expiry asks the profile too, not the global config",
      "send_window_for(active, cfg)" in _src, True)
check("every terminal is inside the refusal list",
      _s.TERMINALS <= _s.NEVER_FALLBACK, True)
# explorer.exe used to be refused outright, which also refused the folder
# windows the user actually wanted to dictate into. It is split by title now:
# a folder window has a search box, the desktop and the switcher do not, and
# Enter on the desktop opens whichever icon happens to be selected.
check("the desktop is refused, because Enter opens what is selected",
      route_all("explorer.exe", "Program Manager"), None)
check("...and so is the alt-tab switcher",
      route_all("explorer.exe", "Task Switching"), None)
check("...and so is an explorer window with no title at all",
      route_all("explorer.exe", ""), None)
for _t in sorted(_s.SHELL_WINDOWS):
    check("...and so is %r" % _t, route_all("explorer.exe", _t), None)
    check("...whatever its case", route_all("explorer.exe", _t.title()), None)
check("a folder window is allowed, so its search box can be dictated into",
      (route_all("explorer.exe", "Downloads") or {}).get("activate"),
      "ctrl+space")
check("...and carries explorer.exe in its name like any other unwired app",
      "explorer.exe" in route_all("explorer.exe", "Downloads")["name"], True)

# An app that IS wired keeps its own key. The catch-all is a last resort, not
# an override, or a snap in Claude would press the system key instead of ctrl+d.
check("a wired app still uses its own shortcut",
      route_all("claude.exe", "Claude")["activate"], "ctrl+d")
check("...and an unwired one gets the system-wide key",
      route_all("chrome.exe", "New Tab")["activate"], "ctrl+space")

# Load-bearing, not cosmetic. send_key_if_focused re-checks focus by comparing
# profile NAMES, so if every unwired app shared one name a gesture begun in a
# browser could finish by typing into a text editor the user alt-tabbed to.
check("each unwired app resolves under its own name",
      route_all("chrome.exe")["name"] == route_all("notepad.exe")["name"], False)
check("...and the name says which app it was",
      "chrome.exe" in route_all("chrome.exe")["name"], True)

# A parked profile is oneshot; the catch-all is dictation. What runs in that
# window is the catch-all whole, so the mode has to come from it too. Reading
# the mode off the parked profile would promise a gesture nobody gets.
check("a parked profile falls through with the catch-all's mode",
      route_all("code.exe", "x - Visual Studio Code")["mode"], "dictation")
check("...and the profile it fell through from was not itself dictation",
      [p["mode"] for p in DEFAULTS["profiles"]
       if p["process"] == "code.exe"], ["oneshot"])
check("the startup banner reports the catch-all's mode, not the parked one",
      'prof["mode"], fb["activate"]' in _src, False)

# Turning it off has to actually turn it off.
check("disabling the catch-all silences every unwired app",
      resolve_profile("chrome.exe", "", {"profiles": DEFAULTS["profiles"],
          "fallback": dict(DEFAULTS["fallback"], enabled=False)}), None)
check("...and so does leaving its key unset",
      resolve_profile("chrome.exe", "", {"profiles": DEFAULTS["profiles"],
          "fallback": dict(DEFAULTS["fallback"], activate=None)}), None)

print("\nlifecycle plumbing")
TEST_EVENT = "Local\\SnapToDictate.selftest"

check("running_exes finds this interpreter",
      Path(sys.executable).name.lower() in running_exes(), True)

first = claim_instance(TEST_EVENT)
# Two listeners on one microphone each send the keystroke, so a snap arrives
# twice and a dictation toggle turns on and straight back off. The failure is
# invisible from inside either process - both look healthy in their own log -
# so the lock is unconditional rather than a default that can be waived.
#
# This asserts an absence on purpose. A parameter for running without the lock
# is a seam, and a seam that exists is one someone eventually passes False to;
# comparing two configs is --replay's job, on identical recorded audio.
check("nothing can run the listener without the lock",
      "singleton" in inspect.signature(cmd_run).parameters, False)

check("first claim wins", first is not None, True)
check("second claim is refused", claim_instance(TEST_EVENT), None)
check("no stop pending yet", stop_requested(first), False)
cmd_stop(TEST_EVENT)
check("stop request is seen", stop_requested(first), True)

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
