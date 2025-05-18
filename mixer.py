"""
Audio mixing functionality for Eigenradio
"""

import time
import random
import numpy as np
from datetime import datetime
from typing import List, Optional, Dict, Any
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import traceback

from config import RATE, CHANNELS, executor, HOLD_SEC, debug
from audio_core import read_frames, looped_segment
from streaming import open_stream
from station_manager import get_random_station, add_to_play_history, check_station_url, reset_station_cache, verify_previously_working_stations_async

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
                # Reset all station caches to ensure fresh checks
                reset_station_cache()

                current_url = get_random_station(stations, executor=executor)
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

    # Flag to indicate a successful prefetch is ready
    prefetch_success = False
    # Time to initiate scheduled switch (will be updated when prefetch succeeds)
    scheduled_switch_time = None
    # Successfully prefetched URL and stream
    prefetched_url = None
    prefetched_stream = None

    # Debug counter for prefetch checks
    prefetch_check_counter = 0

    # Time of last station reset (to avoid hammering same URLs)
    last_station_reset_time = time.time()
    # How often to reset station exclusion list (30 minutes)
    STATION_RESET_INTERVAL = 1800

    # Track first prefetch cycle to handle it specially
    is_first_prefetch_cycle = True
    # Count failures in the first prefetch cycle
    first_cycle_failures = 0

    while True:
        current_time = time.time()

        # Periodically reset excluded stations to prevent getting stuck
        if current_time - last_station_reset_time > STATION_RESET_INTERVAL:
            excluded_for_prefetch.clear()
            reset_station_cache()  # Reset station cache too
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Reset station exclusion list and cache")
            last_station_reset_time = current_time

        # If current stream is unhealthy, start crossfade to static
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
                    if fade_phase is None and prefetch_job is None and not prefetch_success:
                        try:
                            # Reset caches and exclusions for emergency
                            reset_station_cache()
                            excluded_for_prefetch.clear()

                            target_url = get_random_station(stations, exclude=current_url, executor=executor)
                            excluded_for_prefetch = {current_url}
                            prefetch_job = executor.submit(open_stream, target_url)
                            prefetch_start_time = time.time()
                            prefetch_failures = 0
                            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Emergency switch - prefetching →", target_url)
                            # Don't start fade yet - wait for successful prefetch
                        except Exception as e:
                            print(f"Emergency switch failed: {e}")

        # Check for stuck prefetch jobs
        if prefetch_job and prefetch_start_time and time.time() - prefetch_start_time > PREFETCH_TIMEOUT:
            print(f"Prefetch operation timed out after {PREFETCH_TIMEOUT} seconds, canceling and trying another station")
            prefetch_job.cancel()
            prefetch_job = None
            prefetch_start_time = None
            prefetch_failures += 1
            prefetch_success = False

            # If this is the first cycle, count separately
            if is_first_prefetch_cycle:
                first_cycle_failures += 1
                # If we've had multiple failures in the first cycle, do a complete reset
                if first_cycle_failures >= 3:
                    print("Multiple failures in first prefetch cycle, doing complete cache reset")
                    reset_station_cache()  # Reset all station caches
                    excluded_for_prefetch.clear()  # Clear exclusion list
                    is_first_prefetch_cycle = False  # No longer first cycle

            # Add the failed station to our exclusion list
            if target_url:
                excluded_for_prefetch.add(target_url)

            # If we're in hold phase and haven't failed too many times, try another station
            if fade_phase == 'hold' and prefetch_failures < MAX_PREFETCH_FAILURES:
                try:
                    # Reset any station caches to ensure fresh checks
                    reset_station_cache()

                    # Get a random station, excluding those we've already tried
                    all_excluded = excluded_for_prefetch.copy()
                    if current_url:
                        all_excluded.add(current_url)

                    # Get a fresh list of stations to try
                    available_stations = [s for s in stations if s not in all_excluded]
                    # If we've tried too many, reset and try some that failed before too
                    if len(available_stations) < 3 and len(all_excluded) > 5:
                        print(f"Exhausted stations, resetting exclusion list")
                        excluded_for_prefetch.clear()
                        all_excluded = {current_url} if current_url else set()
                        available_stations = [s for s in stations if s not in all_excluded]

                    # First check for known working stations
                    working_stations_future = verify_previously_working_stations_async(available_stations, executor)
                    working_stations = working_stations_future.result()  # TODO: Make non-blocking for optimal audio
                    if working_stations:
                        print(f"Found {len(working_stations)} verified working stations to try")
                        target_url = random.choice(working_stations)
                    else:
                        target_url = get_random_station(available_stations, exclude=current_url, executor=executor)

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
                first_cycle_failures = 0  # Reset this too
                excluded_for_prefetch.clear()

                # No longer first cycle
                is_first_prefetch_cycle = False

        # Check if prefetch job completed successfully
        if prefetch_job and prefetch_job.done():
            prefetch_check_counter += 1
            if prefetch_check_counter % 10 == 0:  # Reduce log spam
                debug(f"Checking prefetch job completion (attempt {prefetch_check_counter})")

            try:
                # Non-blocking check of the prefetch result (increased timeout)
                stream_result = prefetch_job.result(timeout=0.01)

                # Store the successful prefetch
                prefetched_stream = stream_result
                prefetched_url = target_url  # Save the URL that was prefetched
                prefetch_job = None
                prefetch_start_time = None
                prefetch_success = True
                prefetch_failures = 0  # Reset failures when we have a success
                prefetch_check_counter = 0  # Reset counter
                first_cycle_failures = 0  # Reset first cycle failures
                is_first_prefetch_cycle = False  # No longer first cycle

                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Prefetch successful for {prefetched_url}")

                # Set a time to start the crossfade - immediately for emergency switches,
                # or schedule it for later for regular switches
                if fade_phase is None:  # Not an emergency case
                    scheduled_switch_time = time.time()
                else:
                    # Emergency case - start fade-in immediately if we were in hold
                    if fade_phase == 'hold':
                        fade_phase, fade_pos = 'in', 0
                        next_stream = prefetched_stream
                        current_url = prefetched_url
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)

                        # Reset stream health
                        if current in stream_health:
                            del stream_health[current]
                        stream_health[next_stream] = {
                            'errors': 0,
                            'last_error': None,
                            'silence_count': 0
                        }

                        # Reset after we've used this prefetched stream
                        prefetch_success = False
                        prefetched_url = None
                        prefetched_stream = None
                    elif fade_phase is None:
                        # Start the fade sequence when appropriate
                        # (this will be managed by the scheduled_switch_time section)
                        pass

            except FutureTimeoutError:
                # This is normal - the job is still running
                pass
            except Exception as e:
                print(f"Prefetch failed: {str(e)}")
                traceback.print_exc()  # Print detailed error info
                prefetch_job = None
                prefetch_start_time = None
                prefetch_failures += 1
                prefetch_success = False
                prefetched_url = None
                prefetched_stream = None
                prefetch_check_counter = 0  # Reset counter

                # If this is the first cycle, count separately
                if is_first_prefetch_cycle:
                    first_cycle_failures += 1
                    # If we've had multiple failures in the first cycle, do a complete reset
                    if first_cycle_failures >= 3:
                        print("Multiple failures in first prefetch cycle, doing complete cache reset")
                        reset_station_cache()  # Reset all station caches
                        excluded_for_prefetch.clear()  # Clear exclusion list

                # Add the failed station to our exclusion list
                if target_url:
                    excluded_for_prefetch.add(target_url)

                # If we were in an emergency situation, try another station
                if fade_phase is not None and prefetch_failures < MAX_PREFETCH_FAILURES:
                    try:
                        # Reset caches to ensure fresh checks
                        reset_station_cache()

                        # Get a random station, excluding those we've already tried
                        all_excluded = excluded_for_prefetch.copy()
                        if current_url:
                            all_excluded.add(current_url)

                        # Get a fresh list of stations to try
                        available_stations = [s for s in stations if s not in all_excluded]
                        # If we've tried too many, reset and try some that failed before too
                        if len(available_stations) < 3 and len(all_excluded) > 5:
                            print(f"Exhausted stations, resetting exclusion list")
                            excluded_for_prefetch.clear()
                            all_excluded = {current_url} if current_url else set()
                            available_stations = [s for s in stations if s not in all_excluded]

                        # Check for known working stations first
                        working_stations_future = verify_previously_working_stations_async(available_stations, executor)
                        working_stations = working_stations_future.result()  # TODO: Make non-blocking for optimal audio
                        if working_stations:
                            print(f"Found {len(working_stations)} verified working stations to try")
                            target_url = random.choice(working_stations)
                        else:
                            target_url = get_random_station(available_stations, exclude=current_url, executor=executor)

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
                    if prefetch_job is None and not prefetch_success:
                        try:
                            # Reset caches for emergency
                            reset_station_cache()

                            # Exclude stations we've already tried and failed
                            available_stations = [s for s in stations if s not in excluded_for_prefetch]
                            if len(available_stations) < 3:  # Too few stations left
                                print("Limited stations available, resetting exclusion list")
                                excluded_for_prefetch.clear()
                                available_stations = stations

                            target_url = get_random_station(available_stations, exclude=current_url, executor=executor)
                            excluded_for_prefetch = {current_url}
                            prefetch_job = executor.submit(open_stream, target_url)
                            prefetch_start_time = time.time()
                            prefetch_failures = 0
                            first_cycle_failures = 0  # Reset this too
                            is_first_prefetch_cycle = False  # No longer first cycle
                            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Stream error - prefetching →", target_url)
                            # We'll start the fade-out only when prefetch succeeds
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
                        # If we already have a successful prefetch, we can go straight to fade-in
                        if prefetch_success:
                            fade_phase = 'in'
                            next_stream = prefetched_stream
                            current_url = prefetched_url
                            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Going straight to fade-in for {current_url}")
                            # Clear the prefetch info since we're using it
                            prefetch_success = False
                            prefetched_stream = None
                            prefetched_url = None

                elif fade_phase == 'hold':  # static full
                    samples[:] = read_frames(looped_segment(static_pcm, static_pos, required),
                                            required)
                    fade_pos += required
                    static_pos = (static_pos + required) % (len(static_pcm)//CHANNELS)

                    # If we have a successfully prefetched stream, start the fade-in
                    if prefetch_success:
                        fade_phase, fade_pos = 'in', 0
                        next_stream = prefetched_stream
                        current_url = prefetched_url
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)

                        # Reset stream health
                        if current in stream_health:
                            del stream_health[current]
                        stream_health[next_stream] = {
                            'errors': 0,
                            'last_error': None,
                            'silence_count': 0
                        }

                        # Clear the prefetch info since we're using it
                        prefetch_success = False
                        prefetched_stream = None
                        prefetched_url = None

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
                                # Reset caches to ensure fresh checks
                                reset_station_cache()

                                # Get a random station, excluding those we've already tried
                                all_excluded = excluded_for_prefetch.copy()
                                if current_url:
                                    all_excluded.add(current_url)
                                if target_url:
                                    all_excluded.add(target_url)

                                # Get a fresh list of stations to try
                                available_stations = [s for s in stations if s not in all_excluded]
                                # If we've tried too many, reset and try some that failed before too
                                if len(available_stations) < 3 and len(all_excluded) > 5:
                                    print(f"Exhausted stations, resetting exclusion list")
                                    excluded_for_prefetch.clear()
                                    all_excluded = {current_url} if current_url else set()
                                    available_stations = [s for s in stations if s not in all_excluded]

                                # Always prioritize diversity by using the full pool of available stations
                                # Identify working stations just for fallback if needed
                                working_stations_future = verify_previously_working_stations_async(available_stations, executor)
                                working_stations = working_stations_future.result()  # TODO: Make non-blocking for optimal audio

                                # Get a station from the full pool to maximize diversity
                                # The get_random_station function will already avoid known bad stations
                                target_url = get_random_station(available_stations, exclude=current_url, executor=executor)
                                print(f"Selected station from full station pool for maximum diversity: {target_url}")

                                # If no station was found and we have working stations, use one as fallback
                                if not target_url and working_stations:
                                    target_url = random.choice(working_stations)
                                    print(f"Falling back to verified working station: {target_url}")

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
                        next_stream = None
                        next_switch = time.time() + playtime
                        fade_phase = None
                        prefetch_failures = 0
                        first_cycle_failures = 0  # Reset this too
                        is_first_prefetch_cycle = False  # No longer first cycle
                        excluded_for_prefetch.clear()

            # ───── launch prefetch just *before* we need it ────────────────────
            if (fade_phase is None and
                    prefetch_job is None and
                    not prefetch_success and
                    time.time() >= next_switch - (fade + HOLD_SEC)):
                try:
                    # Reset excluded_for_prefetch if we've tried most stations
                    if len(excluded_for_prefetch) > len(stations) // 2:
                        excluded_for_prefetch.clear()
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Reset exclusion list to try more stations")

                    # Reset any station caches to ensure fresh checks
                    reset_station_cache()

                    # First check if there are known good stations
                    # Exclude stations we've excluded this cycle
                    available_stations = [s for s in stations if s not in excluded_for_prefetch and s != current_url]

                    # If no stations available after exclusions, try all except current
                    if not available_stations:
                        available_stations = [s for s in stations if s != current_url]
                        # If still nothing, use all stations
                        if not available_stations:
                            available_stations = stations

                    # Special handling for first prefetch cycle
                    if is_first_prefetch_cycle:
                        # Try using current URL in verify to see if any similar stations exist
                        if current_url:
                            reset_station_cache(current_url)  # Reset its cache to ensure fresh check
                            check_station_url(current_url, force_check=True)  # Force a check
                            add_to_play_history(current_url)  # Re-add to play history

                    # Always prioritize diversity by using the full pool of available stations
                    # Identify working stations just for fallback if needed
                    working_stations_future = verify_previously_working_stations_async(available_stations, executor)
                    working_stations = working_stations_future.result()  # TODO: Make non-blocking for optimal audio

                    # Get a station from the full pool to maximize diversity
                    # The get_random_station function will already avoid known bad stations
                    target_url = get_random_station(available_stations, exclude=current_url, executor=executor)
                    print(f"Selected station from full station pool for maximum diversity: {target_url}")

                    # If no station was found and we have working stations, use one as fallback
                    if not target_url and working_stations:
                        target_url = random.choice(working_stations)
                        print(f"Falling back to verified working station: {target_url}")

                    excluded_for_prefetch.add(current_url)

                    prefetch_job = executor.submit(open_stream, target_url)
                    prefetch_start_time = time.time()
                    prefetch_failures = 0
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Prefetching →", target_url)
                    # Don't start the fade yet - wait for successful prefetch
                except Exception as e:
                    print(f"Prefetch failed: {e}")
                    traceback.print_exc()  # Print the error for debugging
                    next_switch = time.time() + 10  # Brief delay before retry

                    # If this is the first cycle and we've failed, reset counters and caches
                    if is_first_prefetch_cycle:
                        first_cycle_failures += 1
                        # If multiple failures in first cycle, do more aggressive reset
                        if first_cycle_failures >= 2:
                            print("Multiple failures in first prefetch cycle, doing complete reset")
                            reset_station_cache()
                            excluded_for_prefetch.clear()

            # Start the crossfade if we have a successful prefetch and it's time to switch
            if (fade_phase is None and
                    prefetch_success and
                    scheduled_switch_time is not None and
                    time.time() >= scheduled_switch_time):
                fade_phase, fade_pos = 'out', 0
                scheduled_switch_time = None
                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Starting crossfade to {prefetched_url}")

        except Exception as e:
            print(f"Unexpected error in mixer: {e}")
            traceback.print_exc()  # Print detailed error info
            # Return silence for this frame if we hit an unexpected error
            samples = np.zeros(required*CHANNELS, dtype=np.int16)

        required = yield samples.tobytes()
