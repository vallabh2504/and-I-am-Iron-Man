"""Snap-to-dictate: drive Claude desktop app dictation hands-free by snapping.

Listens to the microphone for a finger-snap transient. When it hears one, it
sends the app's dictation shortcut (Ctrl+D) to the focused window, but only if
that window belongs to an allowlisted process - by default the Claude desktop
app and nothing else.

Snap once to start recording. Speak. Snap again to stop, or snap twice to stop
and submit, so a whole message goes out without touching the keyboard.

Stopping is judged harder than starting, and not on the sound. A stop is held
for a moment and the speech band is read again once the transient has passed: if
the talking carried on, it was a plosive or a mouth click rather than a snap, and
the stop is dropped. A false start only toggles a microphone, but a false stop
cuts a sentence in half and a false send puts it in front of Claude.

    python snap_to_dictate.py --whoami      # what process is focused right now?
    python snap_to_dictate.py --calibrate   # measure mic noise floor + snap level
    python snap_to_dictate.py --dry-run     # detect and log, never send keys
    python snap_to_dictate.py --record s.wav   # ...and save the audio for later
    python snap_to_dictate.py --replay s.wav   # re-run a recording through the gates
    python snap_to_dictate.py               # run for real
    python snap_to_dictate.py --stop        # stop a background listener

Started at logon by a scheduled task via autostart.py, which also passes
--follow so the microphone is held only while a target app is actually running.
Refusing to be the second copy is the default: two listeners on one microphone
each send the keystroke, so every snap arrives twice.

Windows only (uses SendInput / GetForegroundWindow).
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import json
import queue
import re
import sys
import wave
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
import sounddevice as sd

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

DEFAULTS = {
    # --- audio ---
    "device": None,           # input device index, None = system default
    "samplerate": 44100,
    "blocksize": 256,         # 5.8 ms per block at 44.1 kHz

    # --- what counts as a snap ---
    "hf_band_hz": [1500, 16000],  # a snap dumps most of its energy up here
    "hf_ratio_min": 0.45,         # HF share at onset; rejects thumps/vowels
    "tail_hf_ratio_min": 0.55,    # HF share once decayed; rejects bright-onset knocks
    "noise_ratio_thresh": 14.0,   # onset must be this much above the noise floor
    "attack_ratio": 5.0,          # onset must be this much above the previous block
    "abs_floor_db": -70.0,        # never trigger below this absolute HF level
    "decay_frac": 0.08,           # "decayed" = fell to this fraction of peak
    "min_decay_ms": 30.0,         # must take at least this long; rejects ticks
    "max_decay_ms": 160.0,        # must decay within this; rejects sustained sound
    "refractory_ms": 220.0,       # deaf time after an accepted snap
    "pair_refractory_ms": 60.0,   # ...but only this much while a send may follow
    "reject_refractory_ms": 12.0, # deaf time after a rejected transient

    # --- how many snaps make a trigger ---
    "require_double": False,      # True = two snaps in a row (far fewer false fires)
    "double_min_ms": 120.0,
    "double_max_ms": 700.0,
    "trigger_cooldown_ms": 700.0,

    # --- what to send, and where ---
    "key": "ctrl+d",              # the Claude desktop app's dictation toggle
    "send_key": "enter",          # pressed after the stop snap, to submit
    "send_delay_ms": 1500.0,      # let the final transcript land before Enter
    "min_recording_ms": 700.0,    # ignore a stop snap that soon after starting
    "send_window_ms": 1000.0,     # a snap this soon after a stop means "send"
    "recording_max_s": 180.0,     # assume the app stopped on its own after this

    # --- the stricter gate the confirming send snap must clear ---
    # Sending is the only irreversible thing here, so the snap that confirms it
    # is judged harder than the ones that merely toggle a microphone.
    #
    # Only tail brightness is tightened, because it is the only feature with a
    # real gap between the classes: every labelled snap sits at 0.66-0.98 and
    # every labelled non-snap that survives to the decay gate sits below 0.15.
    # A stricter decay window was tried and removed - decay is counted in whole
    # blocks, so it lands only on multiples of 5.8 ms, and a 30 ms minimum sat
    # between the 5-block (29.0) and 6-block (34.8) steps. It threw away clean
    # snaps for arithmetic reasons, at a measured cost of a quarter of all
    # stops, while never once rejecting a real non-snap in the field.
    "strict_tail_hf_ratio_min": 0.70,

    # --- the silence check that guards a stop ---
    # The one thing that reliably separates a real snap from a mouth click or a
    # plosive is not the sound itself, which overlaps on every spectral feature
    # measured here, but what happens next: speech carries on, and a snap is
    # followed by quiet. Measured on 19 labelled events from one session, low
    # band energy 150-300 ms after the onset kept 12 of 12 snaps while rejecting
    # 6 of 7 speech transients, with a 29 dB gap between the classes - so the
    # threshold can sit anywhere in a wide range and give the same answer.
    #
    # Applied only to stopping. After a deliberate stop the speaker falls silent,
    # but after a deliberate start they begin talking immediately, so the same
    # test on a start would reject exactly the snaps it is meant to pass.
    "confirm_stop_with_silence": True,
    "speech_band_hz": [100, 900],   # voiced speech; a finger snap has nothing here
    "speech_window_ms": [150, 300], # measured from the onset, not the detection
    # Threshold sits above the loudest labelled snap (10.3 dB) rather than
    # halfway to the quietest speech (21.7 dB). The two mistakes are not equal:
    # a refused stop costs one more snap, while a stop that should have been
    # refused cuts the sentence off, which is the thing being fixed.
    "speech_over_floor_db": 14.0,   # louder than this = still talking = not a snap
    "speech_floor_fall": 0.25,      # how fast the floor follows the room down
    "speech_floor_rise": 0.0001,    # ...and up; ~1 minute to accept a louder room
    # Deliberately just the desktop app. Ctrl+D is also end-of-input in every
    # terminal, and it is how the Claude Code CLI exits, so a snap that landed
    # on a terminal instead would close a session rather than start dictation.
    # Kept as the fallback for configs written before profiles existed; when
    # "profiles" is present it is what actually decides.
    "target_processes": ["claude.exe"],

    # --- per-app routing ---
    # One snap has to mean different things in different windows, so the
    # foreground window picks the profile and the profile supplies the keys.
    #
    # Matching is by image name AND, optionally, an anchored title pattern.
    # The title half is not decoration. Measured on this machine, the ChatGPT
    # desktop app runs its chat window and its Codex window from a single
    # process at a single pid, so nothing but the title separates them. The
    # pattern is anchored for the same reason: an Antigravity window sitting on
    # a folder called CODEX has "CODEX" in its title, and an unanchored
    # substring match would route it to Codex.
    #
    # First profile whose process and title both match wins, so order is
    # meaningful - put the narrow title-matched entries above the catch-alls.
    #
    # mode "dictation": snap starts, snap stops, a second snap inside
    #   send_window_ms submits. This is the full state machine, and the only
    #   mode the stop-side silence check applies to.
    # mode "oneshot": one snap presses "activate" and that is the whole
    #   gesture. For a voice agent that listens and replies on its own there is
    #   nothing to stop and nothing to submit.
    #
    # A profile with "activate": null, or "enabled": false, NEVER sends
    # anything - it logs what it would have done. Unknown shortcuts stay unset
    # rather than guessed, because a wrong keystroke sent into a window that
    # happens to be a terminal is exactly the failure this routing exists to
    # prevent.
    # "enabled": false means the shortcut is a candidate nobody has confirmed
    # against the real window yet. --test-key will still fire it on demand, so
    # a candidate can be checked deliberately; a snap cannot fire it by
    # accident.
    "profiles": [
        {"name": "Claude desktop", "process": "claude.exe", "title": None,
         "mode": "dictation", "activate": "ctrl+d", "send": "enter",
         "enabled": True},

        # Ctrl+B raises the ChatGPT desktop app's voice agent, and the Codex
        # window is a window of that same app, so the same key reaches it.
        # oneshot, not dictation: a voice agent listens and answers on its own,
        # so there is no transcript to stop and nothing to submit - which is
        # the whole point of routing per window rather than per key.
        #
        # openai/codex issue 23398 documents Ctrl+M as Codex's *dictation*
        # shortcut, which types into the composer instead. That is a different
        # feature needing the full start/stop/submit gesture; switch this
        # profile to {"mode": "dictation", "activate": "ctrl+m",
        # "send": "enter"} if the typed transcript is what is wanted here.
        {"name": "Codex", "process": "chatgpt.exe", "title": "^Codex$",
         "mode": "oneshot", "activate": "ctrl+b", "send": None,
         "enabled": True},
        {"name": "ChatGPT", "process": "chatgpt.exe", "title": "^ChatGPT$",
         "mode": "oneshot", "activate": "ctrl+b", "send": None,
         "enabled": True},

        # The Antigravity desktop app. Note this is NOT the same program as
        # "Antigravity IDE" below - both are installed here, they ship separate
        # executables, and only this one is wired up.
        #
        # Ctrl+M here is dictation rather than a conversational agent: it types
        # into the composer the way Claude's Ctrl+D does, so it takes the full
        # start/stop/submit gesture and, with it, the stop-side silence check.
        # Confirmed in the app by the user, not inferred from the key.
        {"name": "Antigravity", "process": "antigravity.exe", "title": None,
         "mode": "dictation", "activate": "ctrl+m", "send": "enter",
         "enabled": True},

        # Left off deliberately. Antigravity IDE is VS Code based, where Ctrl+M
        # is the accessibility "tab moves focus" toggle rather than anything to
        # do with voice, so inheriting the desktop app's key here would fire a
        # real but unrelated command. Its voice-chat commands exist but want
        # the ms-vscode.vscode-speech extension, which is not installed in
        # either Antigravity profile on this machine.
        {"name": "Antigravity IDE", "process": "antigravity ide.exe",
         "title": None, "mode": "oneshot", "activate": None, "send": None,
         "enabled": False},

        # Same story, same missing extension.
        {"name": "VS Code", "process": "code.exe", "title": None,
         "mode": "oneshot", "activate": None, "send": None,
         "enabled": False},
    ],

    # --- lifecycle, for --follow ---
    "watch_grace_s": 30.0,              # ...releasing it this long after they go
    "watch_poll_s": 5.0,
}

EPS = 1e-20
RING_SAMPLES = 8192   # ~186 ms of raw audio at 44.1 kHz
SPEECH_HISTORY_BLOCKS = 300   # ~1.7 s, comfortably past any one event
PENDING_TIMEOUT_MS = 1200.0   # if the silence check cannot answer, stop anyway


def db(x):
    return 10.0 * np.log10(max(float(x), EPS))


def load_config(path):
    cfg = dict(DEFAULTS)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    return cfg


# ---------------------------------------------------------------- detection

class SnapDetector:
    """Streaming onset detector tuned for finger snaps.

    Two stages. The onset gates ask whether a block is loud, abrupt and bright
    enough to be worth following. The verify gates then watch the tail: a snap
    stays bright as it fades and takes a few tens of milliseconds to do it,
    which is what separates it from a mouth click or a knock whose onset is
    bright but whose body is not.
    """

    IDLE, VERIFY = 0, 1

    def __init__(self, cfg):
        self.cfg = cfg
        n = cfg["blocksize"]
        sr = cfg["samplerate"]
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        lo, hi = cfg["hf_band_hz"]
        self.hf_mask = (freqs >= lo) & (freqs <= hi)
        slo, shi = cfg["speech_band_hz"]
        self.speech_mask = (freqs >= slo) & (freqs <= shi)
        self.block_ms = 1000.0 * n / sr

        self.max_decay_blocks = max(1, int(cfg["max_decay_ms"] / self.block_ms))
        self.refractory_blocks = int(cfg["refractory_ms"] / self.block_ms)
        self.pair_refractory_blocks = max(
            1, int(cfg["pair_refractory_ms"] / self.block_ms))
        self.reject_refractory_blocks = max(
            1, int(cfg["reject_refractory_ms"] / self.block_ms))
        self.abs_floor = 10.0 ** (cfg["abs_floor_db"] / 10.0)

        self.state = self.IDLE
        self.noise_hf = None
        self.prev_hf = EPS
        self.peak = 0.0
        self.onset_ratio = 0.0
        self.verify_blocks = 0
        self.cooldown = 0
        self.block_index = 0
        self.warmup = []          # first blocks seed the noise floor

        # Speech-band history, long enough to look a little way past any event.
        # Keyed by block index so a measurement can be aligned to an onset that
        # has already scrolled by.
        self.speech_hist = collections.deque(maxlen=SPEECH_HISTORY_BLOCKS)
        self.speech_floor = None
        self.speech_warmup = []

        # Raw-sample history, so the onset can be measured at a finer
        # resolution than the block grid. A block is 5.8 ms, and the thing that
        # physically separates a snap from a plosive is how fast it rises -
        # under 1 ms against 5-15 ms - which the block grid cannot see at all.
        self.ring = np.zeros(RING_SAMPLES, dtype=np.float64)
        self.ring_pos = 0
        self.onset_pos = None

    def features(self, block):
        spec = np.abs(np.fft.rfft(block)) ** 2
        total = float(spec.sum())
        hf = float(spec[self.hf_mask].sum())
        self.last_speech = float(spec[self.speech_mask].sum())
        return hf, hf / (total + EPS)

    def _track_speech_floor(self):
        """Follow the room's speech-band level: fall quickly, rise slowly.

        A gated EMA that only learns from quiet blocks cannot be used here.
        Seeded from a single block it can latch onto near-silence, and then
        nothing is ever quiet enough to move it again - the floor sticks at
        zero and every later measurement reads as hundreds of dB above it.

        Falling fast and rising slowly cannot get stuck in either direction.
        Connected speech has a gap every second or two, and those gaps pull the
        floor straight back down to the room, while a genuinely louder room
        takes about a minute to be accepted as the new normal. The two rates
        were swept against a labelled recording: the pair below gave an 11.4 dB
        gap between speech and snaps, against 4.1 dB for a floor that chased
        the room more eagerly.
        """
        v = self.last_speech
        if self.speech_floor is None:
            self.speech_warmup.append(v)
            if len(self.speech_warmup) >= 40:
                self.speech_floor = float(np.median(self.speech_warmup)) + EPS
                self.speech_warmup = []
            return
        rate = (self.cfg["speech_floor_fall"] if v < self.speech_floor
                else self.cfg["speech_floor_rise"])
        self.speech_floor += rate * (v - self.speech_floor)
        self.speech_floor = max(self.speech_floor, EPS)

    def speech_db(self, onset_block, lo_ms, hi_ms):
        """Speech-band level over a span after an onset, in dB above the floor.

        Returns None while the span has not fully arrived yet, which is the
        caller's signal to wait rather than to decide. Also None if the history
        no longer reaches back to the onset, so a stale question gets no answer
        instead of a wrong one.
        """
        if self.speech_floor is None or not self.speech_hist:
            return None
        first = onset_block + int(round(lo_ms / self.block_ms))
        last = onset_block + int(round(hi_ms / self.block_ms))
        if self.block_index < last:
            return None
        if self.speech_hist[0][0] > first:
            return None
        vals = [v for b, v in self.speech_hist if first <= b <= last]
        if not vals:
            return None
        return db(float(np.mean(vals))) - db(self.speech_floor)

    def _remember(self, block):
        """Keep the last RING_SAMPLES of raw audio, oldest overwritten first."""
        n = len(block)
        pos = self.ring_pos
        if pos + n <= RING_SAMPLES:
            self.ring[pos:pos + n] = block
        else:
            cut = RING_SAMPLES - pos
            self.ring[pos:] = block[:cut]
            self.ring[:n - cut] = block[cut:]
        self.ring_pos = (pos + n) % RING_SAMPLES

    def _burst(self, back_samples):
        """The most recent back_samples of audio, oldest first."""
        return np.concatenate((self.ring[self.ring_pos:], self.ring[:self.ring_pos])
                              )[-back_samples:]

    def shape(self, since_onset_samples):
        """Measure how the transient rose, in the sample domain.

        Returns (attack_ms, crest). attack_ms is the 10%-to-90% rise of a
        lightly smoothed envelope; crest is peak over RMS across the burst.
        A finger snap is a near-impulse: it rises in well under a millisecond
        and its peak towers over its own RMS. Speech has to move an airway to
        make a sound, so even its sharpest consonants take milliseconds.
        """
        sr = self.cfg["samplerate"]
        pre = int(sr * 0.004)     # a little context before the onset block
        seg = self._burst(min(RING_SAMPLES, since_onset_samples + pre))
        if len(seg) < 32:
            return float("nan"), float("nan")
        env = np.abs(seg)
        k = max(1, int(sr * 0.0002))              # ~0.2 ms smoothing
        env = np.convolve(env, np.ones(k) / k, mode="same")
        pk = float(env.max())
        if pk <= EPS:
            return float("nan"), float("nan")
        top = int(np.argmax(env))
        lo = np.flatnonzero(env[:top + 1] >= 0.1 * pk)
        hi = np.flatnonzero(env[:top + 1] >= 0.9 * pk)
        if len(lo) == 0 or len(hi) == 0:
            return float("nan"), float("nan")
        attack_ms = 1000.0 * (hi[0] - lo[0]) / sr
        rms = float(np.sqrt(np.mean(seg ** 2)))
        crest = float(np.abs(seg).max()) / (rms + EPS)
        return attack_ms, crest

    def accepts(self, peak_db, tail_hf, decay_ms):
        """Verify-stage gates, expressed over the fields a dry-run logs.

        push() calls this, so a saved log can be replayed against a candidate
        config without a microphone. See test_detector.py.
        """
        return (peak_db >= self.cfg["abs_floor_db"]
                and tail_hf >= self.cfg["tail_hf_ratio_min"]
                and self.cfg["min_decay_ms"] <= decay_ms <= self.cfg["max_decay_ms"])

    def _rearm(self, accepted):
        """Go back to listening.

        After a real snap, stay deaf for the full refractory so its tail cannot
        register twice. After a rejection, go deaf only long enough to clear the
        current transient - a long refractory there would swallow a genuine snap
        that happens to follow a cough or a keystroke.
        """
        self.state = self.IDLE
        self.cooldown = (self.refractory_blocks if accepted
                         else self.reject_refractory_blocks)

    def expect_pair(self):
        """Cut the current deaf time short because a confirming snap may follow.

        The refractory nominally has two jobs: stopping a snap's own tail from
        registering twice, and stopping a person from firing three gestures in
        a second. 220 ms is set for the second. The moment a stop lands the
        second job is actively wrong - the snap that confirms a send arrives as
        fast as fingers move, well inside 220 ms, so the detector was deaf for
        the whole of it. The second snap of a natural double was not rejected;
        it was never heard, which is why nothing appeared in the log at all.

        The first job turns out not to need the refractory. Sweeping it from
        220 ms down to 30 ms across a 350-second recording changed the detection
        count by exactly one, stable the whole way down: a decaying tail never
        presents the rise the onset logic looks for, so it cannot re-fire. That
        one extra detection is the second snap of a double that 220 ms had been
        eating.

        60 ms is set from the double-snap gaps measured on this machine, which
        run 76-989 ms. Calibration measures that distribution properly; until it
        does, this sits below the fastest pair anyone here has produced.
        """
        self.cooldown = min(self.cooldown, self.pair_refractory_blocks)

    def push(self, block):
        self._remember(block)
        """Feed one block. Returns a snap event dict, or None."""
        self.block_index += 1
        hf, hf_ratio = self.features(block)
        self.speech_hist.append((self.block_index, self.last_speech))
        self._track_speech_floor()

        if self.noise_hf is None:
            self.warmup.append(hf)
            if len(self.warmup) < 40:
                self.prev_hf = hf
                return None
            self.noise_hf = float(np.median(self.warmup)) + EPS
            self.warmup = []

        event = None
        if self.cooldown > 0:
            self.cooldown -= 1

        elif self.state == self.IDLE:
            loud = hf > max(self.abs_floor,
                            self.noise_hf * self.cfg["noise_ratio_thresh"])
            sharp = hf > self.prev_hf * self.cfg["attack_ratio"]
            bright = hf_ratio >= self.cfg["hf_ratio_min"]
            if loud and sharp and bright:
                self.state = self.VERIFY
                self.peak = hf
                self.onset_ratio = hf_ratio
                self.verify_blocks = 0
                self.onset_pos = 0
            elif hf < self.noise_hf * 4.0:
                # Track the noise floor only on genuinely quiet blocks.
                self.noise_hf += 0.01 * (hf - self.noise_hf)

        else:  # VERIFY: did it die away like a snap, or like something else?
            self.verify_blocks += 1
            self.peak = max(self.peak, hf)
            if hf < self.peak * self.cfg["decay_frac"]:
                # A snap stays bright all the way down and takes a few tens of
                # ms to get there. A mouth click or a knock with a bright onset
                # collapses into low frequencies, or dies in a single block.
                peak_db = db(self.peak)
                decay_ms = self.verify_blocks * self.block_ms
                if self.accepts(peak_db, hf_ratio, decay_ms):
                    attack_ms, crest = self.shape(
                        (self.verify_blocks + 1) * self.cfg["blocksize"])
                    event = {
                        "block": self.block_index,
                        "onset_block": self.block_index - self.verify_blocks,
                        "peak_db": peak_db,
                        "noise_db": db(self.noise_hf),
                        "onset_hf": self.onset_ratio,
                        "tail_hf": hf_ratio,
                        "decay_ms": decay_ms,
                        "attack_ms": attack_ms,
                        "crest": crest,
                    }
                self._rearm(accepted=event is not None)
            elif self.verify_blocks >= self.max_decay_blocks:
                self._rearm(accepted=False)     # sustained -> not a snap

        self.prev_hf = hf
        return event


class TriggerGate:
    """Turns confirmed snaps into trigger decisions (single or double snap)."""

    def __init__(self, cfg, block_ms):
        self.cfg = cfg
        self.block_ms = block_ms
        self.pending_block = None
        self.cooldown_until = -1
        self.last_gap_ms = None   # gap to the previous transient, when there was one

    def offer(self, event):
        blk = event["block"]
        if blk < self.cooldown_until:
            return False
        cooldown_blocks = int(self.cfg["trigger_cooldown_ms"] / self.block_ms)

        if not self.cfg["require_double"]:
            self.cooldown_until = blk + cooldown_blocks
            return True

        self.last_gap_ms = None
        if self.pending_block is not None:
            gap_ms = (blk - self.pending_block) * self.block_ms
            self.last_gap_ms = gap_ms
            if self.cfg["double_min_ms"] <= gap_ms <= self.cfg["double_max_ms"]:
                self.pending_block = None
                self.cooldown_until = blk + cooldown_blocks
                return True
        self.pending_block = blk
        return False

    def reset(self):
        """Forget a half-finished double snap.

        Called whenever the state machine changes mode, so a lone transient
        logged before the change cannot pair with the first real snap after it
        and fire a double a beat too early.
        """
        self.pending_block = None


# ------------------------------------------------------------ windows input

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT

MODIFIER_VK = {
    "ctrl": 0x11, "control": 0x11,
    "shift": 0x10,
    "alt": 0x12, "meta": 0x12, "opt": 0x12, "option": 0x12,
    "win": 0x5B, "cmd": 0x5B, "super": 0x5B, "command": 0x5B,
}
NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22,
}
EXTENDED_VK = {0x2E, 0x26, 0x28, 0x25, 0x27, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x5B}


def parse_key(spec):
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError("empty key spec: %r" % spec)
    mods, key = parts[:-1], parts[-1]
    try:
        mod_vks = [MODIFIER_VK[m] for m in mods]
    except KeyError as exc:
        raise ValueError("unknown modifier %r in %r" % (exc.args[0], spec)) from None
    if key in NAMED_VK:
        return mod_vks, NAMED_VK[key]
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        return mod_vks, ord(key.upper())
    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return mod_vks, 0x70 + int(key[1:]) - 1
    raise ValueError("unknown key %r in %r" % (key, spec))


def _key_event(vk, up):
    scan = user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    if vk in EXTENDED_VK:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def send_key(mod_vks, key_vk):
    seq = [_key_event(m, False) for m in mod_vks]
    seq += [_key_event(key_vk, False), _key_event(key_vk, True)]
    seq += [_key_event(m, True) for m in reversed(mod_vks)]
    arr = (INPUT * len(seq))(*seq)
    sent = user32.SendInput(len(seq), arr, ctypes.sizeof(INPUT))
    if sent != len(seq):
        raise OSError(
            "SendInput sent %d/%d, error %d. If the target terminal runs "
            "elevated, run this script elevated too."
            % (sent, len(seq), ctypes.get_last_error())
        )


user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int


def loggable(text):
    """A window title with the characters that break a log line taken out.

    Window titles are the one piece of text in this program that comes from
    outside it. Any application can put anything in its own title bar, and one
    of them put a zero-width space (U+200B) in there, which killed a listener
    that had been running fine for hours:

        UnicodeEncodeError: 'charmap' codec can't encode character '​'

    Nothing was wrong with the audio or the detector. The listener wrote its
    log through a pipe, Python picked the system codepage for it because a pipe
    is not a console, and cp1252 has no such character. The traceback went to
    the log and the process was gone, so every snap after that did nothing and
    nothing on screen said why.

    The encoding is fixed at the top of main(). This is the second layer, and
    it is worth having on its own: invisible and control characters in a log
    line make it lie about what it saw. A title that reads Claude here is a
    title that might really be C​laude, and no amount of squinting at the
    log would show it.

    Applied only where a title is printed, never where one is matched.
    resolve_profile has to see exactly what Windows reported, or a profile
    would match a window whose real title is not what the pattern says.
    """
    return "".join(c if c.isprintable() else " " for c in text)


def window_title(hwnd):
    """Title text of a window, or "" if it has none.

    The image name is not always enough to say which app the user is looking
    at. The ChatGPT desktop app serves its chat window and its Codex window
    from one process and one PID - measured on this machine, both report
    ChatGPT.exe at the same pid - so a snap meant for Codex and a snap meant
    for ChatGPT are indistinguishable until the title is read.
    """
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def foreground_window():
    """(image name, window title) for the foreground window, or why not.

    These three failures used to collapse into None, which made a skipped
    trigger impossible to diagnose from the log alone. They mean very different
    things: no foreground window usually means the session is locked or nothing
    is active, while a refused handle means the window belongs to a process at a
    higher integrity level - and that one would have blocked SendInput anyway,
    so it is worth naming rather than guessing at later.

    On any of those failures the title comes back empty rather than partially
    filled, so a profile that matches on title cannot accidentally match a
    window we failed to identify.
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "<no foreground window>", ""
    title = window_title(hwnd)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return "<pid %d: access denied, likely elevated>" % pid.value, ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower(), title
    finally:
        kernel32.CloseHandle(handle)
    return "<pid %d: name unavailable>" % pid.value, ""


