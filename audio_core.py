"""
Core audio functionality for Eigenradio
"""

import array
import queue
import numpy as np
import miniaudio as ma

from config import CHANNELS, BYTES_PER_SAMP, FRAME_SIZE, _leftover

# Frame queue for audio processing
FRAME_QUEUE = queue.Queue(maxsize=100)  # ≤ ~2½ s of audio

def looped_segment(pcm: array.array, start: int, length: int):
    """Yield *bytes* for `length` frames, wrapping around inside pcm."""
    total = len(pcm) // CHANNELS
    idx = start % total
    while length:
        take = min(length, total - idx)
        yield pcm[idx*CHANNELS:(idx+take)*CHANNELS].tobytes()
        idx = (idx + take) % total
        length -= take

def read_frames(gen, frames_needed: int) -> np.ndarray:
    """Read frames from a generator, handling leftovers and underruns"""
    want = frames_needed * CHANNELS * BYTES_PER_SAMP
    buf = bytearray()

    # 1) use what we already have ------------------------------
    if gen in _leftover and _leftover[gen]:
        carry = _leftover[gen]
        take = min(want, len(carry))
        buf += carry[:take]
        _leftover[gen] = carry[take:]

    # 2) pull only what's missing ------------------------------
    while len(buf) < want:
        try:
            chunk = next(gen)
        except StopIteration:
            break  # end of stream – pad with zeros
        need = want - len(buf)
        buf += chunk[:need]
        extra = chunk[need:]
        if extra:  # 3) remember surplus for next time
            _leftover[gen] = extra

    if len(buf) < want:  # (network hiccup)
        buf.extend(b'\x00' * (want - len(buf)))

    return np.frombuffer(buf, dtype=np.int16, count=frames_needed * CHANNELS)

def produce_pcm(gen):
    """Pull fixed-size blocks from the mixer and drop them in the queue."""
    while True:
        try:
            block = gen.send(FRAME_SIZE)  # ← was next(gen)
        except StopIteration:
            break
        FRAME_QUEUE.put(block)  # blocks only if queue is full

def radio_player():
    """
    Ultra-thin device callback:
    • returns exactly the number of frames the device asks for (`required`)
    • pulls 1 024-frame blocks from FRAME_QUEUE
    • keeps any surplus in `carry`
    • pads with silence if the queue is momentarily empty
    """
    carry = b''  # leftover bytes
    silence = b'\x00' * (CHANNELS * BYTES_PER_SAMP)

    required = yield b''  # handshake

    while True:
        need_bytes = required * CHANNELS * BYTES_PER_SAMP

        # ── top-up `carry` until we have enough for this period ──────────
        while len(carry) < need_bytes:
            try:
                carry += FRAME_QUEUE.get_nowait()  # one 1 024-frame block
            except queue.Empty:
                break  # queue ran dry

        # ── slice exactly what the driver wants ──────────────────────────
        chunk, carry = carry[:need_bytes], carry[need_bytes:]

        if len(chunk) < need_bytes:  # underrun → pad with 0s
            pad = need_bytes - len(chunk)
            chunk += silence * (pad // len(silence))

        required = yield chunk
