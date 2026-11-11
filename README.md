# wyzecam-rtsp

View your Wyze cameras' built-in RTSP streams in one live grid on **Linux, macOS or Windows**:
no Wyze app and no cloud. It scans your network, finds the cameras, and shows them tiled in a
single window with names, auto-reconnect, and live rearranging.

> **Important:** this only works with cameras that provide an RTSP stream, and it has only been
> tested with Wyze Cams. Other RTSP cameras may need a different port or stream path.

## 1. Turn on RTSP on each camera

RTSP is **off by default**. For each camera whose model and firmware support it, open the Wyze
mobile app: camera → **Settings → Advanced Settings → RTSP**, turn it on, and set a username and
password. Tested with Cam v3, Cam Pan v3 (production firmware) and Cam v4 (RTSP firmware).

Use the same username and password on every camera. Type them at the login prompt, or fill in
`DEFAULT_USER` / `DEFAULT_PASS` near the top of the script (or set `WYZE_RTSP_USER` /
`WYZE_RTSP_PASS`) so you can just press Enter.

## 2. Install (once)

| OS | Install | Run |
|---|---|---|
| **Linux** (Pop!_OS / Ubuntu) | `sudo apt install python3 ffmpeg mpv` | `./wyzecam-rtsp.py` |
| **macOS** | `brew install python ffmpeg mpv` | `./wyzecam-rtsp.py` |
| **Windows** | Python from [python.org](https://www.python.org/downloads/), then<br>`winget install -e --id Gyan.FFmpeg`<br>`winget install -e --id shinchiro.mpv` | `py wyzecam-rtsp.py` |

Put `wyzecam-rtsp.py` in its own folder. Its camera list (`cameras.json`) and logs are kept
next to it. On Linux/macOS, run `chmod +x wyzecam-rtsp.py` once.

**First-run permissions:**
- **macOS:** the first scan triggers a **Local Network** permission request for your terminal app.
  Allow it (System Settings → Privacy & Security → Local Network). Without it the scan finds nothing.
- **Windows:** if Windows Firewall asks about Python, allow it on **Private networks**.
  Open a new terminal after installing ffmpeg/mpv so they're on the PATH.

## 3. Use it

On Windows, use `py wyzecam-rtsp.py` wherever this says `./wyzecam-rtsp.py`.

```
./wyzecam-rtsp.py                 # play all cameras (scans automatically the first time)
./wyzecam-rtsp.py --scan          # rescan the network, then play
./wyzecam-rtsp.py -c 243,patio    # only these cameras, in this order
./wyzecam-rtsp.py -x 75           # everything except camera 75
./wyzecam-rtsp.py --sub           # lighter substreams: less Wi-Fi and CPU, plenty for a grid
./wyzecam-rtsp.py name            # name cameras while watching each one
./wyzecam-rtsp.py arrange         # set the saved order / which cameras show
./wyzecam-rtsp.py --list          # list saved cameras
./wyzecam-rtsp.py --help          # every option
```

**Keys in the grid** (click the window first):

| Key | Action | Key | Action |
|---|---|---|---|
| f | fullscreen | 1–9 | select a camera (yellow border) |
| n | names on/off | arrows | move the selected camera |
| r | resync all to live | d | hide it |
| h | help | e | rename it |
| q | quit | a | show all hidden |
| z | zoom selected | s | save the layout |

A **dark grey tile** means that camera has no live video right now. It keeps retrying on its own.

## How it behaves

- Each camera has its own decoder and reconnect loop, so a bad camera never freezes the others.
- Every camera reconnects every 10 minutes (staggered) to stay close to live (`--resync`).
- A camera whose main stream keeps dropping switches to its substream automatically,
  and that setting is saved.
- GPU decoding is used where available: VA-API on Linux, VideoToolbox on macOS, D3D11 on Windows.
- Logs: `wyzecam-rtsp.log` (events) and `wyzecam-rtsp-ffmpeg.log` (details, passwords removed).

## Checklist for a first run on macOS / Windows

These parts were tested on Linux and simulated for macOS/Windows, but haven't yet run on a real
Mac or Windows PC. On the first run, check:

1. `--scan` finds your cameras. (macOS: Local Network permission. Windows: firewall prompt.)
2. The grid opens and every tile moves.
3. Camera names appear on the tiles. If not, the font lookup needs a tweak.
4. Keys work: **h** shows help on screen, **1** then **→** moves a camera, **e** opens a rename box.
5. **q** quits cleanly and `wyzecam-rtsp.log` says "grid stopped".

If anything fails, run it again with `--verbose` and keep the terminal output and both log files.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Scan finds no cameras | RTSP not enabled in the Wyze app; wrong subnet (`--subnet 192.168.1.0/24`); macOS Local Network permission |
| "login rejected" | Username/password differ from the camera's RTSP login |
| A tile keeps going grey | Weak Wi-Fi at that camera: try `--sub`, or move an access point closer |
| "camera refused the connection" | Too many viewers on that camera (Wyze app, other players). Close them, wait a minute |
| Everything stutters | Use `--sub`, or `--fps 10` |
