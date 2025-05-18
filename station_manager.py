"""
Station management functionality for Eigenradio
"""

import random
import requests
import xml.etree.ElementTree as ET
import time
import json
import os
from typing import List, Optional, Set, Dict

from config import debug

# File to store station stats
STATS_FILE = "station_stats.json"

# Cache for checked stations
checked_stations = {}
# Track failed stations to avoid immediate retries
failed_stations = {}
# Maximum age for failed station entries (seconds)
FAILED_STATION_EXPIRY = 600  # 10 minutes
# Track consecutively failed stations in a session
consecutive_failures: Set[str] = set()
# Track success rate of stations
station_stats: Dict[str, Dict] = {}
# Maximum consecutive failures before we reset our exclusion list
MAX_CONSECUTIVE_FAILURES = 10

def load_stats_from_disk():
    """Load station stats from disk"""
    global station_stats
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r', encoding='utf-8') as f:
                station_stats = json.load(f)
            debug(f"Loaded stats for {len(station_stats)} stations from {STATS_FILE}")
        except (json.JSONDecodeError, IOError) as e:
            debug(f"Error loading stats file: {str(e)}")
            station_stats = {}

def save_stats_to_disk():
    """Save station stats to disk"""
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(station_stats, f, indent=2)
        debug(f"Saved stats for {len(station_stats)} stations to {STATS_FILE}")
    except IOError as e:
        debug(f"Error saving stats file: {str(e)}")

def parse_m3u(file_path: str) -> List[str]:
    """Parse an M3U file and return a list of stream URLs"""
    stations = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                stations.append(line)
    return stations

def parse_icecast(file_path: str) -> List[str]:
    """Parse an Icecast XML file and return a list of stream URLs"""
    tree = ET.parse(file_path)
    root = tree.getroot()
    stations = [
        elem.text.strip()
        for elem in root.iter('listen_url')
        if elem.text and elem.text.strip()
    ]
    return stations

def check_station_url(url: str, timeout=5) -> bool:
    """Check if a station URL is responsive and working"""
    # Check if this station recently failed
    if url in failed_stations:
        failure_time, attempts = failed_stations[url]
        # If failure was recent and too many attempts, skip it
        if (time.time() - failure_time < FAILED_STATION_EXPIRY and
            attempts > 2):
            debug(f"Skipping recently failed station: {url}")
            return False

    # If it's a consecutive failure in this session, skip it
    if url in consecutive_failures:
        debug(f"Skipping station with consecutive failures in this session: {url}")
        return False

    # Check cached result
    if url in checked_stations:
        return checked_stations[url]

    # Try to connect
    try:
        debug(f"Checking station URL: {url}")
        # Try HEAD request first (faster)
        response = requests.head(url, allow_redirects=True, timeout=timeout)
        if response.status_code == 200:
            checked_stations[url] = True
            # Clear from failed stations if it was there
            if url in failed_stations:
                del failed_stations[url]
            # Update stats
            update_station_stats(url, True)
            return True

        # Fallback to GET if HEAD fails
        debug(f"HEAD request failed for {url}, trying GET")
        response = requests.get(url, stream=True, timeout=timeout)
        if response.status_code == 200:
            # Read a bit of content to verify stream is active
            content = next(response.iter_content(1024), None)
            if content:
                checked_stations[url] = True
                # Clear from failed stations if it was there
                if url in failed_stations:
                    del failed_stations[url]
                # Update stats
                update_station_stats(url, True)
                return True

    except requests.exceptions.RequestException as e:
        debug(f"Error checking station {url}: {str(e)}")

    # Mark as failed
    checked_stations[url] = False

    # Update failed stations record
    current_time = time.time()
    if url in failed_stations:
        _, attempts = failed_stations[url]
        failed_stations[url] = (current_time, attempts + 1)
    else:
        failed_stations[url] = (current_time, 1)

    # Update stats
    update_station_stats(url, False)

    return False

def update_station_stats(url: str, success: bool):
    """Update statistics for a station"""
    if url not in station_stats:
        station_stats[url] = {
            'attempts': 0,
            'successes': 0,
            'failures': 0,
            'last_success': None,
            'last_failure': None
        }

    stats = station_stats[url]
    stats['attempts'] += 1

    if success:
        stats['successes'] += 1
        stats['last_success'] = time.time()
        # Remove from consecutive failures if present
        if url in consecutive_failures:
            consecutive_failures.remove(url)
    else:
        stats['failures'] += 1
        stats['last_failure'] = time.time()
        # Add to consecutive failures
        consecutive_failures.add(url)

    # Save stats periodically (every 5 updates to avoid constant disk writes)
    if stats['attempts'] % 5 == 0:
        save_stats_to_disk()

