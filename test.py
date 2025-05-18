#!/usr/bin/env python
"""
IceCast playlist player with static-noise cross-fade
===================================================

*   Resolves .pls/.m3u links to real stream URLs.
*   Prefetches the next station in a background thread so the
    real-time audio callback never blocks (no “dead air”).
*   Uses NumPy for the 2-line mixing math; remove it if you prefer.
"""

import time, random, array, re, urllib.parse as up, requests
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Optional
from datetime import datetime
import os, sys
import miniaudio as ma
import numpy as np
import threading, queue
import argparse
import xml.etree.ElementTree as ET
import requests
import logging
logging.basicConfig(level=logging.INFO)
checked_stations = {}

# ---------------------------------------------------------------------------
DEBUG = False
RATE            = 44_100     # Hz
CHANNELS        = 2
FRAME_SIZE      = 1_024      # sound-card callback size
FADE_SEC        = 8
HOLD_SEC        = 1
PLAY_SEC        = 20
BYTES_PER_SAMP  = 2
executor        = ThreadPoolExecutor(max_workers=2)   # for prefetching
_last_good      = {}         # generator → last non-empty bytes
_leftover = {}
# ---------------------------------------------------------------------------

# Subclass of IceCastClient to work around buffering problems
class SmoothIceCast(ma.IceCastClient):
    BUFFER_SIZE = 1024 * 1024          # 3 s @ 44.1 kHz/16-bit/stereo
    BLOCK_SIZE  = 64 * 1024

    def read(self, num_bytes: int) -> bytes:          # non-blocking, 5 ms poll
        while True:
            with self._buffer_lock:
                if len(self._buffer) >= num_bytes or self._stop_stream:
                    chunk = self._buffer[:num_bytes]
                    self._buffer = self._buffer[num_bytes:]
                    return chunk
            time.sleep(0.005)

# ─────────────────────────── helpers ────────────────────────────────────────
PLAYLIST_RE = re.compile(r'^\s*File\d+\s*=\s*(http.*)', re.I)
FRAME_QUEUE = queue.Queue(maxsize=100)          # ≤ ~2½ s of audio

def _debug(message):
    if DEBUG:
        logging.info(message)

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
    _debug(f"Getting random station from {len(stations)} stations")
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

# ─────────── producer (runs in its own thread) ───────────
def produce_pcm(gen):
    """Pull fixed-size blocks from the mixer and drop them in the queue."""
    while True:
        try:
            block = gen.send(FRAME_SIZE)      # ←   was  next(gen)
        except StopIteration:
            break
        FRAME_QUEUE.put(block)                # blocks only if queue is full

