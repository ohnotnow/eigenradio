#!/usr/bin/env python
"""
IceCast playlist player with static-noise cross-fade
===================================================

*   Resolves .pls/.m3u links to real stream URLs.
*   Prefetches the next station in a background thread so the
    real-time audio callback never blocks (no "dead air").
*   Uses NumPy for the 2-line mixing math; remove it if you prefer.
"""

import os
import sys
import threading
import argparse
import miniaudio as ma
import time
import signal
import random

# Import modules
from config import set_debug, executor
from station_manager import parse_m3u, parse_icecast, get_random_station
from audio_core import produce_pcm, radio_player, FRAME_SIZE
from mixer import radio_mixer
from streaming import open_stream, StreamConnectionError, StreamTimeoutError

# Global flag for clean shutdown
running = True
# Force exit flag
force_exit = False
# Counter for SIGINT signals
sigint_count = 0

# Maximum initial attempts
MAX_INITIAL_ATTEMPTS = 10

def signal_handler(sig, frame):
    """Handle interrupt signals for clean shutdown"""
    global running, force_exit, sigint_count

    sigint_count += 1

    if sigint_count == 1:
        print("\nShutting down gracefully... (Ctrl-C again to force quit)")
        running = False

        # Set a timer to force exit if graceful shutdown takes too long
        def force_exit_timer():
            time.sleep(3.0)  # Give 3 seconds for graceful shutdown
            if not force_exit:
                print("\nGraceful shutdown taking too long, forcing exit...")
                os._exit(1)

        timer_thread = threading.Thread(target=force_exit_timer, daemon=True)
        timer_thread.start()

    elif sigint_count >= 2:
        print("\nForce quitting...")
        force_exit = True
        # Force exit immediately
        os._exit(1)

def find_working_initial_station(stations, max_attempts=MAX_INITIAL_ATTEMPTS):
    """Find a working initial station or exit if none available"""
    print(f"Finding initial station from {len(stations)} stations...")

    tried_stations = set()
    attempts = 0

    while attempts < max_attempts:
        try:
            # Try to get a random station that we haven't tried yet
            available_stations = [s for s in stations if s not in tried_stations]
            if not available_stations:
                print(f"Tried all available stations ({len(tried_stations)}), none working!")
                return None

            url = random.choice(available_stations)
            tried_stations.add(url)

            print(f"Trying initial station {attempts+1}/{max_attempts}: {url}")

            # Use a shorter timeout for initial station checks to prevent hanging
            import signal
            import threading

            # Set up a timeout for the stream opening
            result = [None]
            error = [None]

            def open_with_timeout():
                try:
                    stream = open_stream(url)
                    # Try to get one frame to verify it's working
                    next(stream)
                    result[0] = (url, stream)
                except Exception as e:
                    error[0] = e

            # Run with timeout
            thread = threading.Thread(target=open_with_timeout)
            thread.daemon = True
            thread.start()
            thread.join(timeout=8)  # 8 second timeout for initial station check

            if thread.is_alive():
                print(f"Station connection timed out after 8 seconds: {url}")
                # Thread is still running, but we'll continue to next station
                attempts += 1
                time.sleep(0.5)
                continue

            if result[0]:
                url, stream = result[0]
                print(f"Found working initial station: {url}")
                return url, stream
            elif error[0]:
                raise error[0]

        except (StreamConnectionError, StreamTimeoutError) as e:
            print(f"Station connection failed: {e}")
        except Exception as e:
            print(f"Unexpected error with station: {e}")

        attempts += 1
        time.sleep(0.5)  # Brief pause before next attempt

    print(f"Failed to find a working station after {max_attempts} attempts.")
    return None

def main(args):
    """Main function to start the radio player"""
    global running, force_exit

    # Register signal handler for clean shutdown
    signal.signal(signal.SIGINT, signal_handler)

    # Load stations
    if args.m3u_file:
        stations = parse_m3u(args.m3u_file)
    else:
        stations = parse_icecast(args.icecast_file)

    # Check static file
    static_file = args.static_file
    if not os.path.isfile(static_file):
        print(f"No static file found at {static_file}!")
        sys.exit(1)

    # Verify stations were loaded
    if not stations:
        if args.m3u_file:
            print("No stations found in M3U file!")
        else:
            print("No stations found in Icecast file!")
        sys.exit(1)

    print(f"Loaded {len(stations)} stations and static sound: {static_file}")

    # Decode static file
    static_pcm = ma.decode_file(static_file,
                               output_format=ma.SampleFormat.SIGNED16,
                               nchannels=2,
                               sample_rate=44100).samples  # array('h')

    # Find an initial working station
    result = find_working_initial_station(stations)
    if not result:
        print("Could not find a working initial station. Exiting.")
        sys.exit(1)

    initial_url, initial_stream = result

    # Create mixer with the already opened stream
    mixer = radio_mixer(stations, static_pcm, args.playtime, args.fade,
                      initial_url=initial_url, initial_stream=initial_stream)
    next(mixer)  # prime coroutine

    producer_thread = threading.Thread(target=produce_pcm,
                     args=(mixer,), daemon=True)
    producer_thread.start()

    # Create player and start playback
    player = radio_player()
    next(player)  # prime coroutine

    # Setup playback device
    dev = ma.PlaybackDevice(sample_rate=44100,
                           nchannels=2,
                           output_format=ma.SampleFormat.SIGNED16,
                           buffersize_msec=120)
    try:
        dev.start(player)
        print("▲  Playing…  Ctrl-C to quit")

        # Main loop with clean shutdown
        while running and not force_exit:
            time.sleep(0.1)

        # Graceful shutdown
        print("Shutting down...")
        time.sleep(0.5)  # Allow final audio to play

    except KeyboardInterrupt:
        # Handle any KeyboardInterrupt that wasn't caught by the signal handler
        print("\nForce quitting...")

    finally:
        # Ensure resources are cleaned up quickly
        print("Shutting down...")

        # Close audio device first
        try:
            dev.close()
        except:
            pass

        # Quick executor shutdown without waiting
        try:
            executor.shutdown(wait=False)
        except:
            pass

        print("Goodbye!")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Stream radio player")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--m3u-file", type=str, help="Path to the M3U file")
    source_group.add_argument("--icecast-file", type=str, help="Path to the icecast file")
    parser.add_argument("--static-file", type=str, default="static.mp3", help="Path to the concatenated static file")
    parser.add_argument("--playtime", type=int, default=600, help="Play time in seconds")
    parser.add_argument("--fade", type=int, default=3, help="Fade time in seconds")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        set_debug(True)

    main(args)