def foreground_exe():
    """Just the image name, for callers that do not care which window it is."""
    return foreground_window()[0]


def legacy_profile(cfg):
    """The pre-profiles config expressed as one dictation profile per process.

    Configs written before per-app routing existed carry target_processes, key
    and send_key at the top level. Rather than special-casing them everywhere
    downstream, they are converted once into the shape the rest of the code now
    expects, so old snapshots keep working through --restore unchanged.
    """
    return [{"name": p, "process": p.lower(), "title": None,
             "mode": "dictation", "activate": cfg["key"],
             "send": cfg["send_key"], "enabled": True}
            for p in cfg["target_processes"]]


def profiles_of(cfg):
    return cfg.get("profiles") or legacy_profile(cfg)


def resolve_profile(exe, title, cfg):
    """Which profile owns the foreground window, or None if none does.

    Both halves must match. The process is compared case-insensitively against
    the image name; the title, when a profile sets one, is a regex searched
    against the window title - anchor it unless a substring really is intended,
    because window titles pick up folder and document names and those collide
    with app names in practice.

    A profile matching is not permission to press anything. The caller still
    has to check enabled/activate, which is what keeps an app whose shortcut
    nobody has confirmed yet from receiving a guessed keystroke.
    """
    exe = (exe or "").lower()
    for prof in profiles_of(cfg):
        if prof.get("process", "").lower() != exe:
            continue
        pattern = prof.get("title")
        if pattern and not re.search(pattern, title or ""):
            continue
        return prof
    return None


