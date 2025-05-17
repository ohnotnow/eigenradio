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
import miniaudio as ma
import numpy as np

# ---------------------------------------------------------------------------
RATE            = 44_100     # Hz
CHANNELS        = 2
FRAME_SIZE      = 1_024      # sound-card callback size
FADE_SEC        = 8
HOLD_SEC        = 1
PLAY_SEC        = 20
BYTES_PER_SAMP  = 2
executor        = ThreadPoolExecutor(max_workers=2)   # for prefetching
_last_good      = {}         # generator → last non-empty bytes
# ---------------------------------------------------------------------------

# ─────────────────────────── helpers ────────────────────────────────────────
PLAYLIST_RE = re.compile(r'^\s*File\d+\s*=\s*(http.*)', re.I)

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
    client = ma.IceCastClient(url)            # no timeout kw in ≥1.58
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
    """Return exactly `frames_needed` frames; repeat last good chunk on underrun."""
    want_bytes = frames_needed * CHANNELS * BYTES_PER_SAMP
    buf = bytearray()

    while len(buf) < want_bytes:
        try:
            chunk = next(gen)
        except StopIteration:
            chunk = b''
        if not chunk:
            chunk = _last_good.get(gen, b'')
        if not chunk:
            break
        _last_good[gen] = chunk
        buf.extend(chunk)

    if len(buf) < want_bytes:
        buf.extend(b'\x00' * (want_bytes - len(buf)))

    return np.frombuffer(buf, dtype=np.int16, count=frames_needed * CHANNELS)

def open_stream(url: str):
    """Run in background thread: open and fully probe a stream generator."""
    return icecast_stream(url)

# ─────────────────────────── core mixer ─────────────────────────────────────
def radio_mixer(stations, static_pcm):
    required = yield b''                     # priming handshake

    current_url = random.choice(stations)
    current     = icecast_stream(current_url)
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)

    next_switch  = time.time() + PLAY_SEC
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
            t1   = fade_pos / (FADE_SEC * RATE)
            t2   = (fade_pos + required) / (FADE_SEC * RATE)
            ramp = np.repeat(np.linspace(t1, t2, required, dtype=np.float32),
                              CHANNELS)

            if fade_phase == 'out':          # station → static
                dry = read_frames(current, required)
                wet = read_frames(looped_segment(static_pcm, static_pos, required),
                                 required)
                samples[:] = ((1 - ramp) * dry + ramp * wet).astype(np.int16)
                fade_pos  += required
                static_pos = (static_pos + required) % (len(static_pcm)//CHANNELS)
                if fade_pos >= FADE_SEC*RATE:
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
                        next_switch  = time.time() + PLAY_SEC
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
                if fade_pos >= FADE_SEC*RATE:
                    current      = next_stream
                    next_switch  = time.time() + PLAY_SEC
                    fade_phase   = None

        # ───── launch prefetch just *before* we need it ────────────────────
        if (fade_phase is None and
                prefetch_job is None and
                time.time() >= next_switch - (FADE_SEC + HOLD_SEC)):
            target_url   = random.choice([s for s in stations if s != current_url])
            prefetch_job = executor.submit(open_stream, target_url)
            fade_phase, fade_pos = 'out', 0
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Prefetching →", target_url)

        # ───── extend hold if stream not ready yet (avoids silence) ───────
        if fade_phase == 'hold' and prefetch_job and not prefetch_job.done():
            next_switch += FRAME_SIZE / RATE

        required = yield samples.tobytes()

# ─────────────────────────── bootstrap ──────────────────────────────────────
def main(m3u_path: str, static_path: str):
    with open(m3u_path, encoding='utf-8') as f:
        stations = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    if not stations:
        raise SystemExit("No stations in playlist!")

    static = ma.decode_file(static_path,
                            output_format=ma.SampleFormat.SIGNED16,
                            nchannels=CHANNELS,
                            sample_rate=RATE).samples   # array('h')

    mixer = radio_mixer(stations, static)
    next(mixer)                                    # prime

    with ma.PlaybackDevice(output_format=ma.SampleFormat.SIGNED16,
                           sample_rate=RATE,
                           nchannels=CHANNELS) as dev:
        dev.start(mixer)
        print("▲  Playing…  Ctrl-C to quit")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

if __name__ == '__main__':
    import sys
    if len(sys.argv) != 3:
        print("Usage: python test.py <playlist.m3u> <static.mp3>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
