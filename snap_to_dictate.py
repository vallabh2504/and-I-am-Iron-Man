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


def cmd_calibrate(cfg, config_path):
    print("Calibration - sit where you normally sit.\n")
    time.sleep(0.4)
    quiet = collect_hf(cfg, 3.0, "Stay quiet for 3 seconds...")
    noise = float(np.median(quiet))
    print("    noise floor: %.1f dB\n" % db(noise))

    time.sleep(0.6)
    snaps = collect_hf(cfg, 8.0, "Now snap 5 times, about a second apart...")
    peaks = np.sort(snaps)[-5:]
    weakest = float(peaks[0])
    print("    5 loudest transients: %s dB" % ["%.1f" % db(p) for p in peaks])
    print("    weakest snap: %.1f dB\n" % db(weakest))

    headroom_db = db(weakest) - db(noise)
    if headroom_db < 12:
        print("    WARNING: only %.1f dB between noise and snap." % headroom_db)
        print("    Snap closer to the mic, or quiet the room, and re-run.\n")

    # Put the threshold at the geometric mean of noise floor and weakest snap,
    # i.e. halfway between them in dB - equal margin against both mistakes.
    ratio = float(np.sqrt(weakest / max(noise, EPS)))
    cfg["noise_ratio_thresh"] = round(min(max(ratio, 6.0), 300.0), 2)
    cfg["abs_floor_db"] = round(db(noise) + headroom_db * 0.35, 1)

    config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print("    noise_ratio_thresh = %s" % cfg["noise_ratio_thresh"])
    print("    abs_floor_db       = %s" % cfg["abs_floor_db"])
    print("\nWrote %s" % config_path)
    print("Next: python snap_to_dictate.py --dry-run")
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
    where = "%s%s" % (exe, (" [%s]" % title) if title else "")
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
    waited = (time.monotonic() - pending["at"]) * 1000.0
    if level is None and waited < PENDING_TIMEOUT_MS:
        return state, since, pending

    stamp = time.strftime("%H:%M:%S")
    if level is not None and level >= cfg["speech_over_floor_db"]:
        print("[%s] snap    %s  still talking %.0f dB over the floor "
              "%.0f-%.0f ms later; not a stop"
              % (stamp, pending["detail"], level, lo_ms, hi_ms))
        return state, since, None

    heard = "quiet" if level is None else "%.0f dB over the floor" % level
    if dry_run:
        print("[%s] TRIGGER %s  %s after; would stop (dry run)"
              % (stamp, pending["detail"], heard))
    else:
        send_key(mod_vks, key_vk)
        print("[%s] TRIGGER %s  %s after  -> %s  %s dictation OFF"
              % (stamp, pending["detail"], heard, prof["activate"],
                 prof["name"]))
    state, since = SETTLING, time.monotonic()
    start_gate.reset()

    follow = pending["follow"]
    if follow is not None:
        if dry_run:
            print("[%s] TRIGGER %s  would send (dry run)"
                  % (stamp, pending["detail"]))
        else:
            time.sleep(cfg["send_delay_ms"] / 1000.0)
            send_key(mod_send, vk_send)
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
            where = "%s%s" % (exe, (" [%s]" % title) if title else "")

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
                    continue
                send_key(mod_vks, key_vk)
                print("[%s] TRIGGER %s  -> %s  dictation OFF (snap again "
                      "within %.0f ms to send)"
                      % (stamp, detail, prof["activate"], cfg["send_window_ms"]))
            else:
                # The activate key has already been pressed; the transcript is
                # still landing. Wait out whatever is left of send_delay_ms
                # measured from that keypress, not from this snap.
                rest = cfg["send_delay_ms"] - held_ms
                if rest > 0:
                    time.sleep(rest / 1000.0)
                send_key(mod_send, vk_send)
                print("[%s] TRIGGER %s  -> %s  SENT (confirmed %.0f ms after "
                      "the stop)" % (stamp, detail, prof["send"], held_ms))
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=CONFIG_PATH)
    ap.add_argument("--device", type=int, help="input device index")
    ap.add_argument("--key", help="override the key to send, e.g. ctrl+d")
    ap.add_argument("--double", action="store_true", help="require a double snap")
    ap.add_argument("--single", action="store_true", help="require a single snap")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--whoami", action="store_true", help="print the focused process")
    ap.add_argument("--calibrate", action="store_true")
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

    if args.whoami:
        return cmd_whoami(cfg)
    if args.test_key:
        return cmd_test_key(cfg, override=args.key)
    if args.calibrate:
        return cmd_calibrate(cfg, args.config)
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