def profile_ready(prof):
    """True if this profile is allowed to send a keystroke."""
    return bool(prof and prof.get("enabled") and prof.get("activate"))


def send_key_if_focused(mod_vks, key_vk, prof, cfg, what):
    """Press a key only if the window that earned it is still in front.

    Every other send in this file happens in the same breath as the focus check
    that authorised it, so the check and the keystroke cannot disagree. Three
    sends are different: a held stop waits up to PENDING_TIMEOUT_MS for the room
    to go quiet, and a submit waits send_delay_ms for the transcript to land.
    Focus can move during either wait, and without this the keystroke follows
    the profile that started the gesture rather than the window now in front.

    That is the exact failure the whole routing table exists to prevent. ctrl+d
    is dictation in the Claude desktop app and end-of-input in every shell, so a
    stop arriving 300 ms late in a terminal closes it. The window is re-read
    here, immediately before the press, so the gap between deciding and sending
    is as small as it can be made.

    Matching is by profile name, not process, because two profiles can share one
    executable - ChatGPT and Codex are one process at one PID, separated only by
    their titles. Name is the identity the rest of the loop already uses.

    Returns True if the key was sent.
    """
    exe, title = foreground_window()
    now = resolve_profile(exe, title, cfg)
    if now is not None and now["name"] == prof["name"]:
        send_key(mod_vks, key_vk)
        return True
    where = "%s%s" % (exe, (" [%s]" % loggable(title)) if title else "")
    print("[%s] dropped %s for %s; focus moved to %s"
          % (time.strftime("%H:%M:%S"), what, prof["name"], where))
    return False


def watch_set(cfg):
    """Image names worth holding the microphone open for.

    Derived from the profiles rather than configured beside them. The two lists
    answer the same question - which apps does this tool act on - and keeping
    them separately is how they end up disagreeing. They did: the routing table
    grew ChatGPT and Antigravity while the watch list still named only Claude,
    so --follow kept the microphone shut whenever Claude was closed and snaps in
    the other two did nothing at all.

    Profiles that cannot send are excluded. There is no reason to hold a capture
    device open for an app whose shortcut nobody has confirmed yet.
    """
    return set(prof["process"].lower() for prof in profiles_of(cfg)
               if profile_ready(prof))


# ----------------------------------------------------------- process watch

TH32CS_SNAPPROCESS = 0x0002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ULONG_PTR),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                    ctypes.POINTER(PROCESSENTRY32W)]


def running_exes():
    """Lowercased image names of every process we are allowed to see."""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE:
        return set()
    names = set()
    try:
        entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return names


# ------------------------------------------------------- single instance

# A named event does two jobs at once: creating it tells us whether a listener
# is already running, and signalling it asks that listener to quit. The kernel
# destroys the object when the last handle closes, so a crashed listener leaves
# nothing stale behind - unlike a PID file, which has to be second-guessed.

EVENT_NAME = "Local\\SnapToDictate.stop"
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0

kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                  wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenEventW.restype = wintypes.HANDLE
kernel32.SetEvent.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]


def claim_instance(name=EVENT_NAME):
    """Return a handle to the stop event, or None if a listener already owns it.

    `name` is a parameter so the tests can exercise this on their own event
    rather than reaching into - and then shutting down - a live listener.
    """
    handle = kernel32.CreateEventW(None, True, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def stop_requested(handle):
    return kernel32.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0


def cmd_stop(name=EVENT_NAME):
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        print("No listener is running.")
        return 1
    kernel32.SetEvent(handle)
    kernel32.CloseHandle(handle)
    print("Asked the listener to stop.")
    return 0


# -------------------------------------------------------------------- modes

def open_stream(cfg, callback):
    return sd.InputStream(
        device=cfg["device"],
        samplerate=cfg["samplerate"],
        blocksize=cfg["blocksize"],
        channels=1,
        dtype="float32",
        callback=callback,
    )


def collect_hf(cfg, seconds, label):
    """Record for `seconds` and return the per-block HF energy series."""
    det = SnapDetector(cfg)
    values = []
    q = queue.Queue()

    def cb(indata, frames, time_info, status):
        q.put(indata[:, 0].copy())

    print("  " + label)
    with open_stream(cfg, cb):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                block = q.get(timeout=0.5)
            except queue.Empty:
                continue
            hf, _ = det.features(block)
            values.append(hf)
    return np.array(values)


# --------------------------------------------------------------- calibration
#
# Six recorded passes, then every threshold derived offline from the recording.
# Nothing is decided live, so a pass can be re-run without redoing the others,
# and two candidate configs can be compared on identical audio afterwards.
#
# The routine this replaces took the five loudest analysis BLOCKS and called
# them five snaps. A block is 5.8 ms and a snap decays over roughly 160 ms, so
# one snap spans about 27 of them - which meant all five "peaks" came from the
# single loudest snap in the take, and "the weakest snap" was really that one
# snap's fifth-loudest block. The derived floor sat far too high, and a floor
# set too high does not error: it silently drops real snaps and the user
# concludes the tool is unreliable.
#
# Everything below groups blocks into snap events before measuring anything,
# which is what SnapDetector.push already does correctly.

CAL_DIR = Path(__file__).resolve().parent / "calibration"

CAL_PASSES = [
    {"key": "room", "seconds": 15.0, "title": "Room floor", "expect": 0,
     "ask": "Sit as you normally would and stay quiet.",
     "why": "Measuring the room with you in it - your breathing, your chair."},
    {"key": "snaps_close", "seconds": 24.0, "title": "Close snaps", "expect": 10,
     "ask": "Snap 10 times, about two seconds apart, from where you sit.",
     "why": "Normal hand, normal distance. Do not lean toward the mic."},
    {"key": "snaps_far", "seconds": 24.0, "title": "Far snaps", "expect": 10,
     "ask": "Stand up. Snap 10 times from across the room.",
     "why": "Deliberately your weakest snaps - that is the whole point."},
    {"key": "speech", "seconds": 60.0, "title": "Your voice", "expect": 0,
     "ask": "Talk continuously for a minute. Do NOT snap.",
     "why": "Plosives (p t k b d) are the sounds that impersonate a snap."},
    {"key": "noises", "seconds": 30.0, "title": "Room noises", "expect": 0,
     "ask": "Type, click, shift in your chair, set a cup down. No talking.",
     "why": "Include the fan or the door if they are part of this room."},
    {"key": "doubles", "seconds": 34.0, "title": "Double snaps", "expect": 20,
     "ask": "Snap twice, ten times over, at your natural rhythm.",
     "why": "Do not count it out. This measures the timing you really produce."},
    {"key": "stop_gesture", "seconds": 50.0, "title": "Stop gesture", "expect": 8,
     "ask": "Talk a few seconds, snap once, stop talking. Repeat 8 times.",
     "why": "The only pass that performs the real gesture, so the only one "
            "where both sides are measured against the same floor."},
]

PAIR_MAX_MS = 1200.0    # a longer gap separates two pairs, it is not one pair
SNAPS_PER_PASS = 10     # what CAL_PASSES asks for in each of the two snap passes
STOP_QUIET_DB = 12.0    # above this, the speaker had not stopped talking yet
STOP_GESTURES = 8       # ...and in the stop-gesture pass


def permissive(cfg):
    """cfg with the level gates opened wide and the shape gates only relaxed.

    The distinction is the whole point, and getting it wrong was measured
    rather than guessed. Calibration exists to derive the LEVEL gates - how
    loud a snap is here, against this floor, from this distance - so those open
    all the way: a weak snap the current floor rejects is exactly what has to
    be seen. The SHAPE gates are different. That a snap stays bright as it
    fades is physics, not a property of the room, and it is the only feature
    that separates a snap from a footstep.

    Opening the shape gates too turns the detector into a transient counter.
    On the first real six-pass recording, tail_hf_ratio_min at 0.05 found 31
    events in a pass containing 10 snaps: the genuine ones sat at tail_hf
    0.62-0.99 and the other 21 - chair, footsteps, clothing while standing up
    for the far pass - sat at 0.56 and below. Every derived threshold was then
    a percentile over mostly-not-snaps, which collapsed the brightness gates to
    nothing and failed three of the five acceptance checks at once.

    0.55 sits below the weakest genuine snap in that recording and above the
    loudest thing that was not one. Sweeping it from 0.55 to 0.62 left the
    weakest detected snap stable at -13.9 dB, so the value is not balanced on
    a knife edge.
    """
    loose = dict(cfg)
    loose.update({"abs_floor_db": -80.0, "noise_ratio_thresh": 2.5,
                  "hf_ratio_min": 0.15, "tail_hf_ratio_min": 0.55,
                  "min_decay_ms": 8.0, "max_decay_ms": 250.0,
                  "refractory_ms": 80.0, "pair_refractory_ms": 80.0})
    return loose


ISOLATION_MS = 400.0   # closer than this and two transients are one burst
TAIL_FLOOR = 0.60      # below this a transient is not shaped like a snap at all


def snap_set(events, expected):
    """The transients in a snap pass that are actually snaps, in time order.

    Two properties separate a snap from the things that share a room with one,
    and this uses both. Shape: a snap stays bright as it fades, so tail_hf sits
    high. Isolation: a snap is performed alone, a second or two from the next,
    while the things mistaken for one arrive in bursts - a run of keystrokes, a
    chair scraping, a snap's own reflection off a hard wall.

    The count is a hint, never a cut. An earlier version ranked by tail_hf and
    kept exactly `expected`, which works only for as long as the extras really
    are junk. On a clean recording it fails badly. The close pass held twelve
    transients on a metronomic two-second cadence with nothing clustered - that
    is twelve snaps from someone who kept going past ten - and keeping the top
    ten discarded two real ones, then reported that the two discarded looked
    just like the ten kept. They did. Every one of them was a snap. Punishing
    the cleanest possible recording is the wrong way round, so the count is now
    only checked for being in the right neighbourhood.

    Returns the kept events and a note naming what was dropped and why.
    """
    ranked = sorted(events, key=lambda e: e["onset_block"])
    dim = [e for e in ranked if e["tail_hf"] < TAIL_FLOOR]
    bright = [e for e in ranked if e["tail_hf"] >= TAIL_FLOOR]

    # Within a burst keep the brightest and drop the neighbour: a snap heard
    # twice off a wall should cost the reflection, not the snap.
    kept, crowded = [], []
    for e in bright:
        if kept and (e["t_s"] - kept[-1]["t_s"]) * 1000.0 < ISOLATION_MS:
            loser = e if e["tail_hf"] <= kept[-1]["tail_hf"] else kept.pop()
            crowded.append(loser)
            if loser is e:
                continue
        kept.append(e)

    bits = []
    if dim:
        bits.append("%d too dull" % len(dim))
    if crowded:
        bits.append("%d too close to a neighbour" % len(crowded))
    return kept, (", ".join(bits) if bits else "nothing dropped")


def trigger_count(audio, cfg, warmup=None):
    """How many sends this config would actually produce from this audio.

    events_in reports what the detector heard; this reports what the user would
    have seen happen, which is the same thing only when pairing, refractory and
    the send window all cooperate.
    """
    n = cfg["blocksize"]
    det = SnapDetector(cfg)
    gate = TriggerGate(cfg, det.block_ms)
    if warmup is not None:
        for i in range(0, len(warmup) - n, n):
            det.push(warmup[i:i + n])
    sent = 0
    for i in range(0, len(audio) - n, n):
        ev = det.push(audio[i:i + n])
        if ev and gate.offer(ev):
            sent += 1
    return sent


def stop_snaps_from(events):
    """The stop-gesture transients that were followed by silence, plus the gap.

    The pass instructs: talk, snap once, stop talking. So the snap is not the
    brightest thing in the rep - it is the one with quiet on the far side of it.
    That is a label the user performed rather than a threshold this code picked,
    and it lands the pass in two obvious groups. On the first recording to carry
    this pass the quiet side read 1.7, 3.9, 5.0, 7.3 dB and the next value was
    18.9: an 11.6 dB canyon between four clean gestures and ten transients from
    the middle of a sentence.

    An earlier version ranked these by tail_hf like the snap passes, kept eight,
    and took the loudest - which stepped across the canyon, picked up a mid-
    sentence transient, and set speech_over_floor_db to 32.8 dB instead of 10.3.
    A threshold is only as good as the side of the gap it is measured on.

    Cutting at the widest gap also reports an honestly botched pass. Eight reps
    were asked for and four came back clean; deriving from those four is right,
    because a rep where the user never stopped talking describes nothing.

    Returns the quiet-side events and the width of the gap they were cut at.
    """
    have = [e for e in events if e.get("speech_after_db") is not None]
    if len(have) < 2:
        return have, float("nan")
    ranked = sorted(have, key=lambda e: e["speech_after_db"])
    lv = [e["speech_after_db"] for e in ranked]
    # Every rep clean is a real outcome, not a missing gap. Cutting at the
    # widest step in a run of uniformly quiet values would keep two of eight
    # and call the other six mid-sentence, which is the same mistake as taking
    # the ten most snap-like transients out of twelve genuine snaps.
    if max(lv) < STOP_QUIET_DB:
        return ranked, float("inf")
    width, at = max((lv[i + 1] - lv[i], i) for i in range(len(lv) - 1))
    return ranked[:at + 1], width


def events_in(audio, cfg, warmup=None):
    """Every transient in `audio`, as snap events with their five features.

    `warmup` seeds the detector's noise floor - the room pass, normally - so a
    take that opens with a snap is not measured against a floor estimated from
    that snap. Blocks fed as warmup do not produce events.

    Each event also carries `speech_after_db`, the speech-band level in the
    window after its onset. That is measured as the pass plays rather than at
    the end, because the detector's speech history is a bounded deque and a
    question asked too late gets None instead of a wrong answer.
    """
    det = SnapDetector(cfg)
    n = cfg["blocksize"]
    lo_ms, hi_ms = cfg["speech_window_ms"]
    skip = 0
    if warmup is not None:
        for i in range(0, len(warmup) - n + 1, n):
            det.push(warmup[i:i + n])
            skip += 1

    out, waiting = [], []

    def settle(force=False):
        still = []
        for ev in waiting:
            level = det.speech_db(ev["onset_block"], lo_ms, hi_ms)
            if level is None and not force:
                still.append(ev)
                continue
            ev["speech_after_db"] = level
            out.append(ev)
        waiting[:] = still

    for i in range(0, len(audio) - n + 1, n):
        ev = det.push(audio[i:i + n])
        if ev is not None:
            ev = dict(ev)
            ev["t_s"] = (ev["onset_block"] - skip) * n / float(cfg["samplerate"])
            waiting.append(ev)
        settle()
    settle(force=True)
    out.sort(key=lambda e: e["onset_block"])
    return out


def band_floor_db(audio, cfg, mask_name):
    """Median per-block level of one frequency band, in dB."""
    det = SnapDetector(cfg)
    n = cfg["blocksize"]
    vals = []
    for i in range(0, len(audio) - n + 1, n):
        hf, _ = det.features(audio[i:i + n])
        vals.append(hf if mask_name == "hf" else det.last_speech)
    return db(float(np.median(vals))) if vals else float("nan")


def record_pass(cfg, seconds, title):
    """Record one pass with a live countdown. Returns float32 mono."""
    q = queue.Queue()
    chunks = []

    def cb(indata, frames, time_info, status):
        q.put(indata[:, 0].copy())

    with open_stream(cfg, cb):
        start = time.monotonic()
        shown = -1
        while True:
            left = seconds - (time.monotonic() - start)
            if left <= 0:
                break
            whole = int(left) + 1
            if whole != shown:
                bar = "#" * int(28 * (1 - left / seconds))
                print("\r    [%-28s] %3d s " % (bar, whole), end="", flush=True)
                shown = whole
            try:
                chunks.append(q.get(timeout=0.2))
            except queue.Empty:
                pass
        while True:                       # whatever the driver still holds
            try:
                chunks.append(q.get_nowait())
            except queue.Empty:
                break
    print("\r    [%-28s] done. %s" % ("#" * 28, " " * 6))
    return (np.concatenate(chunks) if chunks
            else np.zeros(0, dtype=np.float32))


def write_wav(path, audio, samplerate):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(samplerate))
        w.writeframes((np.clip(audio, -1.0, 1.0) * 32767.0)
                      .astype("<i2").tobytes())


