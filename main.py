import vlc
import time
import random
import os
import glob
import sys
import argparse
import xml.etree.ElementTree as ET

M3U_FILE = "yourplaylist.m3u"             # path to your m3u file
STATIC_DIR = "static_sounds"              # folder of .wav/.mp3 static sounds
PLAY_TIME = 600                           # seconds (10 minutes)
FADE_TIME = 3                             # seconds to crossfade

def parse_m3u(file_path):
    """Extract stream URLs from an m3u file."""
    stations = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                stations.append(line)
    return stations

def load_static_files(static_dir):
    """Load all .wav/.mp3 files from static dir."""
    sounds = glob.glob(os.path.join(static_dir, "*.wav")) + glob.glob(os.path.join(static_dir, "*.mp3"))
    return sounds

def parse_icecast(file_path):
    """Extract stream URLs from an icecast XML file."""
    tree = ET.parse(file_path)
    root = tree.getroot()
    stations = [
        elem.text.strip()
        for elem in root.iter('listen_url')
        if elem.text and elem.text.strip()
    ]
    return stations

def play_stream(url):
    """Start a VLC player for a stream URL."""
    player = vlc.MediaPlayer(url)
    player.audio_set_volume(100)
    player.play()
    time.sleep(1)  # let it buffer/play
    return player

def play_static(static_file):
    """Start a VLC player for a static sound."""
    player = vlc.MediaPlayer(static_file)
    player.audio_set_volume(0)  # start silent for crossfade
    player.play()
    time.sleep(0.2)
    return player

def crossfade(old_player, new_url, static_files, fade_time=3):
    """Crossfade from old stream to new stream with static noise in between.

    Phase 1: fade out old stream while fading in static noise over fade_time seconds.
    Phase 2: hold static noise at full volume for fade_time seconds.
    Phase 3: fade out static noise while fading in the new stream over fade_time seconds.
    """
    static_file = random.choice(static_files)
    static_player = play_static(static_file)
    new_player = play_stream(new_url)
    new_player.audio_set_volume(0)
    steps = 20

    for i in range(steps + 1):
        frac = i / steps
        old_player.audio_set_volume(int(100 * (1 - frac)))
        static_player.audio_set_volume(int(100 * frac))
        time.sleep(fade_time / steps)

        if static_player.get_state() == vlc.State.Ended:
            static_player.play()

    old_player.stop()


    plateau_end = time.time() + fade_time
    while time.time() < plateau_end:
        if static_player.get_state() == vlc.State.Ended:
            static_player.play()
        time.sleep(0.1)

    for i in range(steps + 1):
        frac = i / steps
        static_player.audio_set_volume(int(100 * (1 - frac)))
        new_player.audio_set_volume(int(100 * frac))
        time.sleep(fade_time / steps)

        if static_player.get_state() == vlc.State.Ended:
            static_player.play()

    static_player.stop()
    new_player.audio_set_volume(100)
    return new_player

def main(args):
    if args.m3u_file:
        stations = parse_m3u(args.m3u_file)
    else:
        stations = parse_icecast(args.icecast_file)
    static_files = load_static_files(args.static_dir)
    if not stations:
        if args.m3u_file:
            print("No stations found in M3U file!")
        else:
            print("No stations found in Icecast file!")
        sys.exit(1)
    if not static_files:
        print("No static files found in static_sounds/!")
        sys.exit(1)

    print(f"Loaded {len(stations)} stations and {len(static_files)} static sounds.")

    current_url = random.choice(stations)
    player = play_stream(current_url)
    print(f"Now playing: {current_url}")

    while True:
        time.sleep(args.playtime)
        next_url = random.choice(stations)
        print(f"Crossfading to: {next_url}")
        player = crossfade(player, next_url, static_files, fade_time=args.fade)
        print(f"Now playing: {next_url}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stream radio player")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--m3u-file", type=str, help="Path to the M3U file")
    source_group.add_argument("--icecast-file", type=str, help="Path to the icecast file")
    parser.add_argument("--static-dir", type=str, default="static_sounds", help="Path to the static sounds directory")
    parser.add_argument("--playtime", type=int, default=600, help="Play time in seconds")
    parser.add_argument("--fade", type=int, default=3, help="Fade time in seconds")
    args = parser.parse_args()
    main(args)
