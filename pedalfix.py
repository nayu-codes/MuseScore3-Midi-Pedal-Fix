import mido
import sys
import os
import argparse
from bisect import bisect_right


def is_note_event(msg):
    return msg.type in ('note_on', 'note_off')


def build_tempo_map(mid):
    tempo_events = []

    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            if msg.type == 'set_tempo':
                tempo_events.append((abs_tick, msg.tempo))

    tempo_events.sort(key=lambda item: item[0])

    # MIDI default tempo: 500000 microseconds/beat (120 BPM).
    segments = []
    current_tempo = 500000
    current_start = 0

    for tick, tempo in tempo_events:
        if tick == current_start:
            current_tempo = tempo
        else:
            segments.append((current_start, current_tempo))
            current_start = tick
            current_tempo = tempo

    segments.append((current_start, current_tempo))
    starts = [tick for tick, _ in segments]

    return segments, starts


def tempo_at_tick(tick, segments, starts):
    index = bisect_right(starts, tick) - 1
    if index < 0:
        return 500000
    return segments[index][1]


def seconds_between_ticks(start_tick, end_tick, ticks_per_beat, segments, starts):
    if end_tick <= start_tick:
        return 0.0

    total_seconds = 0.0
    current_tick = start_tick

    while current_tick < end_tick:
        idx = bisect_right(starts, current_tick) - 1
        if idx < 0:
            idx = 0

        segment_start, tempo = segments[idx]
        if idx + 1 < len(segments):
            next_change = segments[idx + 1][0]
        else:
            next_change = end_tick

        segment_end = min(end_tick, next_change)
        ticks_in_segment = segment_end - current_tick

        total_seconds += (ticks_in_segment * tempo) / (1_000_000.0 * ticks_per_beat)
        current_tick = segment_end

    return total_seconds