def cmd_calibrate(cfg, config_path):
    """Run the six passes, then derive. See CALIBRATION.md."""
    print("=" * 68)
    print("  Calibration - six passes, about four minutes.")
    print("  Nothing is decided while you record. Every threshold is derived")
    print("  afterwards from the recording, so a pass can be re-run alone.")
    print("=" * 68)

    dev = cfg.get("device")
    print("\n  Input device: %s" % ("system default" if dev is None else dev))
    print("  Sit where you normally sit. Press Enter when you are ready.")
    try:
        input()
    except EOFError:
        pass

    audio = {}
    for i, p in enumerate(CAL_PASSES, 1):
        print("\n" + "-" * 68)
        print("  Pass %d of 6 - %s   (%d s)" % (i, p["title"], int(p["seconds"])))
        print("  %s" % p["ask"])
        print("  %s" % p["why"])
        print("-" * 68)
        for c in (3, 2, 1):
            print("    starting in %d..." % c, flush=True)
            time.sleep(1.0)
        audio[p["key"]] = record_pass(cfg, p["seconds"], p["title"])

    CAL_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d-%H%M")
    wav_path = CAL_DIR / ("%s.wav" % stamp)
    bounds, joined, at = {}, [], 0
    for p in CAL_PASSES:
        a = audio[p["key"]]
        bounds[p["key"]] = [at, at + len(a)]
        joined.append(a)
        at += len(a)
    write_wav(wav_path, np.concatenate(joined), cfg["samplerate"])
    (CAL_DIR / ("%s.passes.json" % stamp)).write_text(
        json.dumps({"samplerate": cfg["samplerate"], "bounds": bounds},
                   indent=2), encoding="utf-8")
    print("\n  Recorded %s (%.1f s)" % (wav_path.name, at / cfg["samplerate"]))

    return derive(cfg, audio, config_path, wav_path, stamp)


def cmd_derive(cfg, wav_path, config_path):
    """Re-derive from a recording made earlier, with nobody in the room.

    This is what makes the recording worth keeping. A rule can be changed and
    judged against the exact audio that produced the last set of complaints,
    instead of against a fresh performance that differs in a dozen
    uncontrolled ways.
    """
    sidecar = wav_path.with_suffix("").with_suffix(".passes.json")
    if not sidecar.exists():
        sidecar = wav_path.with_name(wav_path.stem + ".passes.json")
    if not sidecar.exists():
        print("No pass boundaries beside %s. A calibration recording is a WAV"
              % wav_path.name)
        print("plus a .passes.json naming where each pass starts and ends;")
        print("without it there is no way to tell the speech pass from the")
        print("snap pass. Re-run --calibrate.")
        return 1

    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    with wave.open(str(wav_path), "rb") as w:
        raw = w.readframes(w.getnframes())
        rate = w.getframerate()
    all_audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if rate != cfg["samplerate"]:
        print("WARNING: %s is %d Hz but the config says %d; every millisecond "
              "below will be off." % (wav_path.name, rate, cfg["samplerate"]))
    audio = {k: all_audio[a:b] for k, (a, b) in meta["bounds"].items()}
    stamp = wav_path.stem
    print("Re-deriving from %s (%.1f s)" % (wav_path.name, len(all_audio) / rate))
    return derive(cfg, audio, config_path, wav_path, stamp)


def derive(cfg, audio, config_path, wav_path, stamp):
    """Turn six recorded passes into a config, or refuse and say why.

    Stated as rules rather than magic numbers, so any config this writes can be
    audited against the recording that produced it.
    """
    loose = permissive(cfg)
    room = audio["room"]
    ev = {k: events_in(audio[k], loose, warmup=room)
          for k in ("snaps_close", "snaps_far", "speech", "noises", "doubles")
          if k in audio}
    # Recordings made before the stop-gesture pass existed are still readable;
    # they simply cannot answer the question that pass was added to answer.
    has_stop = "stop_gesture" in audio and len(audio["stop_gesture"]) > 0
    if has_stop:
        ev["stop_gesture"] = events_in(audio["stop_gesture"], loose, warmup=room)

    # The snap passes get ranked down to the count that was actually performed.
    # The other passes do not: every transient in the speech and noise passes is
    # a negative example by definition, and throwing any of them away would be
    # throwing away the evidence the gates are built from.
    close, note_close = snap_set(ev["snaps_close"], SNAPS_PER_PASS)
    far, note_far = snap_set(ev["snaps_far"], SNAPS_PER_PASS)
    snaps = close + far

    print("\n" + "=" * 68)
    print("  What the recording contains")
    print("=" * 68)
    for p in CAL_PASSES[1:]:
        if p["key"] not in ev:
            print("    %-14s  not in this recording" % p["title"])
            continue
        got = len(ev[p["key"]])
        if p["key"] in ("snaps_close", "snaps_far"):
            usable, note = ((close, note_close) if p["key"] == "snaps_close"
                            else (far, note_far))
            print("    %-14s %3d transient(s)  -> %d snap(s), %s"
                  % (p["title"], got, len(usable), note))
        else:
            note = "" if not p["expect"] else "  (expected about %d)" % p["expect"]
            print("    %-14s %3d transient(s)%s" % (p["title"], got, note))
    floor_hf = band_floor_db(room, cfg, "hf")
    floor_sp = band_floor_db(room, cfg, "speech")
    print("    %-14s hf %.1f dB   speech-band %.1f dB"
          % ("Room floor", floor_hf, floor_sp))

    fatal = []
    if floor_hf > -30.0:
        fatal.append("The room floor is %.1f dB, above -30. Turn the input gain "
                     "down or quiet the room, then re-run." % floor_hf)
    if floor_hf < -80.0:
        fatal.append("The room floor is %.1f dB, below -80. The mic is muted or "
                     "the wrong device is selected." % floor_hf)
    # A range, not a count. Snapping eleven or twelve times in a pass that asks
    # for ten is not an error and must not be treated as one; what matters is
    # that the pass yielded enough real snaps to describe the distribution, and
    # not so many that something in the room is being counted as a snap.
    for name, usable, hint in (
            ("close", close, "Snap once every second or two and leave the rest "
                             "of the room alone while the pass runs."),
            ("far", far, "Get into position, stand still, then snap - moving "
                         "about makes more noise than a far snap does.")):
        if len(usable) < 6:
            fatal.append("The %s pass yielded only %d usable snaps. %s"
                         % (name, len(usable), hint))
        elif len(usable) > SNAPS_PER_PASS * 1.8:
            fatal.append("The %s pass yielded %d usable snaps where it asked "
                         "for %d, so something in the room is snap-shaped and "
                         "is being counted as one. %s"
                         % (name, len(usable), SNAPS_PER_PASS, hint))
    if len(ev["snaps_close"]) < 8:
        fatal.append("Only %d of 10 close snaps registered even wide open. That "
                     "is hardware, not tuning - the mic is too far, the gain is "
                     "too low, or it is the wrong device."
                     % len(ev["snaps_close"]))
    if len(ev["snaps_far"]) < 6:
        fatal.append("Only %d of 10 far snaps registered. The room is too big "
                     "for this mic; the working range has to be stated rather "
                     "than tuned around." % len(ev["snaps_far"]))
    if not ev["speech"]:
        fatal.append("No transients at all in the speech pass, so there is no "
                     "negative set to work from. Re-run it and talk more.")
    if not has_stop:
        fatal.append("This recording has no stop-gesture pass, so the margin "
                     "between a real stop and mid-sentence cannot be measured "
                     "against the floor that applies when it matters. Re-run "
                     "--calibrate to record all seven passes.")
    if fatal:
        print("\n" + "=" * 68)
        print("  Cannot derive a config")
        print("=" * 68)
        for f in fatal:
            print("    - %s" % f)
        print("\n  %s is unchanged. The recording is kept at %s"
              % (config_path.name, wav_path.name))
        return 1

    # ---- the rules ---------------------------------------------------------
    peaks = [e["peak_db"] for e in snaps]
    weakest = min(peaks)
    # Set from the weakest snap, not the average. The two mistakes cost
    # differently: a floor set low lets in a room noise the later gates then
    # reject, while a floor set high silently drops real snaps and logs nothing.
    abs_floor = max(round(weakest - 4.0, 1), round(floor_hf + 12.0, 1))

    # hf_ratio_min, tail_hf_ratio_min and the decay bounds are deliberately NOT
    # derived. They describe the shape of a snap - that it arrives instantly,
    # stays bright as it fades, and is over inside a fifth of a second - which
    # is physics, the same in every room. Levels and timings are what a room,
    # a mic and a person change, and those are what this derives.
    #
    # It was tried the other way and the result was worse on both counts. It is
    # circular: the snap set is chosen using the very features being fitted to
    # it, so the gate can only ever loosen. And the measurement it fits is not
    # sound - onset_hf is read at onset_block, which sits before the transient
    # whenever the attack is slow, so it reports the room instead of the snap.
    # Every far snap has a slow measured attack, because at that distance the
    # level climbs through reflections rather than arriving at once: one read
    # onset_hf 0.16 with a tail_hf of 0.98. Fitting to that pulled hf_ratio_min
    # from 0.45 down to 0.162, and the acceptance gate caught what followed.
    #
    # The shipped values are tuned against a labelled field log with confirmed
    # ground truth, which is the evidence an unlabelled 24-second pass cannot
    # offer. If they do not fit this room, acceptance checks 1 and 2 fail and
    # nothing is written - the honest inverse of loosening a gate until it fits.

    # Both sides come from audio where the speaker has been talking, so both
    # are measured against an elevated floor. Taking the quiet side from the
    # snap passes instead - where the floor sits 38 dB lower - is what made the
    # first version of this check report a negative margin on well-separated
    # audio. The stop gesture is the only pass that reproduces the real moment.
    if has_stop:
        stop_snaps, stop_gap = stop_snaps_from(ev["stop_gesture"])
    else:
        stop_snaps, stop_gap = [], float("nan")
    quiet_after = [e["speech_after_db"] for e in stop_snaps
                   if e.get("speech_after_db") is not None]
    speech_after = [e["speech_after_db"] for e in ev["speech"]
                    if e.get("speech_after_db") is not None]
    # Placed above the loudest post-snap quiet, not midway to speech. A stop
    # that fires when it should not have cuts a sentence in half; a stop that
    # is refused costs one more snap. The asymmetry is deliberate.
    # Derived only from a pass that was actually performed. Four clean reps is
    # not enough to see the top of the distribution, and a threshold fitted to
    # the largest of four is tighter than the truth: this recording gave 4 of 8
    # and derived 10.3 dB, while a separately labelled recording holds genuine
    # snap-stops up to 10.6. Under-shooting is the safe direction - a stop below
    # the line is held, not lost, and fires when the room goes quiet - but it is
    # still a stop the user has to wait for, and silently narrowing a working
    # threshold on four samples is not what calibration is for.
    enough = len(quiet_after) >= STOP_GESTURES // 2 + 2
    speech_over = (round(max(quiet_after) + 3.0, 1)
                   if quiet_after and enough else cfg["speech_over_floor_db"])
    if has_stop:
        print("    %-14s %d of %d rep(s) ended in silence, cut at a %.1f dB gap"
              % ("Clean stops", len(stop_snaps), STOP_GESTURES, stop_gap))

    ts = [e["t_s"] for e in ev["doubles"]]
    gaps = [(b - a) * 1000.0 for a, b in zip(ts, ts[1:])
            if (b - a) * 1000.0 <= PAIR_MAX_MS]
    if len(gaps) >= 4:
        g5, g95 = (float(np.percentile(gaps, 5)), float(np.percentile(gaps, 95)))
        fastest = min(gaps)
        # Widened, not fitted. Percentiles land the edges on top of the gaps
        # that were actually performed, and a window whose floor sits 6 ms under
        # the fastest pair recorded refuses the next one that comes in slightly
        # quicker. This recording measured seven pairs at 331-366 ms and derived
        # 325 ms as the floor; five of ten pairs then failed to send. Widening
        # to 232 ms costs nothing - a stray transient still has to land inside
        # the window AND be shaped like a snap - and recovers the pairs.
        double_min = max(40.0, round(g5 * 0.7, 0))
        double_max = round(g95 * 1.4, 0)
        # Rounded up to the next 250 ms, and floored at 600. The two mistakes
        # are not equal: a window too small silently refuses the confirming
        # snap and the user re-snaps into a session that is already off, while
        # a window too large only risks pairing a stray snap that has to land
        # inside one second of a deliberate stop. Ten calibration pairs are a
        # small sample to set a hard ceiling from.
        send_window = max(600.0, float(int(g95 / 250.0 + 1) * 250))
        # Deliberately capped, and capped low. This is the deaf time after a
        # stop while a confirming snap may still be coming, and its only job is
        # to not swallow one. Sweeping the refractory from 220 ms down to 30 ms
        # across a 350-second recording changed the detection count by exactly
        # one, stable the whole way down - a decaying tail never presents the
        # rise the onset logic looks for, so it cannot re-fire and there is
        # nothing here for a long value to buy.
        #
        # Deriving it from the fastest gap alone is how the double-snap bug
        # comes back: one unhurried calibration session sets a value that then
        # rejects every quick double the user produces afterwards.
        # pair_refractory_ms is deliberately NOT derived, for the same reason
        # the shape gates are not: it is detector mechanics, and one take cannot
        # outvote the measurement it already rests on. Sweeping it from 220 ms
        # down to 30 ms over a 350-second recording moved the detection count by
        # exactly one, while double snaps on this machine were measured as fast
        # as 76 ms - so the value has to sit below 76 whatever a single pass
        # happens to contain. This recording's fastest pair was 331 ms and the
        # old rule returned 120, which would have eaten the second snap of every
        # fast double. The shipped 60 ms is the answer to a better-posed
        # question than this pass asks.
        pair_ref = cfg["pair_refractory_ms"]
    else:
        print("\n    Only %d usable pair gap(s); keeping the current pairing "
              "window." % len(gaps))
        g5 = g95 = fastest = float("nan")
        double_min, double_max = cfg["double_min_ms"], cfg["double_max_ms"]
        send_window, pair_ref = cfg["send_window_ms"], cfg["pair_refractory_ms"]

    new = dict(cfg)
    new.update({"abs_floor_db": abs_floor,
                "speech_over_floor_db": speech_over,
                "double_min_ms": double_min, "double_max_ms": double_max,
                "send_window_ms": send_window,
                "pair_refractory_ms": pair_ref})

    print("\n" + "=" * 68)
    print("  Derived")
    print("=" * 68)
    for k in ("abs_floor_db", "speech_over_floor_db",
              "double_min_ms", "double_max_ms", "send_window_ms",
              "pair_refractory_ms"):
        flag = "" if new[k] == cfg[k] else "   <- changed"
        print("    %-22s %-10s (was %s)%s" % (k, new[k], cfg[k], flag))

    # ---- the acceptance gate ----------------------------------------------
    # Re-measured under the DERIVED config, not the permissive one. The point
    # is whether the settings about to be written actually work.
    kept = {k: events_in(audio[k], new, warmup=room)
            for k in ("snaps_close", "snaps_far", "speech", "noises")}
    # The double snap is the action the whole tool exists to perform, and until
    # now nothing checked that the derived config could still perform it. It is
    # measured end to end through TriggerGate rather than by comparing gaps to
    # the window, because pairing also depends on refractory and on both snaps
    # surviving detection - the first version of this window passed every gap
    # check on paper and sent five times out of ten.
    sends = trigger_count(audio["doubles"], dict(new, require_double=True), room)
    idle = trigger_count(room, new)
    stops = [e for e in kept["speech"]
             if e.get("speech_after_db") is None
             or e["speech_after_db"] < new["speech_over_floor_db"]]
    # The gap the stop pass was cut at IS the margin, and taking it from there
    # is the whole point of having the pass. Both sides are then one rep of one
    # gesture recorded against one floor. An earlier version took the quiet side
    # from pass 7 and the talking side from pass 4 and reported -2.7 dB for the
    # same audio that pass 7 alone separates by 11.6, because speech_db is dB
    # above a RUNNING floor and that floor is not the same number in two passes.
    margin = stop_gap

    # bool() on purpose: numpy comparisons return np.bool_, which json refuses,
    # and the journal is written after this list is built.
    checks = [
        ("at least 9 of 10 close snaps detected",
         len(kept["snaps_close"]) >= 9, "%d detected" % len(kept["snaps_close"])),
        ("at least 8 of 10 far snaps detected",
         len(kept["snaps_far"]) >= 8, "%d detected" % len(kept["snaps_far"])),
        # The quiet room, not the noisy one. This gate used to require at most
        # 2 triggers from 30 s of deliberate keyboard, mouse and chair, and no
        # config can pass it. Sweeping abs_floor_db from -30 to -6 never brought
        # that pass below 9 without also losing the snaps, because the levels
        # fully overlap: the noises measured -20 to +3 dB and the far snaps -14
        # to +18, so a keystroke is louder than half of them. Shape does not
        # separate them either - a noise-pass transient measured onset_hf 0.99,
        # tail_hf 0.90, decay 52 ms, better shaped than most genuine snaps. The
        # shipped config, which works in daily use, scores 16 on the same pass.
        #
        # A bar nothing can clear asserts nothing, so this measures what the
        # tool actually does for most of its life: sit in a quiet room without
        # firing. The noise count is still computed, printed and journalled, and
        # README carries the limitation it represents.
        ("no triggers at all from the quiet room",
         idle == 0, "%d fired" % idle),
        ("at least 6 of 10 double snaps actually send",
         sends >= 6, "%d sent" % sends),
        ("at most 1 stop survives 60 s of speech",
         len(stops) <= 1, "%d survived" % len(stops)),
        # Measured within the stop-gesture pass, never across passes. speech_db
        # reports dB above a RUNNING floor, and that floor sat at -43 dB in the
        # snap passes against -5 dB while talking - a 38 dB difference. Comparing
        # the two, as this check first did, compared numbers with no common
        # baseline and reported -3.5 dB for audio that is separated by about 34.
        ("at least 6 dB between post-snap quiet and still-talking",
         bool(not np.isnan(margin) and margin >= 6.0), "%.1f dB" % margin),
    ]
    print("\n" + "=" * 68)
    print("  Acceptance")
    print("=" * 68)
    for name, ok, detail in checks:
        print("    [%s]  %-46s %s" % ("PASS" if ok else "FAIL", name, detail))
    if len(kept["noises"]) > 2:
        print("\n    Note: %d of the deliberate room noises still read as "
              "snaps. Nothing\n    separates a hard keystroke from a snap on "
              "this mic, so typing right\n    next to it can occasionally "
              "fire. See README, Limitations." % len(kept["noises"]))

    journal = {
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "device": cfg.get("device"),
        "room": {"noise_floor_db": round(floor_hf, 1),
                 "speech_floor_db": round(floor_sp, 1)},
        "snaps_close": {"n": SNAPS_PER_PASS, "heard": len(ev["snaps_close"]),
                        "detected": len(kept["snaps_close"]),
                        "dropped": note_close,
                        "peak_db": [round(e["peak_db"], 1) for e in close]},
        "snaps_far": {"n": SNAPS_PER_PASS, "heard": len(ev["snaps_far"]),
                      "detected": len(kept["snaps_far"]),
                      "dropped": note_far,
                      "peak_db": [round(e["peak_db"], 1) for e in far]},
        "speech": {"seconds": 60, "transients": len(ev["speech"]),
                   "levels_db": [round(v, 1) for v in speech_after]},
        "noises": {"seconds": 30, "transients": len(kept["noises"])},
        "idle_triggers": idle,
        "doubles_sent": sends,
        "doubles": {"n": 10, "gaps_ms": [round(g) for g in gaps]},
        "derived": {k: new[k] for k in
                    ("abs_floor_db", "speech_over_floor_db",
                     "double_min_ms", "double_max_ms", "send_window_ms",
                     "pair_refractory_ms")},
        "acceptance": {"passed": all(ok for _, ok, _ in checks),
                       "margin_db": None if np.isnan(margin) else round(margin, 1),
                       "checks": {name: ok for name, ok, _ in checks}},
        "recording": str(wav_path.name),
    }
    CAL_DIR.mkdir(exist_ok=True)
    jpath = CAL_DIR / ("%s.json" % stamp)

    def plain(o):
        """numpy scalars are not JSON, and losing the journal to a TypeError
        after the user has already performed the six passes is the one failure
        worth spending five lines to make impossible."""
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        return str(o)

    jpath.write_text(json.dumps(journal, indent=2, default=plain),
                     encoding="utf-8")

    if not all(ok for _, ok, _ in checks):
        print("\n  %s is unchanged - a config is only written when all %d"
              % (config_path.name, len(checks)))
        print("  hold. The recording and the journal are kept, so this run can")
        print("  be re-derived later without performing it again:")
        print("      %s" % wav_path)
        print("      %s" % jpath)
        if not np.isnan(margin) and margin < 6.0:
            print("\n  The margin is the one that can be genuinely unsatisfiable.")
            print("  If your voice and your snap overlap with no gap, no")
            print("  threshold separates them - move the mic, snap closer, or")
            print("  accept a manual stop key.")
        return 1

    good = config_path.with_name("config.known-good.json")
    if config_path.exists():
        good.write_text(config_path.read_text(encoding="utf-8"),
                        encoding="utf-8")
    # Two successful calibrations in a row used to destroy the hand-tuned
    # fallback: the first moved it into config.known-good.json, the second
    # overwrote that with the config the first had just derived. The outgoing
    # config is therefore also parked next to the journal under the stamp of
    # the run that replaced it, where nothing later can reach it.
    (CAL_DIR / ("%s.replaced.json" % stamp)).write_text(
        json.dumps(cfg, indent=2), encoding="utf-8")
    config_path.write_text(json.dumps(new, indent=2), encoding="utf-8")
    print("\n  All %d hold. Wrote %s" % (len(checks), config_path.name))
    print("  Previous config saved to %s (--restore puts it back)" % good.name)
    print("  Journal:   %s" % jpath)
    print("  Recording: %s" % wav_path)
    print("\n  Next: python snap_to_dictate.py --dry-run")
    return 0


