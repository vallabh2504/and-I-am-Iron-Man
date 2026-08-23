"""Start the snap listener in the background, exactly once.

Run by a logon scheduled task (see README). It exists only to give the listener
a log file and a detached parent - the task itself cannot redirect output, and a
task that stays "Running" for the whole logon session is harder to reason about
than one that fires and finishes. On success this prints nothing at all.

Deciding when to actually open the microphone is the listener's job, not this
one's: --follow keeps it alive but idle until one of the wired apps is running.

Duplicate launches are deliberately not filtered here. The listener itself holds
a named-event singleton, so a second copy exits before it opens the microphone.
Letting a race happen and lose costs one short-lived process; a lock file checked
here would instead have to be second-guessed for staleness every time.

That tolerance is what lets the scheduled task double as a watchdog. The task
carries two triggers: one at logon, and one that repeats every two minutes
forever. The repeating one runs this launcher over and over against a listener
that is usually already alive, which costs a Python startup and nothing else,
and matters on the run where the listener is gone.

It has been gone. A window put a zero-width space in its title, the log line
carrying that title could not be encoded, and UnicodeEncodeError came out of
print() on the per-snap path. That specific bug is fixed at its root, in
utf8_output() and loggable(). The watchdog is for the next one: an unplugged
microphone, a device claimed in exclusive mode, or a driver reset on resume can
still end the listener, and without a repeating trigger it would stay ended
until the next logon with nothing on screen to say so.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "snap_to_dictate.py"
LOG = HERE / "snap.log"
MAX_LOG_BYTES = 1_000_000

DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def interpreter():
    """pythonw.exe runs with no console window; fall back if it is missing."""
    windowless = Path(sys.executable).with_name("pythonw.exe")
    return str(windowless if windowless.exists() else Path(sys.executable))


def spawn(flags, log):
    # -u because pythonw has no console: this log file is the only place the
    # listener's output can go, and buffering would hide it while it runs.
    subprocess.Popen(
        [interpreter(), "-u", str(SCRIPT), "--singleton", "--follow"],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        cwd=str(HERE), creationflags=flags, close_fds=True,
    )


def main():
    if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
        try:
            LOG.unlink()
        except OSError:
            pass          # a running listener still holds it; append instead
    with open(LOG, "a", encoding="utf-8") as log:
        try:
            # Break out of the task scheduler's job object, otherwise the
            # listener dies the moment this launcher is reaped.
            spawn(DETACHED_PROCESS | CREATE_BREAKAWAY_FROM_JOB, log)
        except OSError:
            spawn(DETACHED_PROCESS, log)   # job forbids breakaway; still fine


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("Windows only.")
    main()