# ─────────── super-thin callback ───────────
def radio_player():
    """
    Ultra-thin device callback:
    • returns exactly the number of frames the device asks for (`required`)
    • pulls 1 024-frame blocks from FRAME_QUEUE
    • keeps any surplus in `carry`
    • pads with silence if the queue is momentarily empty
    """
    carry = b''                                     # leftover bytes
    silence = b'\x00' * (CHANNELS * BYTES_PER_SAMP)

    required = yield b''                            # handshake

    while True:
        need_bytes = required * CHANNELS * BYTES_PER_SAMP

        # ── top-up `carry` until we have enough for this period ──────────
        while len(carry) < need_bytes:
            try:
                carry += FRAME_QUEUE.get_nowait()   # one 1 024-frame block
            except queue.Empty:
                break                               # queue ran dry

        # ── slice exactly what the driver wants ──────────────────────────
        chunk, carry = carry[:need_bytes], carry[need_bytes:]

        if len(chunk) < need_bytes:                 # underrun → pad with 0s
            pad = need_bytes - len(chunk)
            chunk += silence * (pad // len(silence))

        required = yield chunk

def resolve_playlist(url: str, timeout=5) -> str:
    """Turn a .pls/.m3u URL into the first real stream URL it contains."""
    path = up.urlparse(url).path
    if path and '.' not in path.split('/')[-1]:
        return url                            # already looks like /mount

    r = requests.get(url, timeout=timeout)
    body = r.text.splitlines()
    ct   = r.headers.get('Content-Type', '').lower()

    if url.lower().endswith('.pls') or 'scpls' in ct:
        for line in body:
            m = PLAYLIST_RE.match(line)
            if m:
                return m.group(1).strip()

    if (url.lower().endswith(('.m3u', '.m3u8')) or
            'mpegurl' in ct or 'vnd.apple.mpegurl' in ct):
        for line in body:
            line = line.strip()
            if line and not line.startswith('#'):
                return line

    raise RuntimeError(f"No stream URL found inside {url!r}")

def icecast_stream(url: str, frames=FRAME_SIZE):
    """Return a miniaudio generator that yields PCM frames."""
    url = resolve_playlist(url)
    client = SmoothIceCast(url)            # no timeout kw in ≥1.58
    return ma.stream_any(client,
                         nchannels=CHANNELS,
                         sample_rate=RATE,
                         output_format=ma.SampleFormat.SIGNED16,
                         frames_to_read=frames)

def looped_segment(pcm: array.array, start: int, length: int):
    """Yield *bytes* for `length` frames, wrapping around inside pcm."""
    total = len(pcm) // CHANNELS
    idx   = start % total
    while length:
        take = min(length, total - idx)
        yield pcm[idx*CHANNELS:(idx+take)*CHANNELS].tobytes()
        idx = (idx + take) % total
        length -= take

def read_frames(gen, frames_needed: int) -> np.ndarray:
    want = frames_needed * CHANNELS * BYTES_PER_SAMP
    buf  = bytearray()

    # 1) use what we already have ------------------------------
    if gen in _leftover and _leftover[gen]:
        carry = _leftover[gen]
        take  = min(want, len(carry))
        buf += carry[:take]
        _leftover[gen] = carry[take:]

    # 2) pull only what’s missing ------------------------------
    while len(buf) < want:
        try:
            chunk = next(gen)
        except StopIteration:
            break                      # end of stream – pad with zeros
        need  = want - len(buf)
        buf  += chunk[:need]
        extra = chunk[need:]
        if extra:                      # 3) remember surplus for next time
            _leftover[gen] = extra

    if len(buf) < want:                # (network hiccup)
        buf.extend(b'\x00' * (want - len(buf)))

    return np.frombuffer(buf, dtype=np.int16, count=frames_needed * CHANNELS)

def open_stream(url: str):
    """Run in background thread: open and fully probe a stream generator."""
    return icecast_stream(url)

# ─────────────────────────── core mixer ─────────────────────────────────────
def radio_mixer(stations, static_pcm, playtime = 600, fade = 3):
    required = yield b''                     # priming handshake

    current_url = get_random_station(stations)
    current     = icecast_stream(current_url)
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)

    next_switch  = time.time() + playtime
    fade_phase   = None                      # None/out/hold/in
    fade_pos     = 0
    static_pos   = random.randrange(len(static_pcm)//CHANNELS)

    prefetch_job: Optional[Future] = None
    next_stream  = None
    target_url   = None

    while True:
        samples = np.zeros(required*CHANNELS, dtype=np.int16)

        # ───── main mixing paths ───────────────────────────────────────────
        if fade_phase is None:               # steady-state station
            samples[:] = read_frames(current, required)

        else:                                # some phase of the X-fade
            t1   = fade_pos / (fade * RATE)
            t2   = (fade_pos + required) / (fade * RATE)
            ramp = np.repeat(np.linspace(t1, t2, required, dtype=np.float32),
                              CHANNELS)

            if fade_phase == 'out':          # station → static
                dry = read_frames(current, required)
                wet = read_frames(looped_segment(static_pcm, static_pos, required),
                                 required)
                samples[:] = ((1 - ramp) * dry + ramp * wet).astype(np.int16)
                fade_pos  += required
                static_pos = (static_pos + required) % (len(static_pcm)//CHANNELS)
                if fade_pos >= fade*RATE:
                    fade_phase, fade_pos = 'hold', 0

            elif fade_phase == 'hold':       # static full
                samples[:] = read_frames(looped_segment(static_pcm, static_pos, required),
                                         required)
                fade_pos  += required
                static_pos = (static_pos + required) % (len(static_pcm)//CHANNELS)

                # ready? start fade-in
                if prefetch_job and prefetch_job.done():
                    try:
                        next_stream = prefetch_job.result()
                        prefetch_job = None
                        current_url  = target_url
                        fade_phase, fade_pos = 'in', 0
                        next_switch  = time.time() + playtime
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)
                    except Exception as e:
                        print("Stream failed:", e)
                        prefetch_job = None
                        next_switch  = time.time() + 10  # retry later

            elif fade_phase == 'in':         # static → next station
                dry = read_frames(looped_segment(static_pcm, static_pos, required),
                                 required)
                wet = read_frames(next_stream, required)
                samples[:] = ((1 - ramp) * dry + ramp * wet).astype(np.int16)
                fade_pos  += required
                static_pos = (static_pos + required) % (len(static_pcm)//CHANNELS)
                if fade_pos >= fade*RATE:
                    current      = next_stream
                    next_switch  = time.time() + playtime
                    fade_phase   = None

        # ───── launch prefetch just *before* we need it ────────────────────
        if (fade_phase is None and
                prefetch_job is None and
                time.time() >= next_switch - (fade + HOLD_SEC)):
            target_url   = random.choice([s for s in stations if s != current_url])
            prefetch_job = executor.submit(open_stream, target_url)
            fade_phase, fade_pos = 'out', 0
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Prefetching →", target_url)

        # ───── extend hold if stream not ready yet (avoids silence) ───────
        if fade_phase == 'hold' and prefetch_job and not prefetch_job.done():
            next_switch += FRAME_SIZE / RATE

        required = yield samples.tobytes()

# ─────────────────────────── bootstrap ──────────────────────────────────────
# ─────────────────────────── bootstrap ──────────────────────────────────────
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

    static_pcm = ma.decode_file(static_file,
                                output_format=ma.SampleFormat.SIGNED16,
                                nchannels=CHANNELS,
                                sample_rate=RATE).samples   # array('h')

    # 3) spin up the mixing coroutine and the producer thread
    mixer = radio_mixer(stations, static_pcm, args.playtime, args.fade)
    next(mixer)  # prime coroutine

    threading.Thread(target=produce_pcm,
                     args=(mixer,), daemon=True).start()

    # 4) create the ultrathin callback and start playback
    player = radio_player()
    next(player)  # prime coroutine

    with ma.PlaybackDevice(sample_rate=RATE,
                           nchannels=CHANNELS,
                           output_format=ma.SampleFormat.SIGNED16,
                           buffersize_msec=120) as dev:
        dev.start(player)
        print("▲  Playing…  Ctrl-C to quit")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


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
        DEBUG = True
    main(args)
