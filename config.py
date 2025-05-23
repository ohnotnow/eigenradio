"""
Configuration and constants for the Eigenradio application
"""

import logging
from concurrent.futures import ThreadPoolExecutor

# Audio settings
RATE = 44_100  # Hz
CHANNELS = 2
FRAME_SIZE = 1_024  # sound-card callback size
BYTES_PER_SAMP = 2

# Crossfade settings
FADE_SEC = 8
HOLD_SEC = 1
PLAY_SEC = 20

# Debug settings
DEBUG = False

# Thread management - use daemon threads for easier cleanup
executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prefetch")

# Global state
_last_good = {}  # generator → last non-empty bytes
_leftover = {}

# Initialize logging
logging.basicConfig(level=logging.INFO)

def set_debug(value):
    """Set the debug flag"""
    global DEBUG
    DEBUG = value

def debug(message):
    """Log debug messages if debug is enabled"""
    if DEBUG:
        logging.info(message)
