"""
Station management functionality for Eigenradio
"""

import random
import requests
import xml.etree.ElementTree as ET
from typing import List

from config import debug

# Cache for checked stations
checked_stations = {}

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

def get_random_station(stations: List[str], max_attempts=10) -> str:
    """Get a random working station from the list"""
    debug(f"Getting random station from {len(stations)} stations")
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
