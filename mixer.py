"""
Audio mixing functionality for Eigenradio
"""

import time
import random
import numpy as np
from datetime import datetime
from typing import List, Optional, Dict, Any
from concurrent.futures import Future, TimeoutError as FutureTimeoutError

from config import RATE, CHANNELS, executor, HOLD_SEC, debug
from audio_core import read_frames, looped_segment
from streaming import open_stream
from station_manager import get_random_station, add_to_play_history

# Timeout for prefetch operations in seconds
PREFETCH_TIMEOUT = 5
# Maximum consecutive prefetch failures before falling back to current station
MAX_PREFETCH_FAILURES = 5

class StreamError(Exception):
    """Exception for stream-related errors"""
    pass

def radio_mixer(stations: List[str], static_pcm, playtime=600, fade=3,
               initial_url: Optional[str] = None, initial_stream = None):
    """
    Core mixer generator that handles crossfading between radio stations

    Args:
        stations: List of station URLs
        static_pcm: Static noise PCM data
        playtime: Duration to play each station in seconds
        fade: Fade duration in seconds
        initial_url: Optional pre-verified initial station URL
        initial_stream: Optional pre-opened stream for initial station
    """
    required = yield b''  # priming handshake

    # Station management
    current_url = initial_url
    current = initial_stream

    # Stream health monitoring
    stream_health: Dict[Any, Dict[str, Any]] = {}

    # For tracking consecutive errors
    consecutive_errors = 0
    # For tracking failed prefetch attempts
    prefetch_failures = 0

    # Initialize with first stream if not provided
    if current is None:
        while current is None:
            try:
                current_url = get_random_station(stations)
                current = open_stream(current_url)
                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)
                consecutive_errors = 0
                # Add the initial station to play history
                add_to_play_history(current_url)
            except Exception as e:
                consecutive_errors += 1
                print(f"Error starting initial stream: {e}")
                if consecutive_errors >= 5:
                    print("Too many consecutive errors, giving up")
                    raise
                time.sleep(1)  # Brief pause before retry
    else:
        # Already have a stream from the caller
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)
        # Add the initial station to play history
        if current_url:
            add_to_play_history(current_url)

    next_switch = time.time() + playtime
    fade_phase = None  # None/out/hold/in
    fade_pos = 0
    static_pos = random.randrange(len(static_pcm)//CHANNELS)

    prefetch_job: Optional[Future] = None
    next_stream = None
    target_url = None

    # Time until we should check stream health again
    next_health_check = time.time() + 5

    # Track prefetch timeouts
    prefetch_start_time = None

    # Track excluded stations for this prefetch cycle
    excluded_for_prefetch = set()

    while True:
        # If current stream is unhealthy, start crossfade to static
        current_time = time.time()
        if current_time >= next_health_check and fade_phase is None:
            try:
                # Simple check - try to get a frame
                if current not in stream_health:
                    stream_health[current] = {
                        'errors': 0,
                        'last_error': None,
                        'silence_count': 0
                    }

                # Schedule next health check
                next_health_check = current_time + 5
            except Exception as e:
                print(f"Stream health check failed: {e}")
                stream_health[current]['errors'] += 1
                stream_health[current]['last_error'] = str(e)

                # If too many errors, find a new station
                if stream_health[current]['errors'] >= 3:
                    debug(f"Stream unhealthy, starting emergency crossfade")
                    # Force a crossfade if not already in one
                    if fade_phase is None and prefetch_job is None:
                        try:
                            target_url = get_random_station(stations, exclude=current_url)
                            excluded_for_prefetch = {current_url}
                            prefetch_job = executor.submit(open_stream, target_url)
                            prefetch_start_time = time.time()
                            prefetch_failures = 0
                            fade_phase, fade_pos = 'out', 0
                            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Emergency switch - prefetching →", target_url)
                        except Exception as e:
                            print(f"Emergency switch failed: {e}")

        # Check for stuck prefetch jobs
        if prefetch_job and prefetch_start_time and time.time() - prefetch_start_time > PREFETCH_TIMEOUT:
            print(f"Prefetch operation timed out after {PREFETCH_TIMEOUT} seconds, canceling and trying another station")
            prefetch_job.cancel()
            prefetch_job = None
            prefetch_start_time = None
            prefetch_failures += 1

            # Add the failed station to our exclusion list
            if target_url:
                excluded_for_prefetch.add(target_url)

            # If we're in hold phase and haven't failed too many times, try another station
            if fade_phase == 'hold' and prefetch_failures < MAX_PREFETCH_FAILURES:
                try:
                    # Get a random station, excluding those we've already tried
                    all_excluded = excluded_for_prefetch.copy()
                    if current_url:
                        all_excluded.add(current_url)
                    target_url = get_random_station(
                        [s for s in stations if s not in all_excluded],
                        exclude=current_url
                    )
                    prefetch_job = executor.submit(open_stream, target_url)
                    prefetch_start_time = time.time()
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Retrying prefetch after timeout ({prefetch_failures}/{MAX_PREFETCH_FAILURES}) →", target_url)
                except Exception as e:
                    print(f"Retry after timeout failed: {e}")
                    # If we've failed too many times, go back to the current station
                    if prefetch_failures >= MAX_PREFETCH_FAILURES:
                        print("Too many consecutive prefetch failures, falling back to current station")
                        # Reset fade to go back to current station
                        fade_phase = None
                        next_switch = time.time() + playtime
                    else:
                        next_switch = time.time() + 10
            elif prefetch_failures >= MAX_PREFETCH_FAILURES:
                # Too many failures, go back to current station
                print("Too many consecutive prefetch failures, falling back to current station")
                fade_phase = None
                next_switch = time.time() + playtime
                prefetch_failures = 0
                excluded_for_prefetch.clear()

        samples = np.zeros(required*CHANNELS, dtype=np.int16)

        try:
            # ───── main mixing paths ───────────────────────────────────────────
            if fade_phase is None:  # steady-state station
                try:
                    samples[:] = read_frames(current, required)

                    # Check for extended silence (could indicate dead stream)
                    if np.max(np.abs(samples)) < 10:  # Very low amplitude
                        if current in stream_health:
                            stream_health[current]['silence_count'] += 1

                            # If too much silence, consider the stream dead
                            if stream_health[current]['silence_count'] > 20:  # ~5 seconds of silence
                                debug("Stream appears to be silent/dead")
                                raise StreamError("Stream is silent")
                    else:
                        # Reset silence counter on normal audio
                        if current in stream_health:
                            stream_health[current]['silence_count'] = 0

                except Exception as e:
                    print(f"Error reading from current stream: {e}")
                    # On error, transition to a new station
                    if prefetch_job is None:
                        try:
                            target_url = get_random_station(stations, exclude=current_url)
                            excluded_for_prefetch = {current_url}
                            prefetch_job = executor.submit(open_stream, target_url)
                            prefetch_start_time = time.time()
                            prefetch_failures = 0
                            fade_phase, fade_pos = 'out', 0
                            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Stream error - prefetching →", target_url)
                        except Exception as fallback_error:
                            print(f"Error during fallback: {fallback_error}")
                    # Return silence for this frame
                    samples[:] = 0

            else:  # some phase of the X-fade
                t1 = fade_pos / (fade * RATE)
                t2 = (fade_pos + required) / (fade * RATE)
                ramp = np.repeat(np.linspace(t1, t2, required, dtype=np.float32),
                                CHANNELS)

                if fade_phase == 'out':  # station → static
                    try:
                        dry = read_frames(current, required)
                    except Exception as e:
                        print(f"Error reading during fade-out: {e}")
                        dry = np.zeros(required*CHANNELS, dtype=np.int16)

                    wet = read_frames(looped_segment(static_pcm, static_pos, required),
                                    required)
                    samples[:] = ((1 - ramp) * dry + ramp * wet).astype(np.int16)
                    fade_pos += required
                    static_pos = (static_pos + required) % (len(static_pcm)//CHANNELS)
                    if fade_pos >= fade*RATE:
                        fade_phase, fade_pos = 'hold', 0

                elif fade_phase == 'hold':  # static full
                    samples[:] = read_frames(looped_segment(static_pcm, static_pos, required),
                                            required)
                    fade_pos += required
                    static_pos = (static_pos + required) % (len(static_pcm)//CHANNELS)

                    # ready? start fade-in
                    if prefetch_job and prefetch_job.done():
                        try:
                            next_stream = prefetch_job.result(timeout=0.5)  # Non-blocking check
                            prefetch_job = None
                            prefetch_start_time = None
                            prefetch_failures = 0
                            excluded_for_prefetch.clear()
                            current_url = target_url
                            fade_phase, fade_pos = 'in', 0
                            next_switch = time.time() + playtime
                            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)

                            # Reset stream health
                            if current in stream_health:
                                del stream_health[current]
                            stream_health[next_stream] = {
                                'errors': 0,
                                'last_error': None,
                                'silence_count': 0
                            }

                        except (Exception, FutureTimeoutError) as e:
                            print(f"Stream failed: {e}")
                            prefetch_job = None
                            prefetch_start_time = None
                            prefetch_failures += 1

                            # Add the failed station to our exclusion list
                            if target_url:
                                excluded_for_prefetch.add(target_url)

                            # Try a different station if we haven't failed too many times
                            if prefetch_failures < MAX_PREFETCH_FAILURES:
                                try:
                                    # Get a random station, excluding those we've already tried
                                    all_excluded = excluded_for_prefetch.copy()
                                    if current_url:
                                        all_excluded.add(current_url)
                                    target_url = get_random_station(
                                        [s for s in stations if s not in all_excluded],
                                        exclude=current_url
                                    )
                                    prefetch_job = executor.submit(open_stream, target_url)
                                    prefetch_start_time = time.time()
                                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Retrying prefetch ({prefetch_failures}/{MAX_PREFETCH_FAILURES}) →", target_url)
                                except Exception as retry_error:
                                    print(f"Retry failed: {retry_error}")
                                    # If we've failed too many times, go back to the current station
                                    if prefetch_failures >= MAX_PREFETCH_FAILURES:
                                        print("Too many consecutive prefetch failures, falling back to current station")
                                        # Reset fade to go back to current station
                                        fade_phase = None
                                        next_switch = time.time() + playtime
                                    else:
                                        next_switch = time.time() + 10  # Short delay before trying again
                            else:
                                # Too many failures, go back to current station
                                print("Too many consecutive prefetch failures, falling back to current station")
                                fade_phase = None
                                next_switch = time.time() + playtime
                                prefetch_failures = 0
                                excluded_for_prefetch.clear()

                elif fade_phase == 'in':  # static → next station
                    dry = read_frames(looped_segment(static_pcm, static_pos, required),
                                    required)
                    try:
                        wet = read_frames(next_stream, required)
                    except Exception as e:
                        print(f"Error during fade-in: {e}")
                        # If the new stream fails during fade-in, go back to static
                        wet = np.zeros(required*CHANNELS, dtype=np.int16)
                        fade_phase, fade_pos = 'hold', 0
                        prefetch_failures += 1

                        # Try another station if we haven't failed too many times
                        if prefetch_failures < MAX_PREFETCH_FAILURES:
                            try:
                                # Get a random station, excluding those we've already tried
                                all_excluded = excluded_for_prefetch.copy()
                                if current_url:
                                    all_excluded.add(current_url)
                                if target_url:
                                    all_excluded.add(target_url)
                                target_url = get_random_station(
                                    [s for s in stations if s not in all_excluded],
                                    exclude=current_url
                                )
                                prefetch_job = executor.submit(open_stream, target_url)
                                prefetch_start_time = time.time()
                                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Fade-in failed, trying ({prefetch_failures}/{MAX_PREFETCH_FAILURES}) →", target_url)
                            except Exception as retry_error:
                                print(f"Retry after fade-in failure failed: {retry_error}")
                                if prefetch_failures >= MAX_PREFETCH_FAILURES:
                                    print("Too many consecutive prefetch failures, falling back to current station")
                                    # Reset fade to go back to current station
                                    fade_phase = None
                                    next_switch = time.time() + playtime
                                    prefetch_failures = 0
                                    excluded_for_prefetch.clear()
                                else:
                                    next_switch = time.time() + 10
                        else:
                            # Too many failures, go back to current station
                            print("Too many consecutive prefetch failures, falling back to current station")
                            fade_phase = None
                            next_switch = time.time() + playtime
                            prefetch_failures = 0
                            excluded_for_prefetch.clear()

                        # Use static for this frame
                        samples[:] = dry
                        continue

                    samples[:] = ((1 - ramp) * dry + ramp * wet).astype(np.int16)
                    fade_pos += required
                    static_pos = (static_pos + required) % (len(static_pcm)//CHANNELS)
                    if fade_pos >= fade*RATE:
                        current = next_stream
                        next_switch = time.time() + playtime
                        fade_phase = None
                        prefetch_failures = 0
                        excluded_for_prefetch.clear()

            # ───── launch prefetch just *before* we need it ────────────────────
            if (fade_phase is None and
                    prefetch_job is None and
                    time.time() >= next_switch - (fade + HOLD_SEC)):
                try:
                    target_url = get_random_station(stations, exclude=current_url)
                    excluded_for_prefetch = {current_url}
                    prefetch_job = executor.submit(open_stream, target_url)
                    prefetch_start_time = time.time()
                    prefetch_failures = 0
                    fade_phase, fade_pos = 'out', 0
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Prefetching →", target_url)
                except Exception as e:
                    print(f"Prefetch failed: {e}")
                    next_switch = time.time() + 10  # Brief delay before retry

            # ───── extend hold if stream not ready yet (avoids silence) ───────
            if fade_phase == 'hold' and prefetch_job and not prefetch_job.done():
                # Only extend for a reasonable amount of time, not indefinitely
                if prefetch_start_time and time.time() - prefetch_start_time < PREFETCH_TIMEOUT:
                    next_switch += required / RATE

        except Exception as e:
            print(f"Unexpected error in mixer: {e}")
            # Return silence for this frame if we hit an unexpected error
            samples = np.zeros(required*CHANNELS, dtype=np.int16)

        required = yield samples.tobytes()
