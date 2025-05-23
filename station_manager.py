"""
Station management functionality for Eigenradio
"""

import random
import requests
import xml.etree.ElementTree as ET
import time
import json
import os
from typing import List, Optional, Set, Dict, Tuple
import concurrent.futures

from config import debug

# File to store station stats
STATS_FILE = "station_stats.json"

# Cache for checked stations
checked_stations = {}
# Track failed stations to avoid immediate retries
failed_stations = {}
# Time to avoid retrying stations that recently failed (8 hours in seconds)
FAILED_STATION_AVOID_DURATION = 8 * 60 * 60  # 8 hours
# Maximum allowed failures before permanently blacklisting a station
MAX_ALLOWED_FAILURES = 5
# Track consecutively failed stations in a session
consecutive_failures: Set[str] = set()
# Track success rate of stations
station_stats: Dict[str, Dict] = {}
# Maximum consecutive failures before we reset our exclusion list
MAX_CONSECUTIVE_FAILURES = 10

# Track recently played stations in this session
# Format: (url, timestamp)
recent_play_history: List[Tuple[str, float]] = []
# Maximum play history to maintain - increased to track more stations
MAX_PLAY_HISTORY = 50
# How much to penalize recently played stations (higher = stronger preference for fresh stations)
# Set to maximum penalty of 1.0 to strongly discourage repeats
RECENT_PLAY_PENALTY = 1.0
# Time window where the recency penalty gradually decreases (in seconds)
# Increased significantly to extend the penalty duration - now 24 hours
RECENCY_WINDOW = 86400  # 24 hours
# Minimum number of stations to try in each selection round
MIN_SELECTION_CANDIDATES = 5

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
    print(f"Saving stats for {len(station_stats)} stations to {STATS_FILE}")
    try:
        with open(STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(station_stats, f, indent=2)
        print(f"Saved stats for {len(station_stats)} stations to {STATS_FILE}")
    except IOError as e:
        print(f"Error saving stats file: {str(e)}")

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

def reset_station_cache(url: Optional[str] = None):
    """
    Reset the station check cache, either for a specific URL or all URLs.
    This forces the system to re-check a station even if it was previously checked.

    Args:
        url: Optional URL to reset. If None, reset all station caches.
    """
    global checked_stations, failed_stations

    if url is None:
        # Reset all station caches
        debug("Resetting all station caches")
        checked_stations.clear()
        failed_stations.clear()
    elif url in checked_stations:
        # Reset just this URL
        debug(f"Resetting cache for {url}")
        del checked_stations[url]
        if url in failed_stations:
            del failed_stations[url]

    # Also clean up any stale entries in consecutive_failures
    for failure_url in list(consecutive_failures):
        if not any((station_url == failure_url for station_url, _ in recent_play_history)):
            consecutive_failures.discard(failure_url)

def check_station_url(url: str, timeout=5, force_check=False) -> bool:
    """Check if a station URL is responsive and working"""
    # Basic URL validation first
    if not url or not isinstance(url, str):
        debug(f"Invalid URL provided: {url}")
        return False

    # If force check requested, clear the cache for this URL
    if force_check and url in checked_stations:
        del checked_stations[url]
        if url in failed_stations:
            del failed_stations[url]

    # Check if this station has exceeded maximum allowed failures (permanent blacklist)
    if url in station_stats and station_stats[url]['failures'] >= MAX_ALLOWED_FAILURES:
        debug(f"Skipping permanently blacklisted station (failed {station_stats[url]['failures']} times): {url}")
        return False

    # Check if this station failed within the avoid duration window
    if url in station_stats and station_stats[url]['last_failure'] is not None:
        time_since_failure = time.time() - station_stats[url]['last_failure']
        if time_since_failure < FAILED_STATION_AVOID_DURATION:
            debug(f"Skipping recently failed station (failed {time_since_failure:.1f} seconds ago): {url}")
            return False

    # If it's a consecutive failure in this session, skip it
    if url in consecutive_failures:
        debug(f"Skipping station with consecutive failures in this session: {url}")
        return False

    # Check cached result
    if url in checked_stations:
        return checked_stations[url]

    # Try to connect with improved timeout handling
    try:
        debug(f"Checking station URL: {url}")
        # Try HEAD request first (faster) with explicit timeout
        response = requests.head(url, allow_redirects=True, timeout=timeout)
        if response.status_code == 200:
            checked_stations[url] = True
            # Clear from failed stations if it was there
            if url in failed_stations:
                del failed_stations[url]
            # Update stats
            update_station_stats(url, True)
            return True

        # Fallback to GET if HEAD fails, with explicit timeout
        debug(f"HEAD request failed for {url}, trying GET")
        response = requests.get(url, stream=True, timeout=timeout)
        if response.status_code == 200:
            # Read a bit of content to verify stream is active, with timeout
            try:
                content = next(response.iter_content(1024), None)
                if content:
                    checked_stations[url] = True
                    # Clear from failed stations if it was there
                    if url in failed_stations:
                        del failed_stations[url]
                    # Update stats
                    update_station_stats(url, True)
                    return True
            except Exception as content_error:
                debug(f"Error reading content from {url}: {str(content_error)}")

    except requests.exceptions.Timeout as e:
        debug(f"Timeout checking station {url}: {str(e)}")
    except requests.exceptions.ConnectionError as e:
        debug(f"Connection error checking station {url}: {str(e)}")
    except requests.exceptions.RequestException as e:
        debug(f"Request error checking station {url}: {str(e)}")
    except Exception as e:
        debug(f"Unexpected error checking station {url}: {str(e)}")

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

def add_to_play_history(url: str):
    """Add a station to the play history"""
    global recent_play_history
    current_time = time.time()
    recent_play_history.append((url, current_time))

    # Keep history limited to MAX_PLAY_HISTORY entries
    if len(recent_play_history) > MAX_PLAY_HISTORY:
        recent_play_history = recent_play_history[-MAX_PLAY_HISTORY:]

    debug(f"Updated play history: now tracking {len(recent_play_history)} recent stations")
    # Print current play history for debugging
    play_history_str = ", ".join([f"{u} ({int(time.time() - t)}s ago)" for u, t in recent_play_history])
    debug(f"Current play history: {play_history_str}")

    # Since this station is playing successfully, clear it from any error caches
    if url in consecutive_failures:
        consecutive_failures.remove(url)

    # Mark it as checked and good in our cache
    checked_stations[url] = True
    if url in failed_stations:
        del failed_stations[url]

def get_recency_penalty(url: str) -> float:
    """
    Calculate a penalty for recently played stations

    Returns a value between 0 and RECENT_PLAY_PENALTY, where higher means more penalty
    """
    current_time = time.time()
    max_penalty = 0.0

    # Check if the station is in the play history
    for station_url, timestamp in recent_play_history:
        if station_url == url:
            # Calculate how recently it was played
            time_since_played = current_time - timestamp

            # If it was played within the recency window, apply a penalty
            if time_since_played < RECENCY_WINDOW:
                # Penalty decreases linearly with time
                penalty_factor = 1 - (time_since_played / RECENCY_WINDOW)
                penalty = RECENT_PLAY_PENALTY * penalty_factor
                # Take the highest penalty if played multiple times
                if penalty > max_penalty:
                    max_penalty = penalty

    return max_penalty

def get_station_score(url: str) -> float:
    """
    Calculate a score for a station based on its reliability and recency

    Returns a value between 0 and 1, where higher is better
    """
    if url not in station_stats:
        return 0.5  # Neutral score for unknown stations

    stats = station_stats[url]
    total = stats['attempts']
    if total == 0:
        return 0.5

    # If station has reached or exceeded max allowed failures, return 0
    if stats['failures'] >= MAX_ALLOWED_FAILURES:
        return 0

    # Base score is success rate
    score = stats['successes'] / total

    # Reduce score for stations that have failed recently
    if stats['last_failure'] is not None:
        seconds_since_failure = time.time() - stats['last_failure']
        if seconds_since_failure < FAILED_STATION_AVOID_DURATION:
            # Reduce score based on recency of failure
            recency_penalty = 1 - (seconds_since_failure / FAILED_STATION_AVOID_DURATION)
            score *= (1 - recency_penalty * 0.5)

    # Boost score for stations that succeeded recently
    if stats['last_success'] is not None:
        seconds_since_success = time.time() - stats['last_success']
        if seconds_since_success < FAILED_STATION_AVOID_DURATION:
            # Boost score based on recency of success
            recency_bonus = 1 - (seconds_since_success / FAILED_STATION_AVOID_DURATION)
            score = score + (1 - score) * recency_bonus * 0.5

    # Apply recency penalty for stations played recently
    recency_penalty = get_recency_penalty(url)
    score -= recency_penalty

    # Ensure score stays in 0-1 range
    return max(0.0, min(1.0, score))

def get_play_count_in_history(url: str) -> int:
    """Count how many times a station appears in the play history"""
    return sum(1 for station_url, _ in recent_play_history if station_url == url)

def clean_failed_stations():
    """Remove expired entries from failed_stations"""
    current_time = time.time()
    expired = [url for url, (timestamp, _) in failed_stations.items()
               if current_time - timestamp > FAILED_STATION_AVOID_DURATION]

    for url in expired:
        del failed_stations[url]

    # If we've had too many consecutive failures, reset our exclusion list
    if len(consecutive_failures) > MAX_CONSECUTIVE_FAILURES:
        debug(f"Too many consecutive failures ({len(consecutive_failures)}), resetting exclusion list")
        consecutive_failures.clear()

def verify_previously_working_stations_async(stations: List[str], executor=None) -> concurrent.futures.Future:
    """
    Asynchronously check if stations that were previously working are still working.
    Returns a Future that will resolve to a list of stations that are verified to be working now.

    Args:
        stations: List of station URLs to verify
        executor: ThreadPoolExecutor to use for async execution

    Returns:
        Future[List[str]]
    """
    if executor is None:
        raise ValueError("An executor must be provided for async verification.")

    def check():
        working_stations = []
        recently_played = [url for url, _ in recent_play_history]
        for url in recently_played:
            if url in stations:
                reset_station_cache(url)
                if check_station_url(url, force_check=True):
                    working_stations.append(url)
        if len(working_stations) >= 2:
            return working_stations
        remaining = [s for s in stations if s not in working_stations]
        random.shuffle(remaining)
        for url in remaining[:5]:
            reset_station_cache(url)
            if check_station_url(url, force_check=True):
                working_stations.append(url)
        return working_stations
    return executor.submit(check)

def select_diverse_station(available_stations: List[str], exclude: Optional[str] = None):
    """
    Select a station with emphasis on diversity - tries to avoid recently played stations
    with strong preference for stations that haven't been played at all
    """
    if not available_stations:
        return None

    # Remove excluded station if present
    candidates = [s for s in available_stations if s != exclude]
    if not candidates:
        return None

    # First, check for stations that have NEVER been played in this session
    # These get absolute priority to maximize diversity
    never_played = []
    for station in candidates:
        if get_play_count_in_history(station) == 0:
            never_played.append(station)

    # If we found stations that have never been played, prioritize those
    # and only consider them for selection
    if never_played:
        debug(f"Found {len(never_played)} stations that have never been played")
        # Only score these never-played stations
        scored_candidates = [(url, get_station_score(url)) for url in never_played]
        # Sort by score, highest first
        scored_candidates.sort(key=lambda x: x[1], reverse=True)

        # Show debug info
        debug(f"Selecting from {len(never_played)} never-played stations")
        for url, score in scored_candidates[:5]:  # Show top 5 for brevity
            debug(f"  {url}: score={score:.2f}")

        # Return the highest-scoring never-played station
        if scored_candidates:
            return scored_candidates[0][0]

    # If no never-played stations, continue with standard approach
    # Group stations by how many times they've appeared in history
    play_counts = {}
    for station in candidates:
        count = get_play_count_in_history(station)
        if count not in play_counts:
            play_counts[count] = []
        play_counts[count].append(station)

    # Start with stations that have been played the least
    min_count = min(play_counts.keys()) if play_counts else 0
    least_played = play_counts.get(min_count, candidates)

    # Score these least-played stations
    scored_candidates = [(url, get_station_score(url)) for url in least_played]
    # Sort by score, highest first
    scored_candidates.sort(key=lambda x: x[1], reverse=True)

    # Debug info about selection
    debug(f"Selecting from {len(least_played)} least-played stations (played {min_count} times)")
    for url, score in scored_candidates[:5]:  # Show top 5 for brevity
        debug(f"  {url}: score={score:.2f}, penalty={get_recency_penalty(url):.2f}, play_count={get_play_count_in_history(url)}")

    # Return the highest-scoring station among the least-played ones
    if scored_candidates:
        return scored_candidates[0][0]

    return random.choice(candidates)  # Fallback

def get_random_station(stations: List[str], max_attempts=15, exclude: Optional[str] = None, executor=None) -> str:
    """
    Get a random working station from the list, with preference for stations
    that haven't been played recently.

    Args:
        stations: List of station URLs
        max_attempts: Maximum number of attempts to find a working station
        exclude: URL to exclude (typically the current station)
        executor: ThreadPoolExecutor to use for async verification

    Returns:
        A working station URL

    Raises:
        Exception: If no working stations could be found
    """
    # Validate input
    if not stations:
        raise Exception("No stations provided")

    # Filter out any None or invalid URLs
    valid_stations = [s for s in stations if s and isinstance(s, str)]
    if not valid_stations:
        raise Exception("No valid station URLs provided")

    clean_failed_stations()

    debug(f"Getting random station from {len(valid_stations)} valid stations")
    tried = set()
    attempts = 0

    # Create a blacklist of recently played stations to enforce diversity
    # Get the timestamps for recently played stations
    current_time = time.time()
    recent_blacklist = set()

    # Get all stations played in the last 60 minutes (strict blacklist)
    STRICT_BLACKLIST_WINDOW = 60 * 60  # 1 hour
    for station_url, timestamp in recent_play_history:
        time_since_played = current_time - timestamp
        if time_since_played < STRICT_BLACKLIST_WINDOW:
            recent_blacklist.add(station_url)
            debug(f"Station {station_url} blacklisted (played {time_since_played:.0f} seconds ago)")

    # If we have enough stations, filter out all recently played ones
    if len(valid_stations) - len(recent_blacklist) > 3:
        debug(f"Filtering out {len(recent_blacklist)} recently played stations")
        filtered_stations = [s for s in valid_stations if s not in recent_blacklist]
    else:
        # If filtering would leave too few stations, just continue with all
        filtered_stations = valid_stations
        debug(f"Not enough stations to filter ({len(valid_stations)} total, {len(recent_blacklist)} played recently)")

    # Before randomly trying stations, let's verify if any previously working ones
    # are still available (using our filtered list)
    working_stations_future = None
    working_stations = []
    if executor:
        try:
            working_stations_future = verify_previously_working_stations_async(filtered_stations, executor)
            working_stations = working_stations_future.result() if working_stations_future else []
            if exclude and exclude in working_stations:
                working_stations.remove(exclude)
        except Exception as e:
            debug(f"Error during async verification: {e}")
            working_stations = []

    if working_stations:
        debug(f"Found {len(working_stations)} verified working stations")
        # Use the diverse station algorithm on these known-good stations
        diverse_pick = select_diverse_station(working_stations, exclude)
        if diverse_pick:
            debug(f"Selected verified working station: {diverse_pick}")
            add_to_play_history(diverse_pick)
            return diverse_pick

    # Filter out excluded station from our already filtered list
    available_stations = [s for s in filtered_stations if s != exclude]
    if not available_stations and exclude and check_station_url(exclude):
        # If all other stations are unavailable, reuse the current one
        debug("No alternative stations available, reusing current")
        add_to_play_history(exclude)
        return exclude

    # Ensure we have at least some stations to work with
    if not available_stations:
        debug("No available stations after filtering, using all valid stations")
        available_stations = [s for s in valid_stations if s != exclude]
        if not available_stations:
            # Last resort - use all stations including the excluded one
            available_stations = valid_stations

    # Choose stations based on their reliability score and play history
    while attempts < max_attempts and len(tried) < len(available_stations):
        # Get at least MIN_SELECTION_CANDIDATES to ensure we have enough diversity
        num_candidates = max(MIN_SELECTION_CANDIDATES, min(10, len(available_stations) - len(tried)))

        # Select a batch of random stations to score
        candidates = []
        for _ in range(num_candidates):
            if len(candidates) >= len(available_stations) - len(tried):
                break

            candidate = random.choice(available_stations)
            while candidate in tried or candidate in candidates:
                if len(available_stations) - len(tried) <= len(candidates):
                    break
                candidate = random.choice(available_stations)

            if candidate not in tried and candidate not in candidates:
                candidates.append(candidate)
            tried.add(candidate)

        if not candidates:
            break

        # Try using the diverse station selection first
        diverse_candidate = select_diverse_station(candidates, exclude)
        if diverse_candidate:
            attempts += 1
            debug(f"Attempt {attempts}/{max_attempts}: Trying diverse candidate {diverse_candidate}")

            if check_station_url(diverse_candidate):
                debug(f"Found working station: {diverse_candidate}")
                # Add to play history
                add_to_play_history(diverse_candidate)
                return diverse_candidate

        # Fall back to traditional scoring method if diverse selection fails
        scored_candidates = [(url, get_station_score(url)) for url in candidates]
        # Sort by score, highest first
        scored_candidates.sort(key=lambda x: x[1], reverse=True)

        # Try candidates in order of score
        for url, score in scored_candidates:
            if url in tried:
                continue
            attempts += 1
            play_count = get_play_count_in_history(url)
            debug(f"Attempt {attempts}/{max_attempts}: Trying {url} (score: {score:.2f}, recency penalty: {get_recency_penalty(url):.2f}, played {play_count} times)")

            # Clear this URL from the cache to ensure a fresh check
            # This is important for stations that might have failed earlier
            reset_station_cache(url)

            if check_station_url(url, force_check=True):
                debug(f"Found working station: {url}")
                # Add to play history
                add_to_play_history(url)
                return url

    # If we've tried all stations without success but exclude works, use it
    if exclude and check_station_url(exclude):
        debug("All alternatives failed, reusing current station")
        # Add to play history even when reusing
        add_to_play_history(exclude)
        return exclude

    # If we have too many consecutive failures, clear the list and try again with one random station
    if len(consecutive_failures) > MAX_CONSECUTIVE_FAILURES and len(valid_stations) > MAX_CONSECUTIVE_FAILURES:
        consecutive_failures.clear()
        reset_station_cache()  # Reset all station caches
        debug("Too many consecutive failures, resetting and trying a random station")
        url = random.choice(valid_stations)
        if check_station_url(url, force_check=True):
            add_to_play_history(url)
            return url

    # Last resort - return any random available station without checking
    if len(valid_stations) > 0:
        debug("All checks failed, returning a random station without validation")
        url = random.choice(valid_stations)
        add_to_play_history(url)
        return url

    raise Exception(f"No working stations found after {attempts} attempts")

# Load stats when module is imported
load_stats_from_disk()
