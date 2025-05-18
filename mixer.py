"""
Audio mixing functionality for Eigenradio
"""

import time
import random
import numpy as np
from datetime import datetime
from typing import List, Optional
from concurrent.futures import Future

from config import RATE, CHANNELS, executor, HOLD_SEC
from audio_core import read_frames, looped_segment
from streaming import open_stream
from station_manager import get_random_station

def radio_mixer(stations: List[str], static_pcm, playtime=600, fade=3):
    """
    Core mixer generator that handles crossfading between radio stations

    Args:
        stations: List of station URLs
        static_pcm: Static noise PCM data
        playtime: Duration to play each station in seconds
        fade: Fade duration in seconds
    """
    required = yield b''  # priming handshake

    current_url = get_random_station(stations)
    current = open_stream(current_url)
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)

    next_switch = time.time() + playtime
    fade_phase = None  # None/out/hold/in
    fade_pos = 0
    static_pos = random.randrange(len(static_pcm)//CHANNELS)

    prefetch_job: Optional[Future] = None
    next_stream = None
    target_url = None

    while True:
        samples = np.zeros(required*CHANNELS, dtype=np.int16)

        # ───── main mixing paths ───────────────────────────────────────────
        if fade_phase is None:  # steady-state station
            samples[:] = read_frames(current, required)

        else:  # some phase of the X-fade
            t1 = fade_pos / (fade * RATE)
            t2 = (fade_pos + required) / (fade * RATE)
            ramp = np.repeat(np.linspace(t1, t2, required, dtype=np.float32),
                             CHANNELS)

            if fade_phase == 'out':  # station → static
                dry = read_frames(current, required)
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
                        next_stream = prefetch_job.result()
                        prefetch_job = None
                        current_url = target_url
                        fade_phase, fade_pos = 'in', 0
                        next_switch = time.time() + playtime
                        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Now playing →", current_url)
                    except Exception as e:
                        print("Stream failed:", e)
                        prefetch_job = None
                        next_switch = time.time() + 10  # retry later

            elif fade_phase == 'in':  # static → next station
                dry = read_frames(looped_segment(static_pcm, static_pos, required),
                                required)
                wet = read_frames(next_stream, required)
                samples[:] = ((1 - ramp) * dry + ramp * wet).astype(np.int16)
                fade_pos += required
                static_pos = (static_pos + required) % (len(static_pcm)//CHANNELS)
                if fade_pos >= fade*RATE:
                    current = next_stream
                    next_switch = time.time() + playtime
                    fade_phase = None

        # ───── launch prefetch just *before* we need it ────────────────────
        if (fade_phase is None and
                prefetch_job is None and
                time.time() >= next_switch - (fade + HOLD_SEC)):
            target_url = random.choice([s for s in stations if s != current_url])
            prefetch_job = executor.submit(open_stream, target_url)
            fade_phase, fade_pos = 'out', 0
            print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} Prefetching →", target_url)

        # ───── extend hold if stream not ready yet (avoids silence) ───────
        if fade_phase == 'hold' and prefetch_job and not prefetch_job.done():
            next_switch += required / RATE

        required = yield samples.tobytes()
