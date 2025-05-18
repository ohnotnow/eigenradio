"""
Streaming functionality for Eigenradio
"""

import re
import time
import urllib.parse as up
import requests
from requests.exceptions import RequestException, Timeout, HTTPError
import socket
import miniaudio as ma
from typing import Optional, Union, Dict
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError

from config import CHANNELS, RATE, FRAME_SIZE, debug

# Regular expression for playlist parsing
PLAYLIST_RE = re.compile(r'^\s*File\d+\s*=\s*(http.*)', re.I)

# Stream validation constants
STREAM_VALIDATION_TIMEOUT = 10  # seconds
STREAM_VALIDATION_SIZE = 8192  # bytes to read for validation
STREAM_CONNECT_TIMEOUT = 5  # seconds for connection establishment
MAX_RETRIES = 2  # Number of retries for connections

# Cache DNS lookups to avoid repeated lookups for the same hostname
dns_cache: Dict[str, Union[str, None]] = {}

class StreamConnectionError(Exception):
    """Exception raised for stream connection failures"""
    pass

class StreamFormatError(Exception):
    """Exception raised when stream format is invalid"""
    pass

class StreamTimeoutError(Exception):
    """Exception raised when stream operations timeout"""
    pass

def get_cached_dns(hostname: str) -> Optional[str]:
    """Get IP address from cache or perform lookup"""
    if hostname in dns_cache:
        return dns_cache[hostname]

    try:
        ip = socket.gethostbyname(hostname)
        dns_cache[hostname] = ip
        return ip
    except socket.gaierror:
        dns_cache[hostname] = None
        return None

# Subclass of IceCastClient to work around buffering problems
class SmoothIceCast(ma.IceCastClient):
    """Enhanced IceCast client with smoother buffering"""
    BUFFER_SIZE = 1024 * 1024          # 3 s @ 44.1 kHz/16-bit/stereo
    BLOCK_SIZE  = 64 * 1024

    def read(self, num_bytes: int) -> bytes:          # non-blocking, 5 ms poll
        timeout_start = time.time()
        timeout = 10  # 10 seconds to get some data

        while True:
            with self._buffer_lock:
                if len(self._buffer) >= num_bytes or self._stop_stream:
                    chunk = self._buffer[:num_bytes]
                    self._buffer = self._buffer[num_bytes:]
                    return chunk

                # Check for timeout (prevent infinite wait on dead streams)
                if time.time() - timeout_start > timeout:
                    debug("Stream read timeout")
                    # If buffer has any content, return what we have
                    if len(self._buffer) > 0:
                        chunk = self._buffer
                        self._buffer = b''
                        return chunk
                    raise TimeoutError("Stream read timeout")

            time.sleep(0.005)

def _validate_stream_data(url, timeout):
    """Helper function to validate stream data with timeout"""
    # Extract hostname for DNS cache
    parsed_url = up.urlparse(url)
    hostname = parsed_url.netloc.split(':')[0]

    # Check DNS resolution first (quick check)
    if hostname and not get_cached_dns(hostname):
        debug(f"DNS resolution failed for {hostname}")
        return False

    for retry in range(MAX_RETRIES + 1):
        try:
            debug(f"Validating stream URL (attempt {retry+1}/{MAX_RETRIES+1}) with timeout {timeout}s: {url}")
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Eigenradio/0.1.0',
                'Connection': 'close',  # Don't keep the connection alive
                'Accept': '*/*'
            })

            # Set timeouts for both connection and read
            with session.get(url, stream=True,
                          timeout=(STREAM_CONNECT_TIMEOUT, timeout)) as response:
                response.raise_for_status()

                # Check content type (should be audio)
                content_type = response.headers.get('Content-Type', '').lower()
                if not ('audio' in content_type or 'stream' in content_type or
                        'ogg' in content_type or 'mpeg' in content_type or
                        'video' in content_type or 'mp3' in content_type or
                        'mp4' in content_type or 'octet-stream' in content_type):
                    debug(f"Suspicious content type: {content_type}")

                # Try to read some content
                chunk = next(response.iter_content(STREAM_VALIDATION_SIZE), None)
                if not chunk:
                    debug("No data received from stream")
                    if retry < MAX_RETRIES:
                        time.sleep(0.5)
                        continue
                    return False

                # Check for reasonable chunk size
                if len(chunk) < 100:  # Arbitrary minimum size
                    debug(f"Stream returned very small data chunk: {len(chunk)} bytes")
                    if retry < MAX_RETRIES:
                        time.sleep(0.5)
                        continue
                    return False

            return True

        except (RequestException, socket.error) as e:
            debug(f"Stream validation attempt {retry+1} failed: {str(e)}")
            if retry < MAX_RETRIES:
                time.sleep(0.5)
                continue
            return False

    return False

def validate_stream_url(url: str, timeout: int = STREAM_VALIDATION_TIMEOUT) -> bool:
    """
    Validate that a URL points to a valid audio stream with a timeout

    Args:
        url: URL to validate
        timeout: Timeout in seconds

    Returns:
        bool: True if the URL is a valid stream, False otherwise
    """
    try:
        # Use a thread with timeout to avoid blocking indefinitely
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_validate_stream_data, url, timeout)
            try:
                return future.result(timeout=timeout + 5)  # Give extra time for retries
            except TimeoutError:
                debug(f"Stream validation timed out after {timeout}s: {url}")
                future.cancel()
                return False

    except Exception as e:
        debug(f"Unexpected error during stream validation: {str(e)}")
        return False

def resolve_playlist(url: str, timeout=5) -> str:
    """
    Turn a .pls/.m3u URL into the first real stream URL it contains.

    Args:
        url: The URL to resolve
        timeout: Timeout in seconds for HTTP requests

    Returns:
        The resolved URL

    Raises:
        RuntimeError: If no stream URL could be found
    """
    path = up.urlparse(url).path
    if path and '.' not in path.split('/')[-1]:
        return url  # already looks like /mount

    # Extract hostname for DNS cache
    parsed_url = up.urlparse(url)
    hostname = parsed_url.netloc.split(':')[0]

    # Check DNS resolution first (quick check)
    if hostname and not get_cached_dns(hostname):
        raise RuntimeError(f"DNS resolution failed for {hostname}")

    for retry in range(MAX_RETRIES + 1):
        try:
            debug(f"Resolving playlist (attempt {retry+1}/{MAX_RETRIES+1}): {url}")
            # Use a shorter timeout for playlist resolution
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Eigenradio/0.1.0',
                'Connection': 'close',  # Don't keep the connection alive
                'Accept': '*/*'
            })

            r = session.get(url, timeout=timeout)
            r.raise_for_status()  # Raise exception for 4XX/5XX responses

            body = r.text.splitlines()
            ct = r.headers.get('Content-Type', '').lower()

            if url.lower().endswith('.pls') or 'scpls' in ct:
                for line in body:
                    m = PLAYLIST_RE.match(line)
                    if m:
                        resolved_url = m.group(1).strip()
                        debug(f"Resolved .pls to: {resolved_url}")
                        return resolved_url

            if (url.lower().endswith(('.m3u', '.m3u8')) or
                    'mpegurl' in ct or 'vnd.apple.mpegurl' in ct):
                for line in body:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        debug(f"Resolved m3u to: {line}")
                        return line

            if retry < MAX_RETRIES:
                debug(f"No stream URL found, retrying {retry+1}/{MAX_RETRIES}")
                time.sleep(0.5)
                continue

            raise RuntimeError(f"No stream URL found inside {url!r}")

        except (RequestException, socket.error) as e:
            debug(f"Error resolving playlist {url} (attempt {retry+1}): {str(e)}")
            if retry < MAX_RETRIES:
                time.sleep(0.5)
                continue
            raise RuntimeError(f"Failed to resolve playlist: {str(e)}") from e

    raise RuntimeError(f"Failed to resolve playlist after {MAX_RETRIES+1} attempts")

def icecast_stream(url: str, frames=FRAME_SIZE):
    """
    Return a miniaudio generator that yields PCM frames.

    Args:
        url: The URL to stream from
        frames: Number of frames to read at a time

    Returns:
        A generator that yields PCM frames

    Raises:
        StreamConnectionError: If connection to the stream fails
        StreamFormatError: If the stream format is invalid
    """
    for retry in range(MAX_RETRIES + 1):
        try:
            url = resolve_playlist(url)
            debug(f"Creating stream (attempt {retry+1}/{MAX_RETRIES+1}) for: {url}")

            # Validate the stream URL with timeout
            if not validate_stream_url(url):
                if retry < MAX_RETRIES:
                    debug(f"Stream validation failed, retrying {retry+1}/{MAX_RETRIES}")
                    time.sleep(0.5)
                    continue
                raise StreamConnectionError(f"Failed to validate stream: {url}")

            # Create client and return generator
            client = SmoothIceCast(url)
            return ma.stream_any(client,
                                nchannels=CHANNELS,
                                sample_rate=RATE,
                                output_format=ma.SampleFormat.SIGNED16,
                                frames_to_read=frames)

        except ma.MiniaudioError as e:
            debug(f"Miniaudio error (attempt {retry+1}) for {url}: {str(e)}")
            if retry < MAX_RETRIES:
                time.sleep(0.5)
                continue
            raise StreamFormatError(f"Stream format error: {str(e)}") from e
        except TimeoutError as e:
            debug(f"Timeout error (attempt {retry+1}) for {url}: {str(e)}")
            if retry < MAX_RETRIES:
                time.sleep(0.5)
                continue
            raise StreamTimeoutError(f"Stream timeout: {str(e)}") from e
        except Exception as e:
            debug(f"Error creating stream (attempt {retry+1}) for {url}: {str(e)}")
            if retry < MAX_RETRIES:
                time.sleep(0.5)
                continue
            raise StreamConnectionError(f"Stream connection error: {str(e)}") from e

    # Should never reach here, but just in case
    raise StreamConnectionError(f"Failed to create stream after {MAX_RETRIES+1} attempts")

def open_stream(url: str):
    """
    Run in background thread: open and fully probe a stream generator.

    This validates the stream and ensures it's playable before returning.

    Args:
        url: The URL to stream from

    Returns:
        A generator that yields PCM frames

    Raises:
        Various exceptions if the stream cannot be opened
    """
    debug(f"Opening stream: {url}")
    # Set a timer to prevent blocking indefinitely
    start_time = time.time()

    try:
        generator = icecast_stream(url)

        # Force loading at least one frame to validate the stream is working
        # but with a timeout to prevent blocking indefinitely
        if time.time() - start_time > STREAM_VALIDATION_TIMEOUT:
            raise StreamTimeoutError(f"Timeout while opening stream: {url}")

        # Try to get one sample to verify the stream works
        next(generator)
        return generator

    except Exception as e:
        if time.time() - start_time > STREAM_VALIDATION_TIMEOUT:
            debug(f"Stream open timed out for {url}")
            raise StreamTimeoutError(f"Timeout while opening stream: {url}")
        debug(f"Stream validation failed for {url}: {str(e)}")
        raise