def get_station_score(url: str) -> float:
    """
    Calculate a score for a station based on its reliability

    Returns a value between 0 and 1, where higher is better
    """
    if url not in station_stats:
        return 0.5  # Neutral score for unknown stations

    stats = station_stats[url]
    total = stats['attempts']
    if total == 0:
        return 0.5

    # Base score is success rate
    score = stats['successes'] / total

    # Reduce score for stations that have failed recently
    if stats['last_failure'] is not None:
        seconds_since_failure = time.time() - stats['last_failure']
        if seconds_since_failure < FAILED_STATION_EXPIRY:
            # Reduce score based on recency of failure
            recency_penalty = 1 - (seconds_since_failure / FAILED_STATION_EXPIRY)
            score *= (1 - recency_penalty * 0.5)

    # Boost score for stations that succeeded recently
    if stats['last_success'] is not None:
        seconds_since_success = time.time() - stats['last_success']
        if seconds_since_success < FAILED_STATION_EXPIRY:
            # Boost score based on recency of success
            recency_bonus = 1 - (seconds_since_success / FAILED_STATION_EXPIRY)
            score = score + (1 - score) * recency_bonus * 0.5

    return score

def clean_failed_stations():
    """Remove expired entries from failed_stations"""
    current_time = time.time()
    expired = [url for url, (timestamp, _) in failed_stations.items()
               if current_time - timestamp > FAILED_STATION_EXPIRY]

    for url in expired:
        del failed_stations[url]

    # If we've had too many consecutive failures, reset our exclusion list
    if len(consecutive_failures) > MAX_CONSECUTIVE_FAILURES:
        debug(f"Too many consecutive failures ({len(consecutive_failures)}), resetting exclusion list")
        consecutive_failures.clear()

def get_random_station(stations: List[str], max_attempts=15, exclude: Optional[str] = None) -> str:
    """
    Get a random working station from the list

    Args:
        stations: List of station URLs
        max_attempts: Maximum number of attempts to find a working station
        exclude: URL to exclude (typically the current station)

    Returns:
        A working station URL

    Raises:
        Exception: If no working stations could be found
    """
    clean_failed_stations()

    debug(f"Getting random station from {len(stations)} stations")
    tried = set()
    attempts = 0

    # Filter out excluded station
    available_stations = [s for s in stations if s != exclude]
    if not available_stations and exclude and check_station_url(exclude):
        # If all other stations are unavailable, reuse the current one
        debug("No alternative stations available, reusing current")
        return exclude

    # Choose stations based on their reliability score
    while attempts < max_attempts and len(tried) < len(available_stations):
        # Select a batch of random stations to score
        candidates = []
        for _ in range(min(10, len(available_stations) - len(tried))):
            candidate = random.choice(available_stations)
            while candidate in tried:
                candidate = random.choice(available_stations)
            candidates.append(candidate)
            tried.add(candidate)

        # Score the candidates
        scored_candidates = [(url, get_station_score(url)) for url in candidates]
        # Sort by score, highest first
        scored_candidates.sort(key=lambda x: x[1], reverse=True)

        # Try candidates in order of score
        for url, score in scored_candidates:
            attempts += 1
            debug(f"Attempt {attempts}/{max_attempts}: Trying {url} (score: {score:.2f})")

            if check_station_url(url):
                debug(f"Found working station: {url}")
                return url

    # If we've tried all stations without success but exclude works, use it
    if exclude and check_station_url(exclude):
        debug("All alternatives failed, reusing current station")
        return exclude

    # If we have too many consecutive failures, clear the list and try again with one random station
    if len(consecutive_failures) > MAX_CONSECUTIVE_FAILURES and len(stations) > MAX_CONSECUTIVE_FAILURES:
        consecutive_failures.clear()
        debug("Too many consecutive failures, resetting and trying a random station")
        url = random.choice(stations)
        if check_station_url(url):
            return url

    # Last resort - return any random available station without checking
    if len(stations) > 0:
        debug("All checks failed, returning a random station without validation")
        return random.choice(stations)

    raise Exception(f"No working stations found after {attempts} attempts")

# Load stats when module is imported
load_stats_from_disk()
