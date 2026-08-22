# calibration/

`--calibrate` writes three files here, all named after the moment the recording
started:

| File | What it is |
|---|---|
| `<stamp>.wav` | the seven passes as one recording |
| `<stamp>.passes.json` | where each pass starts and ends, in samples |
| `<stamp>.json` | the journal: what was measured, what was derived, what the acceptance gate said |

A fourth, `<stamp>.replaced.json`, holds the config that was in place before a
successful run overwrote it. It exists because `config.known-good.json` only
remembers one generation back, so two calibrations in a row used to destroy a
hand-tuned fallback.

The recording is kept deliberately. A derivation that turns out to be wrong can
be fixed and re-run against the same audio, without asking anyone to perform
five minutes of passes a second time:

```bash
python ../snap_to_dictate.py --derive <stamp>.wav
```

None of it is committed. The recordings are tens of megabytes of binary, and the
journals describe one person's room, microphone and voice — so they would not
mean anything on another machine, and without the matching `.wav` nobody could
check them anyway.
