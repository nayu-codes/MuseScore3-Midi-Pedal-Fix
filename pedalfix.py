import mido
import sys
import os
import argparse
from bisect import bisect_right


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

def process_pedal(input_file, output_file, gap_seconds=0.15, gap_mode='delay_next', debug=False):
    if not os.path.exists(input_file):
        print(f"Error: Input file '{input_file}' not found.")
        return

    mid = mido.MidiFile(input_file)
    new_mid = mido.MidiFile(type=mid.type, ticks_per_beat=mid.ticks_per_beat)
    tempo_segments, tempo_starts = build_tempo_map(mid)
    
    print(f"--- Processing: {input_file} ---")

    total_adjustments = 0
    total_added_ticks = 0

    apply_shorten = gap_mode in ('shorten_previous', 'both')
    apply_delay = gap_mode in ('delay_next', 'both')

    for track_index, track in enumerate(mid.tracks):
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

                        if apply_shorten:
                            # How many ticks to take from the previous pedal-up.
                            # 'both' splits the gap: half from previous, half to next.
                            shorten_ticks = needed_ticks if gap_mode == 'shorten_previous' else needed_ticks // 2
                            pu_idx = last_pedal_up_new_track_idx

                            if pu_idx is not None and pu_idx < len(new_track):
                                # Cap the reduction at the pedal-up's own delta so it never goes negative.
                                reducible = min(new_track[pu_idx].time, shorten_ticks)
                                if reducible > 0:
                                    new_track[pu_idx].time -= reducible
                                    # Compensate the next message so its absolute time is unchanged.
                                    # If no message exists between pedal-up and pedal-down,
                                    # the pedal-down (current msg) is the next message.
                                    next_idx = pu_idx + 1
                                    if next_idx < len(new_track):
                                        new_track[next_idx].time += reducible
                                    else:
                                        msg.time += reducible
                                    actual_shortened = reducible

                        if apply_delay:
                            # How many ticks to add to the next pedal-down.
                            # 'both' uses ceiling(needed_ticks / 2) so that shorten + delay == needed_ticks.
                            delay_ticks = needed_ticks if gap_mode == 'delay_next' else (needed_ticks - needed_ticks // 2)
                            msg.time += delay_ticks
                            # We must subtract this added time from the NEXT message
                            time_debt += delay_ticks
                            actual_delayed = delay_ticks

                        total_adjustments += 1
                        total_added_ticks += needed_ticks

                        if debug:
                            final_gap = elapsed_seconds + (needed_ticks * current_tempo) / (1_000_000.0 * mid.ticks_per_beat)
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
        f"added {total_added_ticks} total ticks ---"
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
        choices=["delay_next", "shorten_previous", "both"],
        default="delay_next",
        help=(
            "How to create the gap between pedal transitions (default: delay_next). "
            "'delay_next' delays the start of the next pedal-down. "
            "'shorten_previous' ends the previous pedal sooner by moving the pedal-up earlier. "
            "'both' splits the gap evenly between both methods, centering it on the original "
            "pedal change point."
        ),
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Print per-adjustment debug details",
    )

    args = parser.parse_args()

    output_file = args.input.replace(".mid", "_pedalfix.mid")
    # If file already exists, append a number to avoid overwriting
    if os.path.exists(output_file):
        base, ext = os.path.splitext(output_file)
        i = 1
        while os.path.exists(f"{base}_{i}{ext}"):
            i += 1
        output_file = f"{base}_{i}{ext}"

    process_pedal(args.input, output_file, gap_seconds=args.gap_seconds, gap_mode=args.gap_mode, debug=args.debug)