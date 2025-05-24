"""
Configuration and constants for the Eigenradio application
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

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

class TimestampFormatter(logging.Formatter):
    """Custom formatter that preserves the timestamp format used throughout the application"""

    def format(self, record):
        # Use the same timestamp format as was used in print statements
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

        # For certain log messages, we want to include the timestamp
        if hasattr(record, 'include_timestamp') and record.include_timestamp:
            return f"{timestamp} {record.getMessage()}"
        else:
            return record.getMessage()

# Create logger
logger = logging.getLogger('eigenradio')
logger.setLevel(logging.DEBUG)

# Create console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(TimestampFormatter())

# Add handler to logger
logger.addHandler(console_handler)

def set_debug(value):
    """Set the debug flag and adjust logging level accordingly"""
    global DEBUG
    DEBUG = value

    if DEBUG:
        console_handler.setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
    else:
        console_handler.setLevel(logging.INFO)
        logger.setLevel(logging.INFO)

def log_info(message, include_timestamp=False):
    """Log info messages (always shown)"""
    logger.info(message, extra={'include_timestamp': include_timestamp})

def log_debug(message, include_timestamp=False):
    """Log debug messages (only shown when debug is enabled)"""
    logger.debug(message, extra={'include_timestamp': include_timestamp})

def log_error(message, include_timestamp=False):
    """Log error messages (always shown)"""
    logger.error(message, extra={'include_timestamp': include_timestamp})

def log_warning(message, include_timestamp=False):
    """Log warning messages (always shown)"""
    logger.warning(message, extra={'include_timestamp': include_timestamp})

# Legacy compatibility
def debug(message):
    """Legacy debug function for backward compatibility"""
    log_debug(message)

# Initialize logging level based on current DEBUG setting
set_debug(DEBUG)