def cmd_test_key(cfg, seconds=5, override=None):
    """Send a profile's activate key on a countdown, with no microphone involved.

    Splits the chain in half. If the app responds here but not on a snap, the
    detector is at fault; if it does not respond here either, the problem is the
    shortcut or the window - not this script.

    `override` sends that keystroke instead of the profile's, which is how an
    unknown shortcut gets confirmed: try a candidate against the real window,
    watch what the app does, and write it into the config only once it works.
    The window still has to match a profile first, so a candidate key cannot be
    fired into an app this tool was never pointed at.
    """
    print("Click into the window you want to test and leave its input EMPTY.")
    for i in range(seconds, 0, -1):
        print("  %d..." % i, flush=True)
        time.sleep(1)
    exe, title = foreground_window()
    prof = resolve_profile(exe, title, cfg)
    where = "%s%s" % (exe, (" [%s]" % loggable(title)) if title else "")
    if prof is None:
        print("\nFocused window is %s, which no profile claims." % where)
        print("Nothing sent. Run --whoami to see how to add it.")
        return 1

    key = override or prof.get("activate")
    if not key:
        print("\nProfile %r matched %s but has no activate key set."
              % (prof["name"], where))
        print("Nothing sent. Try a candidate with:")
        print("    --test-key --key ctrl+shift+v")
        return 1

    send_key(*parse_key(key))
    print("\nSent %s to %s (profile %r). SendInput reported success."
          % (key, where, prof["name"]))
    print("Did the app react? If not, the keystroke is arriving but the app is")
    print("not acting on it - press %s by hand in the same window to confirm"
          % key)
    print("the shortcut is what you think it is.")
    return 0


def cmd_whoami(cfg, seconds=4):
    """Report the focused window and which profile, if any, would claim it.

    Reports the title as well as the image name because for some apps the title
    is the only distinguishing feature, and prints the resolved profile so the
    answer to "why did nothing happen" is visible without reading the config.
    """
    print("Click the window you want to check. Reading focus in %d seconds..."
          % seconds)
    for i in range(seconds, 0, -1):
        print("  %d..." % i, flush=True)
        time.sleep(1)
    exe, title = foreground_window()
    print("\n  process : %s" % exe)
    print("  title   : %s" % (title or "<none>"))

    prof = resolve_profile(exe, title, cfg)
    if prof is None:
        print("  profile : none - a snap here is ignored")
        print("\nTo route this window, add a profile to config.json with")
        print('  "process": %r' % exe)
        if title:
            print('  "title": "^%s$"   (only if the image name is shared)'
                  % re.escape(title))
        return 0

    print("  profile : %s  (mode %s)" % (prof["name"], prof.get("mode")))
    if profile_ready(prof):
        print("  a snap here sends: %s" % prof["activate"])
        if prof.get("mode") == "dictation" and prof.get("send"):
            print("  a confirming snap sends: %s" % prof["send"])
    else:
        why = ("no activate key set yet" if not prof.get("activate")
               else "profile is disabled")
        print("  a snap here sends: NOTHING (%s)" % why)
        print("\n  Find the shortcut in that app, then set \"activate\" and")
        print("  \"enabled\": true on the %r profile." % prof["name"])
    return 0


STOP, GONE = "stop", "gone"
IDLE, RECORDING, SETTLING = "idle", "recording", "settling"


def strict_profile(cfg):
    """cfg with the strict_* gates swapped in over their normal counterparts."""
    out = dict(cfg)
    for name in ("tail_hf_ratio_min",):
        out[name] = cfg["strict_" + name]
    return out


def classify(state, ev, held_ms, cfg, strict, start_gate):
    """Decide what a confirmed snap means right now.

    Returns (action, note): action is "start", "stop", "send" or None, and note
    explains a refusal for the log. held_ms is how long the current state has
    been in force. Kept out of listen() so the whole cycle can be tested
    without a microphone or a window.

    The three states are the three things the user can see on screen: nothing
    is recording, the app is recording, or recording has just stopped and one
    more snap would submit what was dictated. Every state has exactly one snap
    gesture leading out of it, so a snap is never ambiguous and there is always
    a way out - which is the part the earlier design got wrong. Requiring a
    double snap to stop meant that when the pair was missed there was no escape
    at all, and the mic stayed open until a 180-second timer noticed.

    Sending is still the only action that needs two snaps, because it is the
    only one that cannot be undone. The other two toggle a microphone.
    """
    if state == IDLE:
        if start_gate.offer(ev):
            return "start", ""
        return None, "still in trigger cooldown"

    if state == RECORDING:
        # A snap has a tail, and the app takes a moment to actually open the
        # mic. Without this, three fast snaps would start, stop and send in
        # under a second - putting whatever was already in the composer in
        # front of Claude.
        if held_ms < cfg["min_recording_ms"]:
            return None, "only %.0f ms into recording; need %.0f to stop" % (
                held_ms, cfg["min_recording_ms"])
        return "stop", ""

    # SETTLING: dictation is already off and this snap decides whether to send.
    if held_ms > cfg["send_window_ms"]:
        return None, "%.0f ms after the stop; send window is %.0f" % (
            held_ms, cfg["send_window_ms"])
    if not strict.accepts(ev["peak_db"], ev["tail_hf"], ev["decay_ms"]):
        return None, "too dull to confirm a send (tail %.2f < %.2f); ignored" % (
            ev["tail_hf"], cfg["strict_tail_hf_ratio_min"])
    return "send", ""


def drain(q):
    """Discard everything queued while we were busy sending keys.

    The send sequence sleeps for send_delay_ms with the stream still running,
    so by the time it returns the second send snap and its room reflections are
    already sitting in the queue. Acting on them would toggle dictation straight
    back on.
    """
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def wait_for_target(cfg, instance, watch):
    """Sleep with the microphone closed until a watched process appears.

    Returns False if a stop was requested while waiting. The mic is closed on
    purpose rather than held idle: an always-open stream keeps the Windows
    recording indicator lit and pins the capture device for the whole logon
    session, which is a bad trade when Claude is not even running.
    """
    announced = False
    while not (watch & running_exes()):
        if instance is not None and stop_requested(instance):
            return False
        if not announced:
            print("[%s] Waiting for %s; microphone closed."
                  % (time.strftime("%H:%M:%S"), "/".join(sorted(watch))))
            announced = True
        time.sleep(cfg["watch_poll_s"])
    return True


def resolve_pending(pending, det, cfg, dry_run, events, state, since,
                    start_gate):
    """Decide a held stop once the room has had its say.

    Returns (state, since, pending). While the measurement window has not
    arrived yet, everything comes back unchanged and the caller tries again on
    the next pass. The keystrokes come from the profile that created the hold,
    not from whatever is in front now - the stop belongs to the window that
    started recording, even if focus has since moved.

    Two ways out. If the speech band is still loud a beat after the transient,
    the speaker never stopped talking, so the transient was part of the speech
    and the stop is dropped. Otherwise the stop goes through, and a snap that
    arrived while we were waiting is honoured as the send confirmation it was
    meant to be.
    """
    mod_vks, key_vk, mod_send, vk_send = pending["keys"]
    prof = pending["prof"]
    lo_ms, hi_ms = cfg["speech_window_ms"]
    level = det.speech_db(pending["ev"]["onset_block"], lo_ms, hi_ms)

    # Recorded, not yet acted on. The guard below asks one question - was the
    # room loud just after the transient - and measurement says that question
    # cannot tell a wanted stop from an unwanted one. Across 80 stops in a real
    # session the level after was a median 6 dB whether the user kept the stop
    # or immediately snapped dictation back on. Every other number already in
    # this log failed the same test: attack, crest, tail_hf, decay, peak and
    # the length of the recording all overlap between the two cases.
    #
    # What the room was doing just BEFORE the transient is a different question,
    # and one nothing here has ever asked. A deliberate stop comes after the
    # speaker has finished a sentence; a false one lands in the middle of it. The
    # detector already keeps 1741 ms of history, so the answer costs nothing but
    # a log column. It changes no decision. It is here so that ordinary use
    # labels itself: a stop the user did not want is the one they undo, and that
    # undo is already visible in this file.
    before = det.speech_db(pending["ev"]["onset_block"], -800, -100)
    was = "" if before is None else "  [before %.0f dB]" % before
    waited = (time.monotonic() - pending["at"]) * 1000.0
    if level is None and waited < PENDING_TIMEOUT_MS:
        return state, since, pending

    stamp = time.strftime("%H:%M:%S")
    if level is not None and level >= cfg["speech_over_floor_db"]:
        print("[%s] snap    %s  still talking %.0f dB over the floor "
              "%.0f-%.0f ms later; not a stop%s"
              % (stamp, pending["detail"], level, lo_ms, hi_ms, was))
        return state, since, None

    heard = "quiet" if level is None else "%.0f dB over the floor" % level
    heard += was
    if dry_run:
        print("[%s] TRIGGER %s  %s after; would stop (dry run)"
              % (stamp, pending["detail"], heard))
    elif send_key_if_focused(mod_vks, key_vk, prof, cfg, "the stop"):
        print("[%s] TRIGGER %s  %s after  -> %s  %s dictation OFF"
              % (stamp, pending["detail"], heard, prof["activate"],
                 prof["name"]))
    else:
        # The stop never landed, so the app it belonged to is still recording.
        # Leave the state alone - when the user comes back to that window their
        # next snap reads as the stop it was always meant to be. Only the hold
        # is dropped, and with it anything queued as a send confirmation:
        # submitting into a window we did not stop is the same mistake one step
        # further on.
        return state, since, None
    state, since = SETTLING, time.monotonic()
    start_gate.reset()

    follow = pending["follow"]
    if follow is not None:
        if dry_run:
            print("[%s] TRIGGER %s  would send (dry run)"
                  % (stamp, pending["detail"]))
        else:
            time.sleep(cfg["send_delay_ms"] / 1000.0)
            if send_key_if_focused(mod_send, vk_send, prof, cfg, "the submit"):
                print("[%s] TRIGGER %s  -> %s  SENT"
                      % (time.strftime("%H:%M:%S"), pending["detail"],
                         prof["send"]))
            drain(events)
        state, since = IDLE, time.monotonic()
        start_gate.reset()
    return state, since, None


