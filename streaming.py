"""
Streaming functionality for Eigenradio
"""

import re
import time
import urllib.parse as up
import requests
import miniaudio as ma

from config import CHANNELS, RATE, FRAME_SIZE

# Regular expression for playlist parsing
PLAYLIST_RE = re.compile(r'^\s*File\d+\s*=\s*(http.*)', re.I)

# Subclass of IceCastClient to work around buffering problems
class SmoothIceCast(ma.IceCastClient):
    """Enhanced IceCast client with smoother buffering"""
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

def open_stream(url: str):
    """Run in background thread: open and fully probe a stream generator."""
    return icecast_stream(url)
