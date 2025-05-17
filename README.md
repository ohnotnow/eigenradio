# Eigenradio

A command-line Internet radio player written in Python. Eigenradio parses M3U or Icecast XML playlists, randomly selects a working stream, plays it via VLC, and crossfades between stations with static-noise filler as a faux 'retune the radio'.

## Features
- Supports M3U (`.m3u`) playlists and Icecast XML files
- Random station selection with automatic health checks
- Crossfades between streams using a static‐noise track
- Configurable play duration and fade times
- Pure‐Python implementation leveraging `python-vlc` and `requests`

## Requirements
- Python 3.x (tested on 3.8+)
- VLC media player (with libVLC bindings)
- [`uv`](https://docs.astral.sh/uv/) (modern Python dependency runner)
- A valid static‐noise file (`static.mp3` by default)

The Python dependencies (`python-vlc`, `requests`) are installed via `uv sync`.

## Installation

Clone the repository and install dependencies:

macOS & Ubuntu
```
git clone https://github.com/ohnotnow/eigenradio.git
cd eigenradio
uv sync
```

Windows (PowerShell)
```
git clone https://github.com/ohnotnow/eigenradio.git
Set-Location eigenradio
uv sync
```

> For more on `uv`, visit https://docs.astral.sh/uv/

## Usage

Invoke the player with one of the mutually exclusive source options:

```
uv run main.py --m3u-file path/to/playlist.m3u
```
or
```
uv run main.py --icecast-file path/to/icecast.xml
```

You can grab a decent icecast xml file from `https://dir.xiph.org/yp.xml`.

Optional flags:
  --static-file \<file\>   Path to static‐noise MP3 (default: `static.mp3`)
  --playtime \<secs\>      Seconds to play each stream before crossfade (default: 600)
  --fade \<secs\>          Duration of fade‐in/out phases (default: 3)

Examples:
```
uv run main.py \
  --m3u-file stations.m3u \
  --static-file noise.mp3 \
  --playtime 300 \
  --fade 5
```

For a very quick idea of how it sounds (if you've grabbed the icecast xml file above):
```
uv run main.py --icecast-file icecast.xml --playtime=20
```

## License

This project is licensed under the MIT License.
See [LICENSE](LICENSE) for details.