def seconds_to_ticks(seconds, tempo, ticks_per_beat):
    if seconds <= 0:
        return 0
    return int((seconds * 1_000_000.0 * ticks_per_beat + tempo - 1) // tempo)


def normalize_gap_mode(mode):
    aliases = {
        'delay': 'delay_next',
        'd': 'delay_next',
        'delay_next': 'delay_next',
        'shorten': 'shorten_previous',
        's': 'shorten_previous',
        'shorten_previous': 'shorten_previous',
        'both': 'both',
        'b': 'both',
    }
    normalized = aliases.get(mode)
    if normalized is None:
        raise ValueError(f"Invalid gap mode: {mode}")
    return normalized


def process_track_absolute_pedal_gap(
    track,
    track_index,
    ticks_per_beat,
    tempo_segments,
    tempo_starts,
    gap_seconds,
    gap_mode,
    debug=False,
):
    events = []
    abs_tick = 0
    for order, msg in enumerate(track):
        abs_tick += msg.time
        events.append({
            'abs': abs_tick,
            'order': order,
            'msg': msg.copy(time=0),
        })

    total_adjustments = 0
    total_gap_ticks = 0

    if gap_mode not in ('shorten_previous', 'both'):
        raise ValueError(f"Unsupported absolute-gap mode: {gap_mode}")

    last_val = 0
    last_pedal_up_event = None
    last_pedal_down_tick = None

    for ev in events:
        msg = ev['msg']
        if msg.type == 'control_change' and msg.control == 64:
            if msg.value < 64:
                last_pedal_up_event = ev

            if msg.value >= 64 and last_val < 64:
                if last_pedal_up_event is not None:
                    up_tick = last_pedal_up_event['abs']
                    down_tick = ev['abs']
                    elapsed_seconds = seconds_between_ticks(
                        up_tick,
                        down_tick,
                        ticks_per_beat,
                        tempo_segments,
                        tempo_starts,
                    )

                    if elapsed_seconds < gap_seconds:
                        missing_seconds = gap_seconds - elapsed_seconds
                        current_tempo = tempo_at_tick(
                            down_tick,
                            tempo_segments,
                            tempo_starts,
                        )
                        needed_ticks = seconds_to_ticks(
                            missing_seconds,
                            current_tempo,
                            ticks_per_beat,
                        )

                        planned_shorten = needed_ticks if gap_mode == 'shorten_previous' else needed_ticks // 2
                        planned_delay = 0 if gap_mode == 'shorten_previous' else (needed_ticks - planned_shorten)

                        min_up_tick = 0 if last_pedal_down_tick is None else last_pedal_down_tick
                        max_shift = max(0, up_tick - min_up_tick)
                        actual_shortened = min(planned_shorten, max_shift)
                        actual_delayed = planned_delay

                        # If shorten is capped, move the remainder into delay so
                        # the enforced gap is still reached in 'both' mode.
                        if gap_mode == 'both' and actual_shortened < planned_shorten:
                            actual_delayed += (planned_shorten - actual_shortened)

                        if actual_shortened > 0:
                            last_pedal_up_event['abs'] = up_tick - actual_shortened

                        if actual_delayed > 0:
                            ev['abs'] = down_tick + actual_delayed

                        total_adjustments += 1
                        total_gap_ticks += (actual_shortened + actual_delayed)

                        if debug:
                            final_gap = elapsed_seconds + ((actual_shortened + actual_delayed) * current_tempo) / (1_000_000.0 * ticks_per_beat)
                            print(
                                f"[debug] track={track_index} tick={down_tick} "
                                f"elapsed={elapsed_seconds:.6f}s needed_ticks={needed_ticks} "
                                f"shortened={actual_shortened} delayed={actual_delayed} "
                                f"final_gap≈{final_gap:.6f}s"
                            )

            if msg.value >= 64:
                last_pedal_down_tick = ev['abs']

            last_val = msg.value

    events.sort(key=lambda item: (item['abs'], item['order']))

    new_track = mido.MidiTrack()
    prev_abs = 0
    for ev in events:
        delta = ev['abs'] - prev_abs
        prev_abs = ev['abs']
        new_track.append(ev['msg'].copy(time=delta))

    return new_track, total_adjustments, total_gap_ticks

def process_pedal(input_file, output_file, gap_seconds=0.15, gap_mode='delay_next', debug=False):
    if not os.path.exists(input_file):
        print(f"Error: Input file '{input_file}' not found.")
        return

    mid = mido.MidiFile(input_file)
    new_mid = mido.MidiFile(type=mid.type, ticks_per_beat=mid.ticks_per_beat)
    tempo_segments, tempo_starts = build_tempo_map(mid)
    
    print(f"--- Processing: {input_file} ---")

    total_adjustments = 0
    total_gap_ticks = 0

    apply_shorten = gap_mode in ('shorten_previous', 'both')
    apply_delay = gap_mode in ('delay_next', 'both')

    for track_index, track in enumerate(mid.tracks):
        if gap_mode in ('shorten_previous', 'both'):
            new_track, track_adjustments, track_gap_ticks = process_track_absolute_pedal_gap(
                track,
                track_index,
                mid.ticks_per_beat,
                tempo_segments,
                tempo_starts,
                gap_seconds,
                gap_mode,
                debug=debug,
            )
            new_mid.tracks.append(new_track)
            total_adjustments += track_adjustments
            total_gap_ticks += track_gap_ticks
            continue

        new_track = mido.MidiTrack()
        new_mid.tracks.append(new_track)
        
        last_val = 0
        last_pedal_up_abs_tick = None
        last_pedal_up_new_track_idx = None  # Index in new_track where pedal-up was appended
        time_debt = 0 # Tracks how much we've shifted the timeline
        original_abs_tick = 0
        
        for msg in track:
            original_delta = msg.time
            original_abs_tick += original_delta

            # 1. Apply any pending 'time debt' to this message's delta time.
            # If this delta is too small, carry the remaining debt forward.
            if time_debt > 0:
                paid = min(msg.time, time_debt)
                msg.time -= paid
                time_debt -= paid
            
            # 2. Check for Sustain Pedal (CC 64)
            if msg.type == 'control_change' and msg.control == 64:
                if msg.value < 64:
                    last_pedal_up_abs_tick = original_abs_tick
                    # Record where the pedal-up will be stored in new_track (appended at end of loop)
                    last_pedal_up_new_track_idx = len(new_track)

                # If Pedal Down (>=64) follows Pedal Up (<64)
                if msg.value >= 64 and last_val < 64:
                    if last_pedal_up_abs_tick is not None:
                        elapsed_seconds = seconds_between_ticks(
                            last_pedal_up_abs_tick,
                            original_abs_tick,
                            mid.ticks_per_beat,
                            tempo_segments,
                            tempo_starts,
                        )

                        if elapsed_seconds < gap_seconds:
                            missing_seconds = gap_seconds - elapsed_seconds
                            current_tempo = tempo_at_tick(
                                original_abs_tick,
                                tempo_segments,
                                tempo_starts,
                            )
                            needed_ticks = seconds_to_ticks(
                                missing_seconds,
                                current_tempo,
                                mid.ticks_per_beat,
                            )
                        else:
                            needed_ticks = 0

                    else:
                        needed_ticks = 0

                    if needed_ticks > 0:
                        actual_shortened = 0
                        actual_delayed = 0
                        planned_shorten = 0

                        if apply_shorten:
                            # How many ticks to pull from events before the pedal-up.
                            # 'both' splits: half from previous, half to next.
                            planned_shorten = needed_ticks if gap_mode == 'shorten_previous' else needed_ticks // 2
                            pu_idx = last_pedal_up_new_track_idx

                            if pu_idx is not None and pu_idx < len(new_track):
                                # Note-safe shortening: only steal from the pedal-up and
                                # immediately preceding non-note events.  If we would have
                                # to cross a note event, stop. This guarantees note timings
                                # are never changed by shorten_previous/both.
                                remaining_to_steal = planned_shorten
                                steal_idx = pu_idx
                                while steal_idx >= 0 and remaining_to_steal > 0:
                                    if steal_idx != pu_idx and is_note_event(new_track[steal_idx]):
                                        break

                                    can_steal = min(new_track[steal_idx].time, remaining_to_steal)
                                    if can_steal > 0:
                                        new_track[steal_idx].time -= can_steal
                                        actual_shortened += can_steal
                                        remaining_to_steal -= can_steal
                                    steal_idx -= 1

                                if actual_shortened > 0:
                                    # Compensate the first event after the pedal-up so all
                                    # subsequent events remain at their original absolute positions.
                                    next_idx = pu_idx + 1
                                    if next_idx < len(new_track):
                                        new_track[next_idx].time += actual_shortened
                                    else:
                                        # Pedal-down is the immediately next event; compensate it.
                                        msg.time += actual_shortened

                        if apply_delay:
                            # How many ticks to add to the next pedal-down.
                            # 'both' uses ceiling(needed_ticks / 2) so that shorten + delay == needed_ticks.
                            delay_ticks = needed_ticks if gap_mode == 'delay_next' else (needed_ticks - needed_ticks // 2)
                            # If shortening was capped (e.g., pedal-up delta too small),
                            # add the remainder to delay so the minimum gap is still enforced.
                            if gap_mode == 'both' and actual_shortened < planned_shorten:
                                delay_ticks += (planned_shorten - actual_shortened)
                            msg.time += delay_ticks
                            # We must subtract this added time from the NEXT message
                            time_debt += delay_ticks
                            actual_delayed = delay_ticks

                        achieved_ticks = actual_shortened + actual_delayed
                        total_adjustments += 1
                        total_gap_ticks += achieved_ticks

                        if debug:
                            final_gap = elapsed_seconds + (achieved_ticks * current_tempo) / (1_000_000.0 * mid.ticks_per_beat)
                            print(
                                f"[debug] track={track_index} tick={original_abs_tick} "
                                f"elapsed={elapsed_seconds:.6f}s needed_ticks={needed_ticks} "
                                f"shortened={actual_shortened} delayed={actual_delayed} "
                                f"final_gap≈{final_gap:.6f}s"
                            )
                
                last_val = msg.value
            
            new_track.append(msg)

    new_mid.save(output_file)
    print(
        f"--- Summary: adjusted {total_adjustments} pedal transitions, "
        f"enforced {total_gap_ticks} total gap ticks ---"
    )
    print(f"--- Success! Notes are preserved. Saved to {output_file} ---")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix consecutive sustain pedal transitions by enforcing a minimum gap."
    )
    parser.add_argument("input", help="Input MIDI file path")
    parser.add_argument(
        "-g",
        "--gap-seconds",
        type=float,
        default=0.15,
        help="Minimum pedal-up to pedal-down gap in seconds (default: 0.15)",
    )
    parser.add_argument(
        "-m",
        "--gap-mode",
        default="delay",
        help=(
            "How to create the gap between pedal transitions (default: delay). "
            "Short names: delay, shorten, both. "
            "Also accepted: delay_next, shorten_previous, d, s, b."
        ),
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Print per-adjustment debug details",
    )

    args = parser.parse_args()

    try:
        gap_mode = normalize_gap_mode(args.gap_mode)
    except ValueError as exc:
        parser.error(str(exc))

    output_file = args.input.replace(".mid", "_pedalfix.mid")
    # If file already exists, append a number to avoid overwriting
    if os.path.exists(output_file):
        base, ext = os.path.splitext(output_file)
        i = 1
        while os.path.exists(f"{base}_{i}{ext}"):
            i += 1
        output_file = f"{base}_{i}{ext}"

    process_pedal(args.input, output_file, gap_seconds=args.gap_seconds, gap_mode=gap_mode, debug=args.debug)