class Recorder:
    """Write every block the detector hears to a 16-bit mono WAV.

    Exists so tuning does not need a person. A recorded session can be replayed
    through the detector any number of times with different thresholds or a
    different feature definition, which is the only honest way to compare two
    candidate rules: on identical audio. Blocks are queued in the audio callback
    and written from the main loop, because a file write inside the callback can
    stall the stream and drop the very transient we are trying to measure.

    Block N of the session starts at sample N * blocksize of the file, so an
    event logged with its block index points at an exact offset in the WAV.
    """

    def __init__(self, path, samplerate):
        self.path = Path(path)
        self.q = queue.Queue()
        self.wav = wave.open(str(self.path), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(int(samplerate))
        self.blocks = 0

    def feed(self, block):
        """Called from the audio callback. Copies, because the driver reuses
        the buffer as soon as the callback returns."""
        self.q.put(np.array(block, copy=True))

    def drain(self):
        while True:
            try:
                block = self.q.get_nowait()
            except queue.Empty:
                return
            clipped = np.clip(block, -1.0, 1.0)
            self.wav.writeframes((clipped * 32767.0).astype("<i2").tobytes())
            self.blocks += 1

    def close(self):
        self.drain()
        self.wav.close()


def listen(cfg, dry_run, instance, watch, record=None):
    """Hold the microphone and act on snaps. Returns STOP or GONE.

    The detector is built here rather than passed in so that every listening
    session starts with a fresh noise floor. Re-opening the mic usually means
    the room changed while it was closed, and a stale EMA would spend its first
    seconds either deaf or trigger-happy.
    """
    det = SnapDetector(cfg)
    # A second detector built on the strict profile, used only for .accepts().
    # Reusing the class keeps the stop gate and the start gate provably the
    # same logic, differing in thresholds alone.
    strict = SnapDetector(strict_profile(cfg))
    start_gate = TriggerGate(cfg, det.block_ms)
    state, since = IDLE, time.monotonic()
    active = None             # the profile that owns the current dictation state
    pending = None            # a stop waiting on the silence check
    events = queue.Queue()

    parsed = {}

    def keys_for(prof):
        """(activate mods, activate vk, send mods, send vk) for a profile.

        Parsed once per profile and cached. A profile with no send key gets
        (None, None) for that half rather than a guess, so a mode that never
        submits cannot accidentally acquire the ability to.
        """
        name = prof["name"]
        if name not in parsed:
            act = parse_key(prof["activate"])
            snd = parse_key(prof["send"]) if prof.get("send") else (None, None)
            parsed[name] = (act[0], act[1], snd[0], snd[1])
        return parsed[name]

    def cb(indata, frames, time_info, status):
        if record is not None:
            record.feed(indata[:, 0])
        ev = det.push(indata[:, 0])
        if ev is not None:
            events.put(ev)

    dev = sd.query_devices(cfg["device"] if cfg["device"] is not None
                           else sd.default.device[0])
    print("[%s] Listening on: %s" % (time.strftime("%H:%M:%S"), dev["name"]))
    if dry_run:
        print("  DRY RUN - nothing is sent.")
    if record is not None:
        print("  recording audio to %s" % record.path.name)
    # What a snap does now depends on the window, so the banner lists the
    # routing rather than one gesture set. Profiles that cannot send are shown
    # too, and shown as such - "nothing happened" should be answerable from the
    # top of the log without opening the config.
    print("  routing:")
    for prof in profiles_of(cfg):
        where = prof["process"] + (" [%s]" % prof["title"]
                                   if prof.get("title") else "")
        if not profile_ready(prof):
            why = ("no key yet" if not prof.get("activate")
                   else "disabled: %s untested" % prof["activate"])
            print("    %-34s %-11s -- nothing sent (%s)"
                  % (where, prof["mode"], why))
        elif prof["mode"] == "dictation":
            print("    %-34s %-11s snap %s on/off, snap twice -> %s"
                  % (where, prof["mode"], prof["activate"], prof["send"]))
        else:
            print("    %-34s %-11s snap -> %s"
                  % (where, prof["mode"], prof["activate"]))

    with open_stream(cfg, cb):
        last_seen = time.monotonic()
        next_poll = 0.0
        while True:
            if record is not None:
                record.drain()
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + cfg["watch_poll_s"]
                if instance is not None and stop_requested(instance):
                    print("[%s] Stop requested." % time.strftime("%H:%M:%S"))
                    return STOP
                if watch:
                    if watch & running_exes():
                        last_seen = now
                    elif now - last_seen > cfg["watch_grace_s"]:
                        print("[%s] %s is gone; releasing the mic."
                              % (time.strftime("%H:%M:%S"),
                                 "/".join(sorted(watch))))
                        return GONE
            try:
                ev = events.get(timeout=0.05 if pending else 0.2)
            except queue.Empty:
                ev = None
            # Re-read the clock: the get() above may have blocked, and both
            # timeouts below are shorter than that block.
            now = time.monotonic()
            held_ms = (now - since) * 1000.0

            # The send window closes on its own, so that a snap a few seconds
            # after a stop reads as "start again" rather than "send".
            if state == SETTLING and held_ms > cfg["send_window_ms"]:
                state, since = IDLE, now
                held_ms = 0.0
            # Nothing tells us when the app stops recording on its own, so a
            # dictation that self-terminated on silence would otherwise leave
            # this stuck in RECORDING, one full cycle out of step.
            elif state == RECORDING and held_ms > cfg["recording_max_s"] * 1000.0:
                print("[%s] nothing for %.0fs; assuming dictation already "
                      "stopped." % (time.strftime("%H:%M:%S"),
                                    cfg["recording_max_s"]))
                state, since = IDLE, now
                held_ms = 0.0
            if pending is not None:
                # A snap that lands while a stop is held is not a second stop -
                # it is the confirmation that this message should be sent, and
                # it has to survive until the stop it confirms actually happens.
                if ev is not None:
                    if strict.accepts(ev["peak_db"], ev["tail_hf"],
                                      ev["decay_ms"]):
                        pending["follow"] = ev
                        print("[%s] held    %s  holding as a send confirmation"
                              % (time.strftime("%H:%M:%S"), pending["detail"]))
                    ev = None
                state, since, pending = resolve_pending(
                    pending, det, cfg, dry_run,
                    events, state, since, start_gate)
                continue

            if ev is None:
                continue

            stamp = time.strftime("%H:%M:%S")
            detail = ("blk %7d  peak %6.1f dB  noise %6.1f dB  onset_hf %.2f  "
                      "tail_hf %.2f  decay %5.1f ms  attack %5.2f ms  "
                      "crest %5.1f"
                      % (ev["block"], ev["peak_db"], ev["noise_db"], ev["onset_hf"],
                         ev["tail_hf"], ev["decay_ms"], ev["attack_ms"],
                         ev["crest"]))

            # Resolve the window before the gates, not after. A snap that lands
            # while something unrouted is in front has nothing to do with us,
            # and letting it reach start_gate would burn the trigger cooldown -
            # so the next snap, the real one, would be swallowed.
            exe, title = foreground_window()
            prof = resolve_profile(exe, title, cfg)
            where = "%s%s" % (exe, (" [%s]" % loggable(title)) if title else "")

            if prof is None:
                print("[%s] snap    %s  ignored, focus=%s" % (stamp, detail, where))
                continue
            if not profile_ready(prof):
                why = ("no activate key set" if not prof.get("activate")
                       else "profile disabled")
                print("[%s] snap    %s  %s matched but %s; nothing sent"
                      % (stamp, detail, prof["name"], why))
                continue

            # A dictation session belongs to the window that started it. If the
            # user has moved on to a different app, we cannot stop the old one
            # from here, and carrying its state over would make the next snap
            # mean "stop" in a window that never started. Drop to idle and let
            # this snap be read fresh under the profile actually in front.
            if active is not None and prof["name"] != active["name"] \
                    and state != IDLE:
                print("[%s] focus   moved %s -> %s; dropping the held %s state"
                      % (stamp, active["name"], prof["name"], state))
                state, since, active = IDLE, time.monotonic(), None
                start_gate.reset()
                held_ms = 0.0

            mod_vks, key_vk, mod_send, vk_send = keys_for(prof)

            # A voice agent listens and answers on its own, so there is nothing
            # to stop and nothing to submit. Judge the snap exactly as a start
            # is judged, press the key, and stay idle.
            if prof.get("mode") == "oneshot":
                action, note = classify(IDLE, ev, 0.0, cfg, strict, start_gate)
                if action is None:
                    print("[%s] snap    %s  %s" % (stamp, detail, note))
                    continue
                if dry_run:
                    print("[%s] TRIGGER %s  focus=%s  would send %s to %s "
                          "(dry run)" % (stamp, detail, where,
                                         prof["activate"], prof["name"]))
                else:
                    send_key(mod_vks, key_vk)
                    print("[%s] TRIGGER %s  -> %s  %s activated"
                          % (stamp, detail, prof["activate"], prof["name"]))
                state, since, active = IDLE, time.monotonic(), None
                start_gate.reset()
                continue

            action, note = classify(state, ev, held_ms, cfg, strict, start_gate)
            if action is None:
                print("[%s] snap    %s  %s" % (stamp, detail, note))
                continue

            if dry_run and action != "stop":
                print("[%s] TRIGGER %s  focus=%s  would %s in %s (dry run)"
                      % (stamp, detail, where, action, prof["name"]))
            elif action == "start":
                send_key(mod_vks, key_vk)
                print("[%s] TRIGGER %s  -> %s  %s dictation ON"
                      % (stamp, detail, prof["activate"], prof["name"]))
            elif action == "stop":
                if cfg["confirm_stop_with_silence"]:
                    pending = {"ev": ev, "detail": detail, "prof": prof,
                               "keys": (mod_vks, key_vk, mod_send, vk_send),
                               "at": time.monotonic(), "follow": None}
                    print("[%s] hold    %s  checking whether the talking stops"
                          % (stamp, detail))
                    det.expect_pair()
                    continue
                send_key(mod_vks, key_vk)
                print("[%s] TRIGGER %s  -> %s  dictation OFF (snap again "
                      "within %.0f ms to send)"
                      % (stamp, detail, prof["activate"], cfg["send_window_ms"]))
                det.expect_pair()
            else:
                # The activate key has already been pressed; the transcript is
                # still landing. Wait out whatever is left of send_delay_ms
                # measured from that keypress, not from this snap.
                rest = cfg["send_delay_ms"] - held_ms
                if rest > 0:
                    time.sleep(rest / 1000.0)
                if send_key_if_focused(mod_send, vk_send, prof, cfg,
                                       "the submit"):
                    print("[%s] TRIGGER %s  -> %s  SENT (confirmed %.0f ms "
                          "after the stop)"
                          % (stamp, detail, prof["send"], held_ms))
                drain(events)

            state = {"start": RECORDING, "stop": SETTLING, "send": IDLE}[action]
            active = prof if state != IDLE else None
            since = time.monotonic()
            start_gate.reset()


def cmd_replay(cfg, path):
    """Run a recorded WAV back through the detector and print what it finds.

    The point of recording is that tuning stops needing a person in a room. A
    threshold change can be judged against the same audio that produced the
    last set of complaints, instead of against a fresh performance that differs
    in a dozen uncontrolled ways.
    """
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            print("Expected 16-bit mono; got %d channels at %d bytes."
                  % (wav.getnchannels(), wav.getsampwidth()))
            return 1
        rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if rate != cfg["samplerate"]:
        print("WARNING: file is %d Hz but the config says %d; the block grid "
              "and every millisecond below will be off." % (rate, cfg["samplerate"]))

    n = cfg["blocksize"]
    lo_ms, hi_ms = cfg["speech_window_ms"]
    det = SnapDetector(cfg)
    found = 0
    open_evs = []
    print("%s: %.1f s, %d blocks" % (path.name, len(audio) / rate, len(audio) // n))

    def report(ev, level):
        """One line per detection, with the silence verdict once it is known."""
        if level is None:
            verdict = "  speech  n/a"
        elif level >= cfg["speech_over_floor_db"]:
            verdict = "  speech %+6.1f dB  STILL TALKING - stop refused" % level
        else:
            verdict = "  speech %+6.1f dB  quiet - stop allowed" % level
        print("  %8.2f s  blk %7d  peak %6.1f dB  onset_hf %.2f  tail_hf %.2f  "
              "decay %5.1f ms  attack %5.2f ms  crest %5.1f%s"
              % (ev["onset_block"] * n / rate, ev["block"], ev["peak_db"],
                 ev["onset_hf"], ev["tail_hf"], ev["decay_ms"],
                 ev["attack_ms"], ev["crest"], verdict))

    for i in range(0, len(audio) - n + 1, n):
        ev = det.push(audio[i:i + n])
        if ev is not None:
            found += 1
            open_evs.append(ev)
        # An event can only be judged once the window after it has gone past,
        # so each one is held here exactly as the live listener holds it.
        still = []
        for pend in open_evs:
            level = det.speech_db(pend["onset_block"], lo_ms, hi_ms)
            if level is None and det.block_index < pend["onset_block"] + int(
                    round(hi_ms / det.block_ms)) + 2:
                still.append(pend)
            else:
                report(pend, level)
        open_evs = still
    for pend in open_evs:
        report(pend, det.speech_db(pend["onset_block"], lo_ms, hi_ms))
    print("%d detections" % found)
    return 0


def cmd_run(cfg, dry_run, config_path, follow=False,
            record_path=None):
    """Run the listener, optionally following the Claude app in and out.

    Without --follow this holds the mic until interrupted. With it, the mic is
    opened when Claude appears and closed when Claude leaves, and the process
    stays alive in between - which is what lets a single logon-time task cover
    every way the app might be started.
    """
    # Unconditional. Two listeners on one microphone both hear every snap and
    # both send the keystroke, so a dictation toggle turns on and straight back
    # off - and neither log looks wrong, because from inside either process
    # nothing is. There is no case where running a second one is what someone
    # wanted: comparing two configs is --replay's job, on identical audio.
    instance = claim_instance()
    if instance is None:
        print("[%s] another listener already has the microphone; exiting."
              % time.strftime("%H:%M:%S"))
        return 0

    watch = watch_set(cfg) if follow else set()
    if not config_path.exists():
        print("WARNING: no config.json - running on permissive defaults, so")
        print("         keyboard clicks may trigger it. Run --calibrate first.")
        print()
    print("Ctrl+C to stop, or: python snap_to_dictate.py --stop")
    print()

    record = (Recorder(record_path, cfg["samplerate"])
              if record_path is not None else None)
    try:
        while True:
            if follow and not wait_for_target(cfg, instance, watch):
                print("[%s] Stop requested." % time.strftime("%H:%M:%S"))
                break
            if listen(cfg, dry_run, instance, watch, record) == STOP:
                break
            if not follow:
                break
    finally:
        if record is not None:
            record.close()
            print("[%s] wrote %s (%d blocks)"
                  % (time.strftime("%H:%M:%S"), record.path.name,
                     record.blocks))
    return 0


TERMINALS = frozenset({
    "windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe",
    "conhost.exe", "wezterm-gui.exe", "alacritty.exe", "kitty.exe",
    "hyper.exe", "mintty.exe",
})


def cmd_verify(cfg, config_path, as_json=False):
    """Check an installation and say plainly what is wrong with it.

    Setup instructions are prose, and prose cannot tell an installer whether it
    succeeded. This is the machine-readable other half: an agent handed this
    repository runs one command and gets a verdict on its own work - whether the
    dependencies import, whether a microphone actually delivers samples, whether
    the config it wrote parses and routes, and whether anything is already
    listening. Exit status is 0 only when nothing FAILed, so it composes into a
    script without parsing the text.

    Deliberately harmless, because it runs on a machine whose state nobody has
    checked yet. It presses no keys - a verification step that typed ctrl+d into
    whatever happened to be in front would be worse than no verification at all
    - and it holds the input stream for under half a second, releasing it before
    a running listener would notice.

    Three outcomes, and the difference between them matters. FAIL means the tool
    cannot work as installed. WARN means it can, but something a person chose is
    worth seeing. OK means checked and good, and never means assumed.
    """
    out = []

    def note(status, label, detail=""):
        out.append({"status": status, "check": label, "detail": detail})

    # ---- interpreter and dependencies -------------------------------------
    v = sys.version_info
    note("OK" if v >= (3, 8) else "FAIL", "python 3.8 or newer",
         "%d.%d.%d" % (v.major, v.minor, v.micro))
    note("OK" if sys.platform == "win32" else "FAIL", "running on Windows",
         "%s - SendInput and the window APIs are Windows-only" % sys.platform)
    for mod in ("numpy", "sounddevice"):
        try:
            m = __import__(mod)
            note("OK", "%s importable" % mod, getattr(m, "__version__", "?"))
        except Exception as exc:
            note("FAIL", "%s importable" % mod, str(exc))

    # ---- the microphone ----------------------------------------------------
    # Opened for real rather than merely enumerated. A device can be listed and
    # still refuse to open - claimed in exclusive mode by another application,
    # or disabled at the driver - so an install that only read the device list
    # would report success on a machine that cannot hear anything.
    try:
        devs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        note("OK" if devs else "FAIL", "an input device exists",
             "%d found" % len(devs))
    except Exception as exc:
        devs = []
        note("FAIL", "an input device exists", str(exc))

    if devs:
        blocks = []
        try:
            with open_stream(cfg, lambda ind, n, t, st:
                             blocks.append(float(np.abs(ind).max()))):
                time.sleep(0.4)
            note("OK" if blocks else "FAIL", "the stream delivers audio",
                 "%d blocks in 400 ms" % len(blocks))
            if blocks:
                loudest = max(blocks)
                # Digital silence is the signature of a muted or wrong device
                # and it is invisible in the device list. A warning rather than
                # a failure, because a genuinely silent room produces it too.
                quiet = loudest == 0.0
                note("WARN" if quiet else "OK",
                     "the samples are not digital silence",
                     "peak %.5f%s"
                     % (loudest, "  <- muted, or the wrong device" if quiet
                        else ""))
        except Exception as exc:
            note("FAIL", "the stream delivers audio", str(exc))

    # ---- the config --------------------------------------------------------
    note("OK" if config_path.exists() else "WARN", "config file present",
         str(config_path) if config_path.exists()
         else "not written yet, so the built-in defaults are in use")

    # Unknown keys are how a typo hides. load_config merges over DEFAULTS, so a
    # misspelled key is accepted in silence while the setting it was meant to
    # change keeps its old value, and nothing else in the tool ever mentions it.
    unknown = sorted(k for k in cfg if k not in DEFAULTS)
    note("OK" if not unknown else "FAIL", "no unrecognised config keys",
         "none" if not unknown else ", ".join(unknown))

    profiles = cfg.get("profiles") or []
    note("OK" if profiles else "WARN", "at least one app profile",
         "%d wired" % len(profiles) if profiles
         else "none, so the legacy single-app path will be used")

    # Only profiles that are switched on have to be complete. A disabled entry
    # with no activate key is a deliberate placeholder - the app is wired up and
    # waiting for somebody to find its dictation shortcut - and profile_ready()
    # already refuses to send for one. The first run of this check called two
    # such placeholders a failure, which would have sent an installing agent off
    # to fix a config that was correct.
    bad, waiting = [], []
    for prof in profiles:
        name = prof.get("name")
        if not name:
            bad.append("a profile has no name")
            continue
        if not prof.get("process") and not prof.get("title"):
            bad.append("%s matches on neither process nor title" % name)
        if not prof.get("enabled"):
            if not prof.get("activate"):
                waiting.append(name)
            continue
        if not prof.get("activate"):
            bad.append("%s is enabled but has no activate key" % name)
        for field in ("activate", "send"):
            if not prof.get(field):
                continue
            try:
                parse_key(prof[field])
            except Exception as exc:
                bad.append("%s: %s %s" % (name, field, exc))
    live_profiles = [p for p in profiles if p.get("enabled")]
    note("OK" if not bad else "FAIL", "every enabled profile is complete",
         "all %d" % len(live_profiles) if not bad else "; ".join(bad))
    if waiting:
        note("WARN", "profiles waiting to be configured",
             "%s - disabled, no activate key set yet" % ", ".join(waiting))

    # ---- the safety property ----------------------------------------------
    # ctrl+d is dictation in the Claude desktop app and end-of-input in every
    # shell, so a stop that lands in a terminal closes it - and when that
    # terminal is running an agent, the agent dies mid-task. No profile may ever
    # match one. The test suite asserts this as well; it is repeated here
    # because somebody editing config.json by hand never runs the test suite.
    caught = sorted(t for t in TERMINALS if resolve_profile(t, "", cfg))
    note("OK" if not caught else "FAIL", "no profile matches a terminal",
         "none of the %d checked" % len(TERMINALS) if not caught
         else "MATCHES " + ", ".join(caught))

    # ---- what is running ---------------------------------------------------
    # claim_instance only asks whether the named event already exists; it never
    # signals it, so this cannot stop a listener it finds. The handle is closed
    # straight away in the other case, or this check would itself become the
    # running instance and block the real one from starting.
    handle = claim_instance()
    if handle is None:
        note("OK", "listener status", "one is already running")
    else:
        kernel32.CloseHandle(handle)
        note("WARN", "listener status",
             "not running - start it with: python autostart.py")

    wanted = {p["process"].lower() for p in profiles if p.get("process")}
    live = sorted(wanted & running_exes())
    note("OK" if live else "WARN", "a wired app is running",
         ", ".join(live) if live else "none of the wired apps is open")

    if as_json:
        print(json.dumps({"checks": out,
                          "failed": [c["check"] for c in out
                                     if c["status"] == "FAIL"]}, indent=2))
        return 1 if any(c["status"] == "FAIL" for c in out) else 0

    print("=" * 68)
    print("  Verifying this installation")
    print("=" * 68)
    for c in out:
        print("  [%-4s] %-34s %s" % (c["status"], c["check"], c["detail"]))
    failed = sum(1 for c in out if c["status"] == "FAIL")
    warned = sum(1 for c in out if c["status"] == "WARN")
    print("")
    if failed:
        print("  %d check(s) failed. The tool will not work until they are "
              "fixed." % failed)
    elif warned:
        print("  Everything required is in place. %d thing(s) above are worth "
              "a look." % warned)
    else:
        print("  Everything checked out.")
    return 1 if failed else 0


def utf8_output():
    """Make stdout and stderr incapable of killing this process.

    Under pythonw there is no console, so autostart.py hands the listener a
    pipe into snap.log. Python then picks an encoding for stdout the way it
    always does for a non-console stream, from the system locale, which on a
    Windows install in most of the world is a legacy codepage rather than
    UTF-8. Printing one character that codepage cannot represent raises
    UnicodeEncodeError from inside print(), and print() is called on the
    normal path for every snap.

    That is exactly how a listener died here after several hours: some window
    put a zero-width space in its title, the log line carrying that title could
    not be encoded, and the traceback ended the process. From the outside the
    tool simply stopped working, and it stayed stopped until the next logon,
    because the scheduled task only fires then.

    errors="replace" is the part that matters more than the encoding. UTF-8 can
    encode anything, but this program also runs by hand in a real console whose
    codepage is whatever the user has, and a logging call is never worth an
    exception. An unprintable character becomes a substitution mark and the
    listener keeps listening.

    Older interpreters, and anything that has already replaced sys.stdout, are
    left alone rather than forced. Failing to improve the stream is not a
    reason to refuse to start.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main():
    utf8_output()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--device", type=int, help="input device index")
    ap.add_argument("--key", help="override the key to send, e.g. ctrl+d")
    ap.add_argument("--double", action="store_true", help="require a double snap")
    ap.add_argument("--single", action="store_true", help="require a single snap")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--whoami", action="store_true", help="print the focused process")
    ap.add_argument("--calibrate", action="store_true",
                    help="record the seven calibration passes, then derive a config")
    ap.add_argument("--derive", type=Path, metavar="PATH.wav",
                    help="re-derive from a past calibration recording")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--replay", type=Path, metavar="PATH.wav",
                    help="run a recorded session back through the detector")
    ap.add_argument("--record", type=Path, metavar="PATH.wav",
                    help="also save the raw audio, so the session can be "
                         "replayed through the detector offline")
    # Kept as an accepted no-op: the scheduled task registered by autostart.py
    # already carries it on its command line, and dropping it here would make
    # that task fail at the next logon with nothing to explain why.
    ap.add_argument("--singleton", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--follow", action="store_true",
                    help="hold the mic only while a wired app is running")
    ap.add_argument("--stop", action="store_true",
                    help="ask a background listener to shut down")
    ap.add_argument("--save-good", action="store_true",
                    help="snapshot the current config as the known-good fallback")
    ap.add_argument("--restore", action="store_true",
                    help="put the known-good fallback back into config.json")
    ap.add_argument("--verify", action="store_true",
                    help="check this installation and exit non-zero if broken")
    ap.add_argument("--json", action="store_true",
                    help="machine-readable output, currently for --verify only")
    ap.add_argument("--test-key", action="store_true",
                    help="send the keystroke on a countdown, no mic involved")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0
    good = args.config.with_name("config.known-good.json")
    if args.save_good:
        good.write_text(args.config.read_text(encoding="utf-8"),
                        encoding="utf-8")
        print("Saved %s as the fallback." % good.name)
        return 0
    if args.restore:
        if not good.exists():
            print("No %s to restore from." % good.name)
            return 1
        args.config.write_text(good.read_text(encoding="utf-8"), encoding="utf-8")
        print("Restored %s from %s. Restart the listener to pick it up."
              % (args.config.name, good.name))
        return 0
    if args.stop:
        return cmd_stop()

    cfg = load_config(args.config)
    if args.device is not None:
        cfg["device"] = args.device
    if args.key:
        # With profiles in play this is a candidate keystroke to try against a
        # window, not a config change - the profile supplies the real key once
        # the candidate is confirmed. It still overrides cfg["key"] so that a
        # pre-profiles config keeps behaving the way it always did.
        cfg["key"] = args.key
    if args.double:
        cfg["require_double"] = True
    if args.single:
        cfg["require_double"] = False

    if args.verify:
        return cmd_verify(cfg, args.config, as_json=args.json)
    if args.whoami:
        return cmd_whoami(cfg)
    if args.test_key:
        return cmd_test_key(cfg, override=args.key)
    if args.calibrate:
        return cmd_calibrate(cfg, args.config)
    if args.derive is not None:
        return cmd_derive(cfg, args.derive, args.config)
    if args.replay:
        return cmd_replay(cfg, args.replay)
    return cmd_run(cfg, args.dry_run, args.config, record_path=args.record,
                   follow=args.follow)


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("This script uses the Windows SendInput API; Windows only.")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
