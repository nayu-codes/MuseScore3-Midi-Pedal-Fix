# MIDI Pedaling Fix for MuseScore 3

`pedalfix.py` fixes problematic sustain pedal transitions in MIDI files by enforcing a minimum delay between **pedal up** and the next **pedal down**, while preserving musical timing as much as possible.

This project was inspired by DaydreamPiano's work:
https://github.com/DaydreamPiano/MuseScore-Midi-Pedaling-Fix

The goal here is to improve reliability for workflows where previous approaches could cause **track desync**.

## GitHub Repo Description

MIDI sustain pedal fixer for MuseScore 3 generated MIDI files that enforces minimum pedal-up to pedal-down gaps without shifting tracks out of sync.

## Why This Exists

Some MIDI-to-performance pipelines (such as using commercial VSTs) do not handle ultra-short consecutive CC64 transitions well, resulting in overly-sustained notes in playback. The previous fix by DaydreamPiano introduces desync for multi-track midi (such as if two-stave piano was exported as 2 tracks in the midi file). This script improves on that and internally tracks the time-debt of shifting pedal timings, to avoid the same desync issues.

`pedalfix.py` addresses this by:

- Finding pedal-up to next pedal-down transitions (`CC64 < 64` then `CC64 >= 64`)
- Enforcing a minimum time gap in **seconds**
- Applying a balancing `time_debt` mechanism so added delay is compensated on following events
- Respecting tempo changes via a tempo map, so timing is computed in real seconds, not fixed ticks

## Features

- Tempo-aware pedal gap enforcement
- Adjustable minimum gap (`--gap-seconds`)
- Three gap creation modes (`--gap-mode`) to control which side of the pedal change is adjusted
- Debug mode (`--debug`) for per-adjustment logging
- Auto-generated output filename (`*_pedalfix.mid`, with numeric suffix if needed)

## Requirements

- Python 3.8+
- `mido`

Install dependency:

```bash
pip install mido
```

## Usage

Basic:

```bash
python pedalfix.py input.mid
```

With custom gap (example: 120 ms):

```bash
python pedalfix.py input.mid --gap-seconds 0.12
```

With a custom gap mode:

```bash
# Shorten the previous pedal instead of delaying the next one
python pedalfix.py input.mid --gap-mode shorten_previous

# Split the gap evenly between both sides (centers the gap on the pedal change point)
python pedalfix.py input.mid --gap-mode both
```

With debug output:

```bash
python pedalfix.py input.mid --debug
```

Short flags:

```bash
python pedalfix.py input.mid -g 0.15 -m both -d
```

## Input and Output

- Input: any `.mid` file path
- Output: `<input_name>_pedalfix.mid`
- If output already exists: `<input_name>_pedalfix_1.mid`, `_2.mid`, etc.

## How It Works

1. Builds a global tempo map from all `set_tempo` events.
2. Walks each track and monitors sustain pedal control changes (`CC64`).
3. For each pedal-up → pedal-down transition, computes real elapsed time using the tempo map.
4. If elapsed time is below threshold, calculates how many ticks are needed to meet the minimum gap.
5. Applies the correction according to `--gap-mode`:
   - `delay_next` *(default)*: adds the needed ticks to the next pedal-down's delta time, then carries the added time as `time_debt` so subsequent messages are compensated.
   - `shorten_previous`: subtracts the needed ticks from the previous pedal-up's delta time (moving it earlier), then compensates the immediately following message so all other events remain in place.
   - `both`: splits the needed ticks evenly — half is taken from the previous pedal-up (as above) and half is added to the next pedal-down (as above), centering the gap on the original pedal change point.

## Notes

- This script only adjusts sustain pedal timing (`CC64` transitions).
- It does not rewrite notes, velocities, or unrelated CC data intentionally.
- Always audition the output MIDI in your target software after processing.

## License

Licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Inspiration and Credit

Inspired by:

- https://github.com/DaydreamPiano/MuseScore-Midi-Pedaling-Fix
