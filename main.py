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

# Import modules
from config import set_debug
from station_manager import parse_m3u, parse_icecast
from audio_core import produce_pcm, radio_player, FRAME_SIZE
from mixer import radio_mixer

# Global flag for clean shutdown
running = True
# Force exit flag
force_exit = False
# Counter for SIGINT signals
sigint_count = 0

def signal_handler(sig, frame):
    """Handle interrupt signals for clean shutdown"""
    global running, force_exit, sigint_count

    sigint_count += 1

    if sigint_count == 1:
        print("\nShutting down gracefully... (Ctrl-C again to force quit)")
        running = False
    elif sigint_count >= 2:
        print("\nForce quitting...")
        force_exit = True
        # Force exit after a short delay to allow message to be printed
        os._exit(0)

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

    # Create mixer and start producer thread
    mixer = radio_mixer(stations, static_pcm, args.playtime, args.fade)
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
        # Ensure resources are cleaned up
        try:
            dev.close()
        except:
            pass
        print("Goodbye!")
        # Make sure we exit
        sys.exit(0)


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
