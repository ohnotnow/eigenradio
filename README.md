# Eigenradio

A command-line Internet radio player written in Python. Eigenradio parses M3U or Icecast XML playlists, randomly selects a working stream and crossfades between stations with static-noise filler as a faux 'retune the radio'.

Named in memory of the MIT 'Eigenradio' site from years gone past.

## Features
- Supports M3U (`.m3u`) playlists and Icecast XML files
- Random station selection with automatic health checks
- Crossfades between streams using a static‐noise track
- Configurable play duration and fade times
- Pure‐Python implementation leveraging `miniaudio` and `requests`

## Requirements
- Python 3.x (tested on 3.8+)
- [`uv`](https://docs.astral.sh/uv/) (modern Python dependency runner)
- A valid static‐noise file (`static.mp3` by default)

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

You can grab a decent icecast xml file from `https://dir.xiph.org/yp.xml` or find lots of links on sites like [InternetRadio](https://www.internet-radio.com/) or [StreamURL](https://streamurl.link/).  There is an example m3u file in the repo (`example.m3u` to get started - though stations go out of date quickly).

Optional flags:
```
  --static-file <file>   Path to static‐noise MP3 (default: `static.mp3`)
  --playtime <secs>      Seconds to play each stream before crossfade (default: 600)
  --fade <secs>          Duration of fade‐in/out phases (default: 3)
```

Examples:
```
uv run main.py \
  --m3u-file stations.m3u \
  --static-file noise.mp3 \
  --playtime 300 \
  --fade 5
```

For a very quick idea of how it sounds (using the example m3u in the repo):
```
uv run main.py --m3u-file example.m3u --playtime=20
```

## License

This project is licensed under the MIT License.  See [LICENSE](LICENSE) for details.
