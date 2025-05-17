import vlc
import time
import random
import os
import sys
import argparse
import xml.etree.ElementTree as ET
import requests

M3U_FILE = "yourplaylist.m3u"
STATIC_FILE = "static.mp3"  # Your concatenated static file
PLAY_TIME = 600
FADE_TIME = 3
checked_stations = {}

def parse_m3u(file_path):
    stations = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                stations.append(line)
    return stations

def parse_icecast(file_path):
    tree = ET.parse(file_path)
    root = tree.getroot()
    stations = [
        elem.text.strip()
        for elem in root.iter('listen_url')
        if elem.text and elem.text.strip()
    ]
    return stations

def check_station_url(url, timeout=5):
    if url in checked_stations:
        return checked_stations[url]
    try:
        response = requests.head(url, allow_redirects=True, timeout=timeout)
        if response.status_code == 200:
            checked_stations[url] = True
            return True
        # fallback to GET if HEAD fails
        response = requests.get(url, stream=True, timeout=timeout)
        if response.status_code == 200:
            next(response.iter_content(1024), None)
            checked_stations[url] = True
            return True
    except Exception:
        pass
    checked_stations[url] = False
    return False

def get_random_station(stations, max_attempts=10):
    tried = set()
    attempts = 0
    while attempts < max_attempts and len(tried) < len(stations):
        url = random.choice(stations)
        if url in tried:
            continue
        tried.add(url)
        if check_station_url(url):
            return url
        attempts += 1
    raise Exception("No working stations found after multiple attempts.")

def play_stream(url):
    player = vlc.MediaPlayer(url)
    player.audio_set_volume(100)
    player.play()
    time.sleep(1)  # let it buffer/play
    return player

def play_static(static_file, start_ms=None):
    player = vlc.MediaPlayer(static_file)
    player.audio_set_volume(0)
    player.play()
    time.sleep(0.2)
    if start_ms is not None:
        player.set_time(start_ms)
    return player

def get_static_length(static_file):
    # Returns length in ms
    instance = vlc.Instance()
    media = instance.media_new(static_file)
    media.parse_with_options(vlc.MediaParseFlag.local, 3000)
    length = media.get_duration()
    if length <= 0:
        # fallback: use a temporary player
        player = vlc.MediaPlayer(static_file)
        player.play()
        time.sleep(0.5)
        length = player.get_length()
        player.stop()
    return length

def crossfade(old_player, new_url, static_file, static_last_pos=0, fade_time=3, use_random_static=True):
    # Play static from last position or a random offset if first time
    static_length = get_static_length(static_file)
    if use_random_static:
        # Use a random start in the static, but don't start too close to the end
        static_start = random.randint(0, max(0, static_length - (fade_time * 3 * 1000)))
    else:
        static_start = static_last_pos
    static_player = play_static(static_file, start_ms=static_start)

    # New stream
    new_player = play_stream(new_url)
    new_player.audio_set_volume(0)
    steps = 20

    # PHASE 1: Fade out old, fade in static
    for i in range(steps + 1):
        frac = i / steps
        old_player.audio_set_volume(int(100 * (1 - frac)))
        static_player.audio_set_volume(int(100 * frac))
        time.sleep(fade_time / steps)
    old_player.stop()

    # PHASE 2: Hold static at full volume
    time.sleep(fade_time)

    # PHASE 3: Fade out static, fade in new stream
    for i in range(steps + 1):
        frac = i / steps
        static_player.audio_set_volume(int(100 * (1 - frac)))
        new_player.audio_set_volume(int(100 * frac))
        time.sleep(fade_time / steps)

    # Get where we left off in the static file
    last_static_pos = static_player.get_time()
    static_player.stop()
    new_player.audio_set_volume(100)
    return new_player, last_static_pos

def main(args):

    if args.m3u_file:
        stations = parse_m3u(args.m3u_file)
    else:
        stations = parse_icecast(args.icecast_file)

    static_file = args.static_file
    if not os.path.isfile(static_file):
        print(f"No static file found at {static_file}!")
        sys.exit(1)

    if not stations:
        if args.m3u_file:
            print("No stations found in M3U file!")
        else:
            print("No stations found in Icecast file!")
        sys.exit(1)

    print(f"Loaded {len(stations)} stations and static sound: {static_file}")

    current_url = get_random_station(stations)
    player = play_stream(current_url)
    print(f"Now playing: {current_url}")

    static_last_pos = 0  # Keep track of where static was left off

    while True:
        time.sleep(args.playtime)
        next_url = get_random_station(stations)
        print(f"Crossfading to: {next_url}")
        player, static_last_pos = crossfade(
            player, next_url, static_file, static_last_pos=static_last_pos,
            fade_time=args.fade, use_random_static=True
        )
        print(f"Now playing: {next_url}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stream radio player")
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--m3u-file", type=str, help="Path to the M3U file")
    source_group.add_argument("--icecast-file", type=str, help="Path to the icecast file")
    parser.add_argument("--static-file", type=str, default="static.mp3", help="Path to the concatenated static file")
    parser.add_argument("--playtime", type=int, default=600, help="Play time in seconds")
    parser.add_argument("--fade", type=int, default=3, help="Fade time in seconds")
    args = parser.parse_args()
    main(args)
