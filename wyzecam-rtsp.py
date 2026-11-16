#!/usr/bin/env python3
"""
wyzecam-rtsp.py — view your Wyze RTSP cameras in one live grid (Linux, macOS, Windows).

IMPORTANT — READ FIRST
  * This only works with cameras that provide an RTSP stream. It has only been tested with
    Wyze Cams (Cam v3, Cam Pan v3, and Cam v4 on RTSP-capable firmware); other RTSP cameras
    may need a different port or stream path.
  * RTSP is OFF by default on Wyze Cams. Turn it on for each camera in the Wyze mobile app,
    if that camera's model and firmware support it: camera -> Settings -> Advanced Settings
    -> RTSP, then set a username and password for the stream.
  * Type the RTSP username and password you created in the Wyze app at the login prompt,
    or put them in DEFAULT_USER / DEFAULT_PASS below (or $WYZE_RTSP_USER / $WYZE_RTSP_PASS).

QUICK START
  ./wyzecam-rtsp.py                     play ALL saved cameras in a grid (resync every 10 min)
  ./wyzecam-rtsp.py --scan              rescan the network first, then play
  ./wyzecam-rtsp.py -c "Front Door"     show just one camera (full window)
  ./wyzecam-rtsp.py -c 243,patio        show only these cameras, in this order (name, IP, or last IP number)
  ./wyzecam-rtsp.py -x 75               show everything EXCEPT these cameras
  ./wyzecam-rtsp.py --list              list saved cameras and exit

OTHER COMMANDS
  ./wyzecam-rtsp.py scan                scan the network only (saves cameras.json)
  ./wyzecam-rtsp.py name                name cameras while watching each one live
  ./wyzecam-rtsp.py arrange             choose the grid order and which cameras are shown
  ./wyzecam-rtsp.py test 243            find the best stream/decoder for one camera
  ./wyzecam-rtsp.py serve --web --record /mnt/nas/cams
                                        no window: browser view and/or recording (servers)
  ./wyzecam-rtsp.py ha                  print Home Assistant setup for each camera
  ./wyzecam-rtsp.py serve --web --detect
                                        no window: browser view with live AI detection

RECORDING AND BROWSER VIEW (grid or 'serve'; each camera still uses ONE camera session)
  --record DIR       record every camera into DIR/<camera name>/ (video copied as-is)
  --rec-format FMT   mkv (default: survives crashes) or mp4 (fragmented: plays in browsers/phones)
  --keep DAYS        delete recordings older than DAYS (default 7, 0 = keep forever)
  --segment MIN      minutes per recording file (default 5)
  --web [PORT]       browser view at http://this-pc:PORT/ (default 8080), RTSP login required;
                     with --record it also plays recordings (timeline per camera; press r)
  --web-bind ADDR    127.0.0.1 = only this computer (default: whole LAN)

LOCAL AI DETECTION (grid or 'serve'; one-time setup: pip install onnxruntime numpy)
  --detect           detect people, dogs and cats; boxes on the tiles, snapshots + events log
  --detect-labels L  what to detect (default person,dog,cat; e.g. add car,truck,bird)
  --detect-model M   nano (fastest) / tiny (default) / s (most accurate) / your own .onnx file
  --detect-cams LIST only these cameras     --detect-fps N    checks per second (default 2)
  --detect-webhook URL  POST each new detection as JSON (e.g. to Home Assistant)

PLAYBACK SWITCHES
  -c, --cam LIST     only these cameras (comma-separated; names, IPs, or last IP number;
                     a partial name works: 'carpark' matches every Carpark camera)
  -x, --exclude LIST hide these cameras
  --scan             rescan the network before playing
  --working-only     skip cameras whose last scan check failed (default: try them all)
  --resync MIN       reconnect each camera every MIN minutes to stay live (default 10, 0 = off)
  --sub              use the lighter substream for every camera
  --fps 10|15|20     grid frame rate (default 15)
  --windows          one window per camera instead of one grid
  --gpu              GPU scaling (needs VA-API video processing; not supported on every GPU)
  --verbose          show mpv/ffmpeg warnings
  --subnet CIDR      subnet to scan (default: auto-detect)

KEYS IN THE GRID
  f = fullscreen   n = names on/off   r = resync all now   h = help   q = quit

SETUP (once)
  Linux (Pop!_OS/Ubuntu):  sudo apt install python3 ffmpeg mpv        run:  ./wyzecam-rtsp.py
  macOS:                   brew install python ffmpeg mpv             run:  ./wyzecam-rtsp.py
                           (allow your terminal under System Settings > Privacy & Security >
                            Local Network, or the scan can't see the cameras)
  Windows:                 install Python from python.org, then
                           winget install -e --id Gyan.FFmpeg
                           winget install -e --id shinchiro.mpv        run:  py wyzecam-rtsp.py
                           (allow Python on Private networks if Windows Firewall asks)
  On Windows, write "py wyzecam-rtsp.py" wherever these notes say "./wyzecam-rtsp.py".

Login: you're prompted for username and password; Enter accepts DEFAULT_USER /
DEFAULT_PASS below if you've filled them in. $WYZE_RTSP_USER / $WYZE_RTSP_PASS skip the prompts.
App folder: cameras.json and logs live next to the script ($WYZECAM_RTSP_DIR overrides).
Needs: python3, ffmpeg (ffmpeg + ffprobe), mpv. For --detect also: onnxruntime, numpy.
"""
import argparse
import concurrent.futures as cf
import queue
import getpass
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

# ---- Which operating system are we on? (WYZECAM_RTSP_OS=linux|mac|windows forces one, for testing)
_OS = os.environ.get("WYZECAM_RTSP_OS") or (
    "windows" if sys.platform.startswith("win") else "mac" if sys.platform == "darwin" else "linux")
IS_WIN, IS_MAC, IS_LINUX = _OS == "windows", _OS == "mac", _OS == "linux"
RUN = "py wyzecam-rtsp.py" if IS_WIN else "./wyzecam-rtsp.py"     # how to run it, for messages

# Everything lives in the script's own folder (cameras.json, logs) unless overridden.
APP_DIR = Path(os.environ.get("WYZECAM_RTSP_DIR") or os.environ.get("WYZECAMS_DIR")  # old name still works
               or Path(__file__).resolve().parent)
CONF_DIR = APP_DIR
CONF_FILE = CONF_DIR / "cameras.json"
DEFAULT_PORT = 322
# Optional: your RTSP login, offered at the prompt (press Enter to accept). Leave empty to
# type it each time. Use the username/password you created for your cameras in the Wyze app
# (camera -> Settings -> Advanced Settings -> RTSP).
DEFAULT_USER = ""
DEFAULT_PASS = ""
MAIN, SUB = "stream0", "stream1"
VERBOSE = False
STALL_SECS = 8          # a camera that stops sending frames this long gets reconnected
FIRST_FRAME_SECS = 25   # allowance for the first frame after (re)connecting
# Recording (--record): video is copied as-is (no re-encoding) into files of this length.
REC_SEGMENT_MIN = 5
REC_KEEP_DAYS = 7       # recordings older than this are deleted (--keep, 0 = keep forever)
# Browser view (--web): JPEG snapshots, at most this wide, of every Nth camera frame.
WEB_PORT = 8080
WEB_WIDTH = 960
WEB_EVERY = 2           # Wyze cams send ~20 fps -> ~10 fps in the browser
WEB_QUALITY = 6         # JPEG quality: 2 (best, biggest) .. 31 (worst, smallest)
# Object detection (--detect): small frames from each camera go to one shared AI model.
DET_W, DET_H = 640, 360         # frame size sent to the detector (16:9, like the cameras)
DET_FPS = 2                     # frames per second checked per camera (cameras send ~20)
DET_SCORE = 0.5                 # minimum confidence for a detection to count
DET_LABELS = "person,dog,cat"   # what to report by default (--detect-labels)
DET_HEARTBEAT = 10              # also check this often (s) with no motion: still objects
DET_GONE = 30                   # an object unseen this long (s) ends its event
DET_KEEP_DAYS = 7               # event snapshots/log kept this long


# --------------------------------------------------------------------------- helpers
def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def popen_ffmpeg(cmd, **kw):
    """Start ffmpeg so that stop_ffmpeg() can later stop it politely on any OS.
    On Windows that means giving it a stdin pipe (it quits cleanly when sent 'q')."""
    if IS_WIN:
        kw["stdin"] = subprocess.PIPE
    else:
        # Own session: Ctrl+C in the terminal reaches only us, and we then stop each ffmpeg
        # with ONE SIGINT. A second SIGINT makes ffmpeg quit without finishing its files.
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw)


def _drain(pipe):
    try:
        while pipe.read(65536):
            pass
    except (OSError, ValueError):
        pass


def stop_ffmpeg(p, grace=3):
    """Stop ffmpeg politely so it sends RTSP TEARDOWN and the camera frees the session.
    A hard kill leaves the session open on the camera until it times out, and Wyze cams
    refuse new viewers once too many dead sessions pile up.
    Linux/macOS: SIGINT. Windows: has no SIGINT for child processes, so send ffmpeg's own
    'q' (quit) key on its stdin. Either way, kill it if it hasn't exited after `grace` s."""
    if p is None or p.poll() is not None:
        return
    if getattr(p, "_stop_sent", False):        # already asked (another thread): just wait
        try:
            p.wait(timeout=grace + 2)
        except subprocess.TimeoutExpired:
            pass
        return
    p._stop_sent = True
    try:
        if IS_WIN:
            if p.stdin:
                p.stdin.write(b"q")
                p.stdin.flush()
        else:
            p.send_signal(signal.SIGINT)
        p.wait(timeout=grace)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        p.kill()
        p.wait()


def need(tool):
    if not shutil.which(tool):
        pkg = "ffmpeg" if tool == "ffprobe" else tool
        how = {"windows": f"winget install -e --id {'Gyan.FFmpeg' if pkg == 'ffmpeg' else 'shinchiro.mpv'}"
                          f"  (or: scoop install {pkg})",
               "mac": f"brew install {pkg}",
               "linux": f"sudo apt install {pkg}"}[_OS]
        die(f"'{tool}' not found — install it:  {how}")


def local_ip():
    """This computer's LAN address, found the portable way: 'connect' a UDP socket toward a
    public address (nothing is actually sent) and see which local address the OS picked."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
    finally:
        s.close()


def detect_subnet():
    """Subnet to scan, e.g. 192.168.1.0/24. Linux reads the real netmask from `ip`; macOS and
    Windows (and Linux as a fallback) assume the usual home-network /24 around our own IP."""
    if not IS_LINUX:
        try:
            return ipaddress.ip_interface(f"{local_ip()}/24").network
        except OSError:
            die("couldn't detect your network — pass --subnet 192.168.1.0/24")
    try:
        out = subprocess.run(["ip", "-4", "route", "show", "default"],
                             capture_output=True, text=True, check=True).stdout
        dev = re.search(r"\bdev (\S+)", out).group(1)
        addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", dev],
                              capture_output=True, text=True, check=True).stdout
        cidr = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", addr).group(1)
        net = ipaddress.ip_interface(cidr).network
        if net.prefixlen < 22:  # don't sweep a /16 by accident
            net = ipaddress.ip_interface(f"{cidr.split('/')[0]}/24").network
        return net
    except Exception:
        try:
            return ipaddress.ip_interface(f"{local_ip()}/24").network
        except OSError:
            die("couldn't detect your subnet — pass --subnet 192.168.1.0/24")


def norm_mac(m):
    """'a:b:c:d:e:f' (macOS drops leading zeros) or 'aa-bb-…' (Windows) -> 'AA:BB:CC:DD:EE:FF'."""
    parts = re.split(r"[:-]", m.strip())
    if len(parts) != 6:
        return ""
    try:
        return ":".join(f"{int(x, 16):02X}" for x in parts)
    except ValueError:
        return ""


def parse_arp_a(text):
    """IP -> MAC from `arp -a` output (macOS: '? (192.168.1.5) at a:b:c:d:e:f on en0 …';
    Windows: '  192.168.1.5           aa-bb-cc-dd-ee-ff     dynamic')."""
    macs = {}
    for line in text.splitlines():
        ip = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
        mac = re.search(r"\b([0-9A-Fa-f]{1,2}(?:[:-][0-9A-Fa-f]{1,2}){5})\b", line)
        if ip and mac and not 224 <= int(ip.group(1).split(".")[0]) <= 239:   # skip multicast
            m = norm_mac(mac.group(1))
            if m and m not in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                macs[ip.group(1)] = m
    return macs


def arp_table():
    """IP -> MAC for devices we just talked to (the scan fills the OS's ARP cache).
    Used only to keep camera names attached if a camera's IP address changes."""
    if IS_LINUX:
        macs = {}
        try:
            with open("/proc/net/arp") as f:
                next(f)
                for line in f:
                    p = line.split()
                    if len(p) >= 4 and p[3] != "00:00:00:00:00:00":
                        macs[p[0]] = p[3].upper()
        except OSError:
            pass
        return macs
    try:
        r = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10)
        return parse_arp_a(r.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return {}


def rtsp_url(ip, port, user, password, path):
    return f"rtsps://{quote(user, safe='')}:{quote(password, safe='')}@{ip}:{port}/{path}"


def redact(url):
    return re.sub(r"//[^@/]+@", "//***@", url)


# --------------------------------------------------------------------------- scan
def stop_soon(stop):
    """Set `stop` from a signal handler. Not directly: the handler runs in the main thread, and
    if that thread is inside stop.wait() (or a second Ctrl+C lands inside the first handler),
    Event.set() would wait forever for a lock the main thread already holds."""
    threading.Thread(target=stop.set, daemon=True).start()


def safe_name(name):
    """A camera name usable as a folder/file name on Linux, macOS and Windows (and inside
    ffmpeg's strftime pattern, so no '%')."""
    s = re.sub(r'[<>:"/\\|?*%\x00-\x1f]+', "_", name).strip(" .")
    return s or "camera"


def port_open(ip, port, timeout=0.6):
    try:
        with socket.create_connection((str(ip), port), timeout=timeout):
            return True
    except OSError:
        return False


def rtsps_options(ip, port, timeout=3.0):
    """TLS-connect and send an unauthenticated RTSP OPTIONS. Returns the server's
    first response line + Server header, or None if it doesn't speak RTSPS."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Wyze cams use a self-signed cert
    try:
        with socket.create_connection((str(ip), port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=str(ip)) as s:
                s.sendall(f"OPTIONS rtsps://{ip}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n"
                          f"User-Agent: wyzecam-rtsp\r\n\r\n".encode())
                data = b""
                while b"\r\n\r\n" not in data and len(data) < 4096:
                    chunk = s.recv(1024)
                    if not chunk:
                        break
                    data += chunk
    except (OSError, ssl.SSLError):
        return None
    text = data.decode(errors="replace")
    if not text.startswith("RTSP/"):
        return None
    server = re.search(r"^Server:\s*(.+)$", text, re.M | re.I)
    return {"status": text.splitlines()[0].strip(),
            "server": server.group(1).strip() if server else ""}


def ffprobe(url, timeout_s=8):
    """Open the stream with credentials; return video/audio info or an error string."""
    cmd = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
           "-timeout", str(timeout_s * 1_000_000),
           "-show_entries", "stream=codec_type,codec_name,width,height:format_tags=title",
           "-of", "json", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 4)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    if r.returncode != 0:
        err = r.stderr.strip().splitlines()
        msg = err[-1] if err else f"ffprobe exit {r.returncode}"
        if "401" in msg or "Unauthorized" in msg:
            msg = "401 Unauthorized (wrong username/password?)"
        return None, redact(msg)
    try:
        j = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None, "unreadable ffprobe output"
    info = {"title": j.get("format", {}).get("tags", {}).get("title", "")}
    for s in j.get("streams", []):
        if s.get("codec_type") == "video" and "video" not in info:
            info["video"] = f"{s.get('codec_name')} {s.get('width')}x{s.get('height')}"
            info["width"], info["height"] = s.get("width"), s.get("height")
        elif s.get("codec_type") == "audio" and "audio" not in info:
            info["audio"] = s.get("codec_name")
    return info, None


def guess_model(info):
    w = (info or {}).get("width") or 0
    if w >= 2560:
        return "Cam v4 (2.5K)"
    if w >= 1920:
        return "Cam v3 / Pan v3 (1080p)"
    return "?"


def load_saved():
    try:
        return json.loads(CONF_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"cameras": []}


def save(cfg):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONF_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CONF_FILE)


# Names you already know, by IP. Used when nothing better is found. Edit freely.
KNOWN_NAMES = {
    "192.168.1.243": "Front Door",
}
GENERIC_NAME = re.compile(r"^(cam(era)?\s*\.?\d+|cam\d+)$", re.I)


def is_generic(name):
    return not name or bool(GENERIC_NAME.match(name.strip()))


def wyze_nicknames():
    """Best effort: find the Wyze app nicknames in CouchCam's saved data (the Wyze
    device list carries nickname + MAC + IP). Returns {mac_or_ip: nickname}."""
    found = {}
    roots = [Path.home() / ".couchcam", Path.home() / ".local/share/couchcam-server",
             Path.home() / "Downloads/couchcam"]

    def walk(o):
        if isinstance(o, dict):
            nick = o.get("nickname") or o.get("nick_name")
            if isinstance(nick, str) and nick.strip():
                mac = o.get("mac") or o.get("mac_address")
                ip = o.get("ip") or (o.get("device_params") or {}).get("ip")
                if isinstance(mac, str) and mac:
                    m = mac.replace("-", "").replace(":", "").upper()
                    found[":".join(m[i:i + 2] for i in range(0, 12, 2))] = nick.strip()
                if isinstance(ip, str) and ip:
                    found[ip] = nick.strip()
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for root in roots:
        if not root.is_dir():
            continue
        for f in list(root.rglob("*.json"))[:200]:
            try:
                if f.stat().st_size < 5_000_000:
                    walk(json.loads(f.read_text(errors="replace")))
            except (OSError, json.JSONDecodeError, RecursionError):
                continue
    return found


def pick_name(ip, mac, prev_name, nicks):
    """Your own name wins; then the Wyze app nickname; then KNOWN_NAMES; then a placeholder."""
    if prev_name and not is_generic(prev_name):
        return prev_name
    return (nicks.get(mac) or nicks.get(ip) or KNOWN_NAMES.get(ip)
            or prev_name or f"Camera {ip.rsplit('.', 1)[1]}")


def scan(args, password):
    need("ffprobe")
    net = ipaddress.ip_network(args.subnet, strict=False) if args.subnet else detect_subnet()
    hosts = list(net.hosts())
    print(f"Scanning {net} ({len(hosts)} hosts) for RTSPS on port {args.port} ...")

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=128) as ex:
        open_hosts = [ip for ip, ok in zip(hosts, ex.map(lambda h: port_open(h, args.port), hosts)) if ok]
    print(f"  {len(open_hosts)} host(s) with port {args.port} open  ({time.time() - t0:.1f}s)")

    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        rtsp = {ip: r for ip, r in zip(open_hosts, ex.map(lambda h: rtsps_options(h, args.port), open_hosts)) if r}
    if not rtsp:
        print("No RTSPS servers found. Is RTSP turned on in the Wyze app for your cameras?")
        if IS_MAC:
            print("On macOS, also check System Settings > Privacy & Security > Local Network:\n"
                  "your terminal app must be allowed, or every camera looks unreachable.")
        if IS_WIN:
            print("On Windows, if Windows Firewall asked about Python, it must be allowed on "
                  "Private networks.")
        return []

    print(f"  {len(rtsp)} RTSPS server(s); checking streams with your login ...")
    def probe(ip):
        return ip, ffprobe(rtsp_url(ip, args.port, args.user, password, MAIN))
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        probed = dict(ex.map(probe, rtsp))
    failed = [ip for ip, (info, err) in probed.items() if err]
    if failed:
        print(f"  retrying {len(failed)} camera(s) one at a time ...")
        for ip in failed:
            probed[ip] = ffprobe(rtsp_url(ip, args.port, args.user, password, MAIN), timeout_s=15)

    macs = arp_table()
    nicks = wyze_nicknames()
    if nicks:
        print(f"  found {len(nicks)} Wyze app name(s) in CouchCam's data")
    old_list = load_saved().get("cameras", [])
    old = {c["ip"]: c for c in old_list}
    old_by_mac = {c.get("mac"): c for c in old_list if c.get("mac")}
    cams, matched = [], []
    for ip in sorted(rtsp, key=lambda a: int(a)):
        ip_s = str(ip)
        info, err = probed[ip]
        mac = macs.get(ip_s, "")
        prev = old.get(ip_s) or old_by_mac.get(mac) or {}   # keep names if the IP moved
        if prev:
            matched.append(id(prev))
        cams.append({
            "_pos": old_list.index(prev) if prev else len(old_list) + int(ip),   # keep your order
            "name": pick_name(ip_s, mac, prev.get("name"), nicks),
            "ip": ip_s, "port": args.port, "mac": mac,
            "model_guess": guess_model(info),
            "video": (info or {}).get("video", ""),
            "audio": (info or {}).get("audio", ""),
            "server": rtsp[ip]["server"] or (info or {}).get("title", ""),
            "ok": err is None, "error": err or "",
            "enabled": prev.get("enabled", True),
            **{k: prev[k] for k in ("stream", "hwaccel") if k in prev},   # keep tuned settings
        })
    # Cameras you had before that didn't answer this time stay in the list (in place), so a
    # camera that's briefly offline doesn't lose its name, order or settings.
    for pos, c in enumerate(old_list):
        if id(c) not in matched:
            cams.append({**c, "_pos": pos, "ok": False, "error": "not found in the last scan"})
    cams.sort(key=lambda c: c.pop("_pos"))

    print()
    print(f"{'NAME':<14} {'IP':<15} {'MAC':<17} {'MODEL (guess)':<24} {'VIDEO':<20} STATUS")
    for c in cams:
        status = "OK" if c["ok"] else f"FAIL: {c['error']}"
        print(f"{c['name']:<14} {c['ip']:<15} {c.get('mac', ''):<17} {c.get('model_guess', ''):<24} "
              f"{c.get('video', ''):<20} {status}")

    cfg = {"user": args.user, "port": args.port, "subnet": str(net),
           "scanned": time.strftime("%Y-%m-%d %H:%M"), "cameras": cams}
    save(cfg)
    print(f"\nSaved to {CONF_FILE}  (edit 'name' / 'enabled' there; password is not stored)")
    unnamed = [c for c in cams if is_generic(c["name"])]
    if unnamed:
        print(f"{len(unnamed)} camera(s) still have placeholder names. To name them while watching each one:\n"
              f"  {RUN} name")
    return cams


# --------------------------------------------------------------------------- play
COMMON = ["--no-audio", "--profile=low-latency", "--untimed", "--hwdec=auto",
          "--network-timeout=10", "--really-quiet", "--keep-open=no"]


def play_windows(cams, user, password, path):
    """One mpv window per camera (the compositor tiles them), each auto-reconnecting."""
    conf = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    conf.write("q quit 9\n")  # q = close this camera for good
    conf.close()
    stop = threading.Event()
    procs = {}

    def run(cam):
        url = rtsp_url(cam["ip"], cam["port"], user, password, cam.get("stream") or path)
        delay = 2
        while not stop.is_set():
            started = time.time()
            p = subprocess.Popen(["mpv", *COMMON, f"--input-conf={conf.name}",
                                  f"--title={cam['name']}", "--force-window=immediate", url])
            procs[cam["ip"]] = p
            rc = p.wait()
            if rc == 9 or stop.is_set():
                break
            if time.time() - started > 60:
                delay = 2
            print(f"[{cam['name']}] stream dropped — reconnecting in {delay}s")
            stop.wait(delay)
            delay = min(delay * 2, 30)

    threads = [threading.Thread(target=run, args=(c,), daemon=True) for c in cams]
    for t in threads:
        t.start()
    print(f"Playing {len(cams)} camera(s). Press q in a window to close it; Ctrl+C here to close all.")
    if IS_LINUX:
        print("COSMIC: press Super+Y if the windows aren't tiled.")

    def shutdown(*_):
        stop_soon(stop)
        for p in procs.values():
            if p.poll() is None:
                p.terminate()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    finally:
        shutdown()
        os.unlink(conf.name)


def input_args(url, hwaccel=True, gpu_frames=False, wallclock=False, progress=False):
    """ffmpeg input options shared by the grid and the 'test' command.
    gpu_frames keeps decoded frames on the GPU (for scaling there).
    wallclock stamps packets with the arrival time (Wyze streams have no usable timestamps,
    which recordings need). progress reports frame counts on stdout (headless liveness)."""
    a = ["-hide_banner"]
    if not IS_WIN:
        a.append("-nostdin")       # Windows keeps stdin: that's how we ask ffmpeg to quit ('q')
    if progress:
        a += ["-nostats", "-progress", "pipe:1"]
    if wallclock:
        a += ["-use_wallclock_as_timestamps", "1"]
    if hwaccel:
        # GPU decoding: VA-API on Linux (Intel/AMD); "auto" picks VideoToolbox on macOS and
        # D3D11VA/DXVA2 on Windows. All fall back to the CPU if the GPU can't do it.
        a += ["-hwaccel", "vaapi" if IS_LINUX else "auto"]
        if gpu_frames and IS_LINUX:
            a += ["-hwaccel_output_format", "vaapi"]
    a += ["-rtsp_transport", "tcp", "-timeout", "10000000",
          "-fflags", "nobuffer+discardcorrupt",    # drop packets the camera flagged as broken
          "-flags", "low_delay", "-i", url]
    return a


def log(msg):
    """Timestamped message to the terminal and to wyzecam-rtsp.log in the app folder."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with open(CONF_DIR / "wyzecam-rtsp.log", "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def drawtext_escape(t):
    return t.replace("\\", "\\\\").replace("'", "’").replace(":", "\\:").replace("%", "\\%")


def camera_ffmpeg_cmd(url, tile_w, tile_h, hwaccel=True, keep_every=2, gpu_scale=False,
                      tiles=True, rec_pattern=None, rec_segment=300, jpeg_port=None,
                      rec_format="mkv", hevc=False, det_port=None, det_every=10):
    """One ffmpeg per camera, i.e. ONE camera session, with up to four outputs:
      tiles       decode, keep every Nth frame (counted, since Wyze streams have no usable
                  timestamps), scale to the tile, raw YUV420 frames on stdout (the grid)
      rec_pattern copy the video as-is (no re-encoding) into clock-aligned .mkv/.mp4 segments
      jpeg_port   JPEG snapshots for the browser view, sent to a local TCP port
      det_port    small RGB frames (every Nth) for object detection, to a local TCP port
    gpu_scale: decode AND shrink on the GPU (VA-API), copy only the small tile back."""
    gpu = (hwaccel and gpu_scale and IS_LINUX and jpeg_port is None
           and det_port is None)                                    # VA-API (Linux) only
    cmd = ["ffmpeg", "-loglevel", "error" if not VERBOSE else "warning",
           *input_args(url, hwaccel, gpu, wallclock=rec_pattern is not None, progress=not tiles)]
    if tiles:
        vf = []
        if keep_every > 1:
            vf.append(f"select='not(mod(n\\,{keep_every}))'")
        if rec_pattern:                # recording adds arrival-time stamps; renumber the tiles
            vf.append("settb=1/1000,setpts=N*50")
        if gpu:
            vf += [f"scale_vaapi=w={tile_w}:h={tile_h}:format=nv12", "hwdownload", "format=nv12"]
            # -pix_fmt yuv420p below converts the small tile on the CPU (cheap at tile size)
        else:
            vf.append(f"scale={tile_w}:{tile_h}")
        cmd += ["-map", "0:v:0", "-an", "-vf", ",".join(vf), "-fps_mode", "passthrough",
                "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"]
    if rec_pattern:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
                "-f", "segment", "-segment_time", str(rec_segment), "-segment_atclocktime", "1",
                "-reset_timestamps", "1", "-strftime", "1"]
        if rec_format == "mp4":
            # Fragmented MP4: written in small self-contained pieces, so a crash or power cut
            # loses only the last moment (a plain MP4 is unplayable until it's finished).
            cmd += ["-segment_format", "mp4", "-segment_format_options",
                    "movflags=+frag_keyframe+empty_moov+default_base_moof"]
            if hevc:
                cmd += ["-tag:v", "hvc1"]          # lets Apple devices play H.265 in MP4
        else:
            cmd += ["-segment_format", "matroska"]
        cmd.append(rec_pattern)
    if jpeg_port:
        cmd += ["-map", "0:v:0", "-an",
                # fresh, strictly increasing timestamps: the JPEG encoder rejects repeats
                "-vf", f"select='not(mod(n\\,{WEB_EVERY}))',settb=1/1000,setpts=N*100,"
                       f"scale='min({WEB_WIDTH},iw)':-2",
                "-fps_mode", "passthrough", "-pix_fmt", "yuvj420p", "-c:v", "mjpeg",
                "-q:v", str(WEB_QUALITY), "-f", "image2pipe", f"tcp://127.0.0.1:{jpeg_port}"]
    if det_port:
        cmd += ["-map", "0:v:0", "-an",
                "-vf", f"select='not(mod(n\\,{det_every}))',settb=1/1000,setpts=N*100,"
                       f"scale={DET_W}:{DET_H}",
                "-fps_mode", "passthrough", "-pix_fmt", "rgb24", "-f", "rawvideo",
                f"tcp://127.0.0.1:{det_port}"]
    if not tiles and not rec_pattern and not jpeg_port and not det_port:
        cmd += ["-map", "0:v:0", "-c", "copy", "-f", "null", "-"]
    return cmd


def drop_reason(lines):
    """Turn ffmpeg's last error lines into a short, plain reason."""
    text = " ".join(lines[-12:])
    for pat, why in (("Connection reset", "camera reset the connection"),
                     ("timed out", "connection timed out"),
                     ("End of file", "camera closed the connection"),
                     ("Connection refused", "camera refused the connection (RTSP off?)"),
                     ("401", "login rejected (username/password)"),
                     ("Invalid data found", "camera refused the session"),
                     ("No route to host", "camera offline (no route to host)"),
                     ("Nothing was written", "no video received")):
        if pat in text:
            return why
    return "stream ended"


FALLBACK_DROPS, FALLBACK_WINDOW = 3, 300   # 3 drops in 5 min on the main stream -> substream


class CameraFeed(threading.Thread):
    """Keeps the newest frame from one camera, reconnects on its own if it drops,
    can be told to resync (reconnect) to get back to live, and falls back to the
    lighter substream + CPU decoding if the main stream keeps dropping."""
    def __init__(self, cam, user, password, path, tile_w, tile_h, stop, keep_every, cfg_cam=None,
                 gpu_scale=False, tiles=True, rec_root=None, rec_segment=300, web=False,
                 rec_format="mkv", detect=False, det_every=10):
        super().__init__(daemon=True)
        self.detect = detect       # True = also send small frames to the object detector
        self.det_every = det_every
        self.det_frame = None      # newest DET_W x DET_H RGB frame (bytes) and its number
        self.det_seq = 0
        self.dets = []             # objects currently tracked: [(label, score, x1, y1, x2, y2)]
                                   # with coordinates as fractions of the frame (0..1)
        self.dets_ver = 0          # bumped whenever dets changes (so the grid repaints)
        self.tiles = tiles         # False = headless (no grid): no raw frames are decoded for us
        self.rec_root = rec_root   # Path: record into rec_root/<camera name>/
        self.rec_segment = rec_segment
        self.rec_format = rec_format
        self.web = web             # True = also make JPEG snapshots for the browser view
        self.jpeg = None           # newest JPEG (bytes) and its sequence number
        self.jpeg_seq = 0
        self.jpeg_time = 0.0
        self.jpeg_cond = threading.Condition()
        self.cam, self.user, self.password = cam, user, password
        self.path = cam.get("stream") or path
        self.w, self.h, self.stop = tile_w, tile_h, stop
        self.name = cam["name"]
        self.hwaccel = cam.get("hwaccel", True)
        self.keep_every = keep_every
        self.gpu_scale = gpu_scale and self.hwaccel
        self.cfg_cam = cfg_cam     # entry in cameras.json to update on fallback
        self.frame = None          # bytes, one YUV420 tile
        self.last = 0.0            # time of newest frame
        self.started = 0.0         # time the current ffmpeg was launched
        self.proc = None
        self.planned = False       # True when we restarted it on purpose (resync)
        self.drops = 0             # unplanned disconnects (shown in the 10-min status line)
        self.drop_times = []       # recent drops, for the substream fallback decision
        self.frames = 0            # frames received; also a sequence number for the compositor
        self.save_needed = False   # set when a fallback changed settings that should be saved
        self.paused = False        # hidden in the grid: stay disconnected until shown again

    def url(self):
        return rtsp_url(self.cam["ip"], self.cam["port"], self.user, self.password, self.path)

    def rec_pattern(self):
        """Segment file name pattern for this session (folder created here; ffmpeg won't)."""
        if not self.rec_root:
            return None
        folder = Path(self.rec_root) / safe_name(self.name)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(f"[{self.name}] can't create recording folder {folder}: {e} — not recording")
            return None
        return str(folder / f"{safe_name(self.name)}_%Y-%m-%d_%H-%M-%S.{self.rec_format}")

    def _jpeg_reader(self, listener):
        """Receive ffmpeg's JPEG stream on a local socket; keep only the newest picture.
        JPEGs are split on their start/end markers (0xFFD8 / 0xFFD9 never occur inside the
        compressed data, which escapes 0xFF bytes)."""
        try:
            listener.settimeout(FIRST_FRAME_SECS + 15)
            conn, _ = listener.accept()
        except OSError:
            return
        finally:
            listener.close()
        buf = b""
        try:
            conn.settimeout(None)
            while True:                    # until ffmpeg closes it (closing first = broken pipe)
                chunk = conn.recv(262144)
                if not chunk:
                    break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    if s < 0:
                        buf = b""
                        break
                    e = buf.find(b"\xff\xd9", s + 2)
                    if e < 0:
                        buf = buf[s:]
                        break
                    with self.jpeg_cond:
                        self.jpeg = buf[s:e + 2]
                        self.jpeg_seq += 1
                        self.jpeg_time = time.time()
                        self.jpeg_cond.notify_all()
                    buf = buf[e + 2:]
        except OSError:
            pass
        finally:
            conn.close()

    @staticmethod
    def _open_tap(reader, taps):
        """A local TCP port that one of ffmpeg's extra outputs connects to, served by `reader`."""
        lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lst.bind(("127.0.0.1", 0))
        lst.listen(1)
        th = threading.Thread(target=reader, args=(lst,), daemon=True)
        th.start()
        taps.append((lst, th))
        return lst

    def _det_reader(self, listener):
        """Receive ffmpeg's small RGB frames for the detector; keep only the newest."""
        size = DET_W * DET_H * 3
        try:
            listener.settimeout(FIRST_FRAME_SECS + 15)
            conn, _ = listener.accept()
        except OSError:
            return
        finally:
            listener.close()
        buf = bytearray(size)
        mv = memoryview(buf)
        try:
            conn.settimeout(None)
            while True:
                n = 0
                while n < size:
                    r = conn.recv_into(mv[n:])
                    if not r:
                        return
                    n += r
                self.det_frame = bytes(buf)
                self.det_seq += 1
        except OSError:
            pass
        finally:
            conn.close()

    def _progress_reader(self, out, first_frame):
        """Headless: ffmpeg's -progress lines tell us video is still arriving. 'frame=' counts
        only re-encoded video (the browser view's JPEGs); when recording alone, everything is
        copied and only 'out_time_us=' moves, so either one counts as progress."""
        last = {"frame": 0, "out_time_us": 0}
        for raw in out:
            line = raw.decode(errors="replace").strip()
            key, _, val = line.partition("=")
            if key in last:
                try:
                    n = int(val)
                except ValueError:
                    continue
                if n > last[key]:
                    self.frames += (n - last[key]) if key == "frame" else 1
                    last[key] = n
                    self.last = time.time()
                    if first_frame[0] is None:
                        first_frame[0] = self.last
            if self.stop.is_set():
                break

    def _stderr_reader(self, pipe, sink):
        """Collect ffmpeg's messages for the log file instead of flooding the terminal."""
        try:
            with open(CONF_DIR / "wyzecam-rtsp-ffmpeg.log", "a") as fh:
                for raw in pipe:
                    line = redact(raw.decode(errors="replace").rstrip())
                    if line:
                        sink.append(line)
                        del sink[:-30]
                        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{self.name}] {line}\n")
                        fh.flush()
        except (OSError, ValueError):
            pass

    def _maybe_fallback(self, had_video):
        """Only streams that connect and then die count — an offline camera won't be helped
        by the substream, so it keeps its settings."""
        if not had_video:
            return False
        now = time.time()
        self.drop_times = [t for t in self.drop_times if now - t < FALLBACK_WINDOW] + [now]
        if self.path == MAIN and len(self.drop_times) >= FALLBACK_DROPS:
            self.path, self.hwaccel, self.gpu_scale = SUB, False, False
            self.drop_times = []
            log(f"[{self.name}] main stream unstable ({FALLBACK_DROPS} drops in "
                f"{FALLBACK_WINDOW // 60} min) — switching to the lighter substream + CPU decoding")
            if self.cfg_cam is not None:
                self.cfg_cam["stream"], self.cfg_cam["hwaccel"] = SUB, False
                return True    # tell the grid to save cameras.json
        return False

    def run(self):
        size = self.w * self.h * 3 // 2
        buf = bytearray(size)
        mv = memoryview(buf)
        delay = 2          # back-off when a camera can't connect / sends nothing: 2,4,8,16,30 s
        quick = 2          # back-off when it WAS streaming before it dropped: 2,4,8,8 s
        while not self.stop.is_set():
            if self.paused:                            # hidden: don't hold a camera session
                self.stop.wait(0.5)
                continue
            self.planned = False
            started = self.started = time.time()
            errlines = []
            frames_before = self.frames
            ff = [None]                    # time of this session's first frame
            taps = []                      # local ports for ffmpeg's JPEG / detection outputs
            listener = self._open_tap(self._jpeg_reader, taps) if self.web else None
            dlistener = self._open_tap(self._det_reader, taps) if self.detect else None
            self.proc = popen_ffmpeg(
                camera_ffmpeg_cmd(self.url(), self.w, self.h, self.hwaccel, self.keep_every,
                                  self.gpu_scale, tiles=self.tiles, rec_pattern=self.rec_pattern(),
                                  rec_segment=self.rec_segment, rec_format=self.rec_format,
                                  hevc=str(self.cam.get("video", "")).lower().startswith(("hevc", "h265")),
                                  jpeg_port=listener.getsockname()[1] if listener else None,
                                  det_port=dlistener.getsockname()[1] if dlistener else None,
                                  det_every=self.det_every),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            et = threading.Thread(target=self._stderr_reader, args=(self.proc.stderr, errlines), daemon=True)
            et.start()
            out = self.proc.stdout
            try:
                if not self.tiles:                     # headless: just follow the progress
                    self._progress_reader(out, ff)
                while self.tiles and not self.stop.is_set():
                    n = 0
                    while n < size:                    # read straight into a reused buffer
                        r = out.readinto(mv[n:])
                        if not r:
                            raise EOFError
                        n += r
                    self.frame = bytes(buf)
                    self.last = time.time()
                    if ff[0] is None:
                        ff[0] = self.last
                    self.frames += 1
            except (EOFError, OSError, ValueError):
                pass
            finally:
                # keep reading while ffmpeg shuts down, so it can't block on a full pipe
                # before it finishes the current recording file
                threading.Thread(target=_drain, args=(out,), daemon=True).start()
                stop_ffmpeg(self.proc)
                et.join(timeout=2)
                for lst, th in taps:
                    try:
                        lst.close()            # unblocks accept() if ffmpeg never connected
                    except OSError:
                        pass
                    th.join(timeout=2)
            first_frame = ff[0]
            if self.stop.is_set():
                break
            if self.planned:
                delay = quick = 2
                continue                               # resync: reconnect right away
            got_video = self.frames > frames_before
            if self.gpu_scale and not got_video and any(
                    k in " ".join(errlines) for k in ("vaapi", "VAAPI", "hwdownload", "hw_frames",
                                                      "Impossible to convert", "No VA display",
                                                      "Device creation failed", "filtergraph",
                                                      "Option not found", "Failed to",
                                                      "Error reinitializing filters")):
                self.gpu_scale = False
                log(f"[{self.name}] GPU scaling not available here — using the standard path")
                delay = 2
                continue
            self.drops += 1
            ran = time.time() - started
            # "Had video" = this session streamed for at least 3 s before it dropped.
            had_video = first_frame is not None and self.last - first_frame >= 3
            if self._maybe_fallback(got_video):
                self.save_needed = True
                delay = quick = 2
            if ran > 60:                               # it was healthy for a while: start over
                delay = quick = 2
            if had_video and ran <= 60:
                # It works but keeps dropping (weak Wi-Fi): retry fast so the tile isn't grey
                # for long. 2, 4, 8, 8 ... s
                wait = quick
                quick = min(quick * 2, 8)
                delay = 2                              # a later failed connect backs off from 2 s
            else:
                # Couldn't connect or sent nothing: back off so we don't hammer the camera.
                # 2, 4, 8, 16, 30, 30 ... s
                wait = delay
                delay = min(delay * 2, 30)
            log(f"[{self.name}] dropped: {drop_reason(errlines)} — reconnecting in {wait}s")
            self.stop.wait(wait)

    def restart(self, planned=False):
        """Reconnect this camera (from another thread, so the grid never waits)."""
        self.planned = planned
        self.started = time.time()          # don't let the watchdog re-trigger meanwhile
        threading.Thread(target=stop_ffmpeg, args=(self.proc,), daemon=True).start()


LABEL_SHADE = 0.45      # how much video shows through the name box (0 = solid black, 1 = clear)
LABEL_TEXT_MIN = 60     # brightness above which a rendered pixel counts as part of the text


def label_font():
    """drawtext font option. Linux finds fonts through fontconfig; macOS and Windows ffmpeg
    builds usually need an explicit font file."""
    candidates = {"mac": ["/System/Library/Fonts/Supplemental/Arial.ttf",
                          "/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf"],
                  "windows": [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", f)
                              for f in ("segoeui.ttf", "arial.ttf")]}.get(_OS, [])
    for f in candidates:
        if os.path.exists(f):
            # drawtext needs forward slashes and an escaped drive colon: C\:/Windows/Fonts/arial.ttf
            return "fontfile='" + f.replace("\\", "/").replace(":", "\\:") + "':"
    return ""


def render_label(text, max_w, font_px):
    """Render a camera name once with ffmpeg (white text on black), and pre-compute, for each
    row, the spans of text pixels. Painting then only has to darken the box area of the live
    video (translucent background) and copy the text spans on top — cheap enough to do on
    every frame. Returns (w, h, luma_bytes, spans_per_row) or None if ffmpeg can't draw text."""
    lh = (int(font_px * 1.7) + 1) // 2 * 2
    lw = min(max_w, (int(len(text) * font_px * 0.62) + 24 + 1) // 2 * 2)
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={lw}x{lh}",
           "-vf", f"drawtext={label_font()}text='{drawtext_escape(text)}':fontcolor=white:fontsize={font_px}:"
                  "x=12:y=(h-text_h)/2",
           "-frames:v", "1", "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or len(r.stdout) != lw * lh * 3 // 2:
        return None
    luma = r.stdout[:lw * lh]                      # text is white, so only brightness matters
    spans = []
    for row in range(lh):
        line, runs, x = luma[row * lw:(row + 1) * lw], [], 0
        while x < lw:
            if line[x] > LABEL_TEXT_MIN:
                start = x
                while x < lw and line[x] > LABEL_TEXT_MIN:
                    x += 1
                runs.append((start, x))
            x += 1
        spans.append(runs)
    return lw, lh, luma, spans


def ipc_path(tmpdir):
    """Where mpv listens for commands: a Unix socket file, or a named pipe on Windows."""
    if IS_WIN:
        return rf"\\.\pipe\wyzecam-rtsp-{os.getpid()}"
    return os.path.join(tmpdir, "mpv.sock")


class MpvIPC:
    """Talk to the grid's mpv: receive key presses (script-message) and show on-screen text.
    Linux/macOS use one Unix-socket connection for both directions. On Windows mpv uses a
    named pipe; a blocking read and a write can't share one pipe handle there, so events
    are read on one long-lived connection and each command opens a short-lived one."""
    def __init__(self, sock_path, on_message, stop):
        self.sock_path, self.on_message, self.stop = sock_path, on_message, stop
        self.sock = None
        self.ready = False
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _open_pipe(self, mode):
        return open(self.sock_path, mode, buffering=0)

    def _run(self):
        deadline = time.time() + 15
        reader = None
        while not self.stop.is_set():                 # wait for mpv to create its endpoint
            try:
                if IS_WIN:
                    reader = self._open_pipe("rb")
                else:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.connect(self.sock_path)
                    self.sock, reader = s, s.makefile("rb")
                break
            except OSError:
                if time.time() > deadline:
                    return
                time.sleep(0.2)
        if reader is None:
            return
        self.ready = True
        try:
            self.on_message(["connected"])
            for raw in reader:
                line = raw.decode(errors="replace")
                if self.stop.is_set():
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") == "client-message":
                    self.on_message(ev.get("args", []))
        except OSError:
            pass

    def command(self, *args):
        """Send any mpv command, e.g. command("vf", "remove", "@zoom")."""
        if not self.ready:
            return
        data = (json.dumps({"command": list(args)}) + "\n").encode()
        try:
            with self.lock:
                if IS_WIN:
                    with self._open_pipe("wb") as w:
                        w.write(data)
                else:
                    self.sock.sendall(data)
        except OSError:
            pass

    def show(self, text, ms=4000):
        self.command("show-text", text, ms)


KEY_HELP = ("Keys:  f = fullscreen   n = names on/off   r = resync all   h = help   q = quit\n"
            "Arrange live:  1-9 = pick a camera   arrows = move it   d = hide it   e = rename it\n"
            "               a = show all   z = zoom in/out   s = save this layout")


def dialog_cmd(prompt, current):
    """The command that pops up a one-line text box on this OS, or None if there isn't one."""
    if IS_MAC:
        esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
        return ["osascript", "-e",
                f'text returned of (display dialog "{esc(prompt)}" default answer "{esc(current)}" '
                f'with title "Rename camera")']
    if IS_WIN:
        q = lambda t: t.replace("'", "''")             # PowerShell single-quote escaping
        return ["powershell", "-NoProfile", "-Command",
                "Add-Type -AssemblyName Microsoft.VisualBasic; "
                f"[Microsoft.VisualBasic.Interaction]::InputBox('{q(prompt)}','Rename camera','{q(current)}')"]
    return None


def ask_name(current, ip, ipc):
    """Ask for a new camera name in a small desktop dialog: osascript on macOS, PowerShell on
    Windows, zenity/kdialog on Linux, else Python's tkinter; failing all of those, a prompt in
    the terminal. Returns the new name, or None if cancelled."""
    prompt = f"New name for the camera at {ip}:"
    try:
        cmd = dialog_cmd(prompt, current)
        if cmd and shutil.which(cmd[0]):
            r = subprocess.run(cmd, capture_output=True, text=True)
            return (r.stdout.strip() or None) if r.returncode == 0 else None
        if shutil.which("zenity"):
            r = subprocess.run(["zenity", "--entry", "--title=Rename camera", f"--text={prompt}",
                                f"--entry-text={current}"], capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else None
        if shutil.which("kdialog"):
            r = subprocess.run(["kdialog", "--title", "Rename camera", "--inputbox", prompt, current],
                               capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else None
    except OSError:
        pass
    try:                                          # Python's own GUI toolkit, if it's installed
        import tkinter
        from tkinter import simpledialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        ans = simpledialog.askstring("Rename camera", prompt, initialvalue=current, parent=root)
        root.destroy()
        return (ans or "").strip() or None
    except Exception:                             # no tkinter or no display: use the terminal
        pass
    ipc.show("Type the new name in the terminal window", 6000)
    try:
        return input(f"\n{prompt} [{current}] ").strip() or None
    except EOFError:
        return None


def watchdog_tick(feeds, next_resync, resync_s, now):
    for i, f in enumerate(feeds):
        p = f.proc
        # watchdog: reconnect a camera that stopped sending
        if p is not None and p.poll() is None and f.started:
            since = now - max(f.last, f.started)
            limit = STALL_SECS if f.last >= f.started else FIRST_FRAME_SECS
            if since > limit:
                log(f"[{f.name}] no new video for {since:.0f}s — reconnecting")
                f.restart()
        # scheduled resync keeps every camera close to live
        if now >= next_resync[i]:
            next_resync[i] = now + resync_s
            if f.last and now - f.last < STALL_SECS:     # only if it's currently healthy
                f.restart(planned=True)


def save_if_needed(feeds, cfg, cfg_by_ip):
    if any(f.save_needed for f in feeds):
        for f in feeds:
            f.save_needed = False
        if cfg_by_ip:
            save(cfg)
            log("saved the new stream settings to cameras.json")


def log_status(feeds, now):
    log("status: " + "; ".join(
        f"{f.name} {'LIVE' if f.last and now - f.last < STALL_SECS else 'DOWN'} "
        f"(drops {f.drops})" for f in feeds))


# --------------------------------------------------------------------------- object detection
# Local AI detection: no cloud, no subscription. Each camera's existing ffmpeg sends a few small
# frames a second; a cheap motion check decides whether the AI model needs to look at all; the
# model's detections are tracked so one person crossing the yard is ONE event, not hundreds.
# (The motion-first / track-objects approach follows Frigate NVR's design; no Frigate code or
# models are used.) Models: YOLOX by Megvii (Apache-2.0), trained on the COCO dataset.
YOLOX_URL = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/"
DET_MODELS = {   # name: (file, size in bytes, sha256)
    "nano": ("yolox_nano.onnx", 3659407,
             "c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"),
    "tiny": ("yolox_tiny.onnx", 20219662,
             "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7"),
    "s": ("yolox_s.onnx", 35858002,
          "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"),
}
COCO_LABELS = (
    "person bicycle car motorcycle airplane bus train truck boat traffic_light fire_hydrant "
    "stop_sign parking_meter bench bird cat dog horse sheep cow elephant bear zebra giraffe "
    "backpack umbrella handbag tie suitcase frisbee skis snowboard sports_ball kite baseball_bat "
    "baseball_glove skateboard surfboard tennis_racket bottle wine_glass cup fork knife spoon bowl "
    "banana apple sandwich orange broccoli carrot hot_dog pizza donut cake chair couch potted_plant "
    "bed dining_table tv laptop mouse remote keyboard cell_phone microwave oven toaster sink "
    "refrigerator book clock vase scissors teddy_bear hair_drier toothbrush").split()
LABEL_COLORS = {"person": "#ff4d4d", "dog": "#3cf05a", "cat": "#3cf05a", "bird": "#3cf05a",
                "car": "#3cb4ff", "truck": "#3cb4ff", "bicycle": "#3cb4ff",
                "motorcycle": "#3cb4ff", "package": "#ffb43c"}


def label_color(label):
    return LABEL_COLORS.get(label, "#ff5cf0")


def need_ai():
    try:
        import numpy
        import onnxruntime
    except ImportError:
        extra = " --break-system-packages" if IS_LINUX else ""
        die("object detection needs two Python packages (a one-time install):\n"
            f"       {'py -m pip' if IS_WIN else 'python3 -m pip'} install onnxruntime numpy{extra}")
    return numpy, onnxruntime


def get_model(spec):
    """Path of the detection model: 'nano' / 'tiny' / 's' are downloaded once (and checked)
    into the app folder; anything else is a path to your own ONNX model."""
    import hashlib
    import urllib.request
    if spec not in DET_MODELS:
        p = Path(spec).expanduser()
        if not p.is_file():
            die(f"detection model not found: {p}  (use nano, tiny, s, or a path to an .onnx file)")
        return p
    fname, size, sha = DET_MODELS[spec]
    p = APP_DIR / "models" / fname
    if p.is_file() and p.stat().st_size == size:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    url = YOLOX_URL + fname
    print(f"Downloading the {spec} detection model ({size / 1e6:.0f} MB, once) from\n  {url}")
    tmp = p.with_suffix(".part")
    try:
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as fh:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                h.update(chunk)
        if h.hexdigest() != sha:
            tmp.unlink()
            die("the downloaded model didn't match its checksum — try again later")
        tmp.replace(p)
    except OSError as e:
        die(f"couldn't download the model ({e}).\n       Download {url}\n"
            f"       yourself and put it at {p}")
    return p


def nms(np, boxes, scores, iou=0.45):
    """Non-maximum suppression: keep the best box among heavily overlapping ones."""
    x1, y1, x2, y2 = boxes.T
    area = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        rest = order[1:]
        w = np.maximum(0, np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]))
        h = np.maximum(0, np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]))
        inter = w * h
        order = rest[inter / (area[i] + area[rest] - inter + 1e-9) <= iou]
    return keep


class Detector:
    """One ONNX model shared by every camera. Understands two common output layouts:
      YOLOX            [1, N, 5 + classes]  (the bundled models)
      YOLOv8/v9 style  [1, 4 + classes, N]  (bring your own, e.g. one trained on packages)
    Class names: COCO, or <model>.txt next to your own model (one name per line)."""
    def __init__(self, model_path):
        np, ort = need_ai()
        self.np = np
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, min(4, os.cpu_count() or 2))
        so.log_severity_level = 3
        self.sess = ort.InferenceSession(str(model_path), so, providers=["CPUExecutionProvider"])
        inp = self.sess.get_inputs()[0]
        self.inp = inp.name
        self.S = inp.shape[2] if isinstance(inp.shape[2], int) else 640
        names = Path(model_path).with_suffix(".txt")
        self.labels = ([ln.strip() for ln in names.read_text().splitlines() if ln.strip()]
                       if names.is_file() else COCO_LABELS)
        shape = self.sess.get_outputs()[0].shape
        n = len(self.labels)
        self.yolox = not (len(shape) == 3 and shape[1] == 4 + n)
        S = self.S
        self.r = min(S / DET_H, S / DET_W)                    # letterbox scale
        self.nh, self.nw = int(DET_H * self.r), int(DET_W * self.r)
        self.yi = (np.arange(self.nh) / self.r).astype(int)  # nearest-neighbour resize indices
        self.xi = (np.arange(self.nw) / self.r).astype(int)
        if self.yolox:
            grids, strides = [], []
            for st in (8, 16, 32):
                g = S // st
                yv, xv = np.meshgrid(np.arange(g), np.arange(g), indexing="ij")
                grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
                strides.append(np.full((g * g, 1), st))
            self.grids, self.strides = np.concatenate(grids), np.concatenate(strides)

    def detect(self, rgb, min_score):
        """rgb: DET_H x DET_W x 3 array. Returns [(label, score, x1, y1, x2, y2)] with the box
        as fractions (0..1) of the frame."""
        np, S = self.np, self.S
        img = np.full((S, S, 3), 114, np.uint8)
        small = rgb[self.yi][:, self.xi]
        if self.yolox:
            img[:self.nh, :self.nw] = small[:, :, ::-1]             # YOLOX wants BGR, 0-255
            x = img.transpose(2, 0, 1)[None].astype(np.float32)
        else:
            img[:self.nh, :self.nw] = small
            x = img.transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = self.sess.run(None, {self.inp: x})[0][0]
        if self.yolox:
            xy = (out[:, :2] + self.grids) * self.strides
            wh = np.exp(np.clip(out[:, 2:4], -10, 10)) * self.strides
            scores = out[:, 4:5] * out[:, 5:]
        else:
            out = out.T                                              # [N, 4 + classes]
            xy, wh, scores = out[:, :2], out[:, 2:4], out[:, 4:]
        cls = scores.argmax(1)
        conf = scores[np.arange(len(cls)), cls]
        keep = conf >= min_score
        if not keep.any():
            return []
        xy, wh, cls, conf = xy[keep], wh[keep], cls[keep], conf[keep]
        boxes = np.concatenate([xy - wh / 2, xy + wh / 2], 1) / self.r
        k = nms(np, boxes + (cls * 4096)[:, None], conf)              # per-class
        # Then across classes: the same animal seen as both "dog" and "cat" keeps the better one.
        k = [k[j] for j in nms(np, boxes[k], conf[k], iou=0.8)]
        res = []
        for i in k:
            x1, y1, x2, y2 = boxes[i]
            res.append((self.labels[cls[i]] if cls[i] < len(self.labels) else str(cls[i]),
                        float(conf[i]),
                        max(0.0, x1 / DET_W), max(0.0, y1 / DET_H),
                        min(1.0, x2 / DET_W), min(1.0, y2 / DET_H)))
        return res


LABEL_GROUPS = {"dog": "pet", "cat": "pet", "car": "vehicle", "truck": "vehicle"}


def same_kind(a, b):
    """Labels the model often mixes up for one object count as the same thing when tracking."""
    return a == b or (a in LABEL_GROUPS and LABEL_GROUPS.get(a) == LABEL_GROUPS.get(b))


def iou(a, b):
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = w * h
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


class DetectionEngine(threading.Thread):
    """Looks at every camera's newest small frame a few times a second:
      1. motion check (cheap): only call the AI model when something moved, an object is
         being tracked, or every DET_HEARTBEAT s (to catch things that arrived and stopped)
      2. AI model: people, pets, cars, ... (only the labels you asked for are kept)
      3. tracking: a detection matching an existing object (same label, overlapping box)
         updates it; an object seen twice starts an EVENT (snapshot + log + webhook); one
         unseen for DET_GONE s ends it."""
    def __init__(self, feeds, opts, stop):
        super().__init__(daemon=True)
        self.feeds, self.opts, self.stop = feeds, opts, stop
        self.detector = opts.get("detector") or Detector(opts["model"])
        self.np = self.detector.np
        self.root = Path(opts["events_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.recent = []                       # newest last; for the browser view
        self.lock = threading.Lock()
        self.ids = dict(zip([id(f) for f in feeds], cam_ids([f.cam for f in feeds])))
        self.state = {id(f): {"seq": 0, "run": 0.0, "full": 0.0, "prev": None, "tracks": [],
                              "next": 1} for f in feeds}
        self.ms = []                           # recent model run times, for the status line

    def run(self):
        np = self.np
        period = 1.0 / max(0.2, self.opts["fps"])
        last_prune = 0.0
        while not self.stop.is_set():
            busy = False
            now = time.time()
            for f in self.feeds:
                st = self.state[id(f)]
                if f.det_frame is not None and f.det_seq != st["seq"] and now - st["run"] >= period:
                    st["seq"] = f.det_seq
                    st["run"] = now
                    frame = np.frombuffer(f.det_frame, np.uint8).reshape(DET_H, DET_W, 3)
                    small = frame[::8, ::8].mean(2)
                    prev, st["prev"] = st["prev"], small
                    moved = prev is None or (np.abs(small - prev) > 18).mean() > 0.002
                    if moved or st["tracks"] or now - st["full"] >= DET_HEARTBEAT:
                        st["full"] = now
                        t0 = time.time()
                        try:
                            dets = self.detector.detect(frame, self.opts["score"])
                        except Exception as e:            # never let the AI kill the viewer
                            log(f"detection error: {e}")
                            dets = []
                        self.ms = (self.ms + [(time.time() - t0) * 1000])[-50:]
                        dets = [d for d in dets if d[0] in self.opts["labels"]]
                        self._track(f, st, dets, now, frame)
                        busy = True
                self._expire(f, st, now)
            if now - last_prune > 1800:
                last_prune = now
                self._prune()
            if not busy:
                self.stop.wait(0.05)

    def _track(self, f, st, dets, now, frame):
        tracks, used = st["tracks"], set()
        for d in sorted(dets, key=lambda d: -d[1]):
            best, best_iou = None, 0.25
            for t in tracks:
                if same_kind(t["label"], d[0]) and id(t) not in used:
                    o = iou(t["box"], d[2:])
                    if o > best_iou:
                        best, best_iou = t, o
            if best is None:
                best = {"id": st["next"], "label": d[0], "first": now, "hits": 0, "event": False}
                st["next"] += 1
                tracks.append(best)
            votes = best.setdefault("votes", {})      # dog <-> cat flips: until the event
            votes[d[0]] = votes.get(d[0], 0) + 1      # starts, use the label seen most often
            if not best["event"]:
                best["label"] = max(votes, key=votes.get)
            best.update(box=d[2:], score=d[1], last=now, hits=best["hits"] + 1)
            used.add(id(best))
        for t in tracks:
            if t["hits"] >= 2 and not t["event"]:
                t["event"] = True
                self._event_start(f, t, frame, tracks)
        self._publish(f, st, now)

    def _expire(self, f, st, now):
        gone = [t for t in st["tracks"] if now - t["last"] > DET_GONE]
        if gone:
            st["tracks"] = [t for t in st["tracks"] if t not in gone]
            for t in gone:
                if t["event"]:
                    self._write({"type": "end", "camera": f.name, "cam_id": self.ids[id(f)],
                                 "label": t["label"], "track": t["id"],
                                 "duration": round(t["last"] - t["first"], 1)})
        self._publish(f, st, now)

    def _publish(self, f, st, now):
        """What the grid and browser view draw: confirmed objects seen in the last 2 s."""
        cur = [(t["label"], round(t["score"], 2), *[round(v, 4) for v in t["box"]])
               for t in st["tracks"] if t["hits"] >= 2 and now - t["last"] < 2]
        if cur != f.dets:
            f.dets = cur
            f.dets_ver += 1

    def _event_start(self, f, t, frame, tracks):
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        day = stamp[:10]
        rel = f"{day}/{stamp}_{safe_name(f.name).replace(' ', '_')}_{safe_name(t['label'])}_{t['id']}.jpg"
        ev = {"type": "start", "camera": f.name, "cam_id": self.ids[id(f)], "label": t["label"],
              "score": round(t["score"], 2), "box": [round(v, 4) for v in t["box"]],
              "track": t["id"], "snapshot": rel}
        log(f"[{f.name}] detected {t['label'].replace('_', ' ')} ({t['score']:.0%})")
        boxes = [(x["label"], x["score"], *x["box"]) for x in tracks
                 if x["hits"] >= 2 and x["last"] >= t["last"] - 1]       # seen in this frame
        threading.Thread(target=self._snapshot, args=(frame.tobytes(), boxes, self.root / rel),
                         daemon=True).start()
        self._write(ev)
        if self.opts.get("webhook"):
            threading.Thread(target=self._webhook, args=(dict(ev),), daemon=True).start()

    def _snapshot(self, raw, boxes, path):
        """Save the frame with the detected objects outlined and labelled (ffmpeg draws them)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        vf = []
        font = label_font()
        for label, score, x1, y1, x2, y2 in boxes:
            c = "0x" + label_color(label)[1:]
            x, y = int(x1 * DET_W), int(y1 * DET_H)
            w, h = max(2, int((x2 - x1) * DET_W)), max(2, int((y2 - y1) * DET_H))
            text = drawtext_escape(f"{label.replace('_', ' ')} {score:.0%}")
            vf.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={c}@0.95:t=3")
            vf.append(f"drawtext={font}expansion=none:text='{text}':x={x + 2}:y={max(0, y - 20)}:fontsize=16:"
                      f"fontcolor=white:box=1:boxcolor={c}@0.85:boxborderw=3")
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{DET_W}x{DET_H}", "-i", "-"]
        if vf:
            cmd += ["-vf", ",".join(vf)]
        cmd += ["-frames:v", "1", "-q:v", "4", str(path)]
        try:
            r = subprocess.run(cmd, input=raw, capture_output=True, timeout=15)
            if r.returncode != 0 and vf:                     # e.g. no font: boxes only
                cmd[cmd.index("-vf") + 1] = ",".join(v for v in vf if v.startswith("drawbox"))
                subprocess.run(cmd, input=raw, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _write(self, ev):
        ev = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "ts": round(time.time(), 3), **ev}
        with self.lock:
            self.recent = (self.recent + [ev])[-300:]
            try:
                with open(self.root / "events.jsonl", "a") as fh:
                    fh.write(json.dumps(ev) + "\n")
            except OSError:
                pass

    def _webhook(self, ev):
        """POST the event as JSON (e.g. to a Home Assistant webhook trigger)."""
        import urllib.request
        try:
            req = urllib.request.Request(self.opts["webhook"], data=json.dumps(ev).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).close()
        except Exception as e:
            log(f"webhook failed: {e}")

    def _prune(self):
        """Delete snapshot folders and log lines older than the keep period."""
        keep = self.opts.get("keep", DET_KEEP_DAYS)
        if keep <= 0:
            return
        cutoff = time.time() - keep * 86400
        day_cut = time.strftime("%Y-%m-%d", time.localtime(cutoff))
        for d in self.root.glob("????-??-??"):
            if d.is_dir() and d.name < day_cut:
                shutil.rmtree(d, ignore_errors=True)
        jl = self.root / "events.jsonl"
        try:
            lines = jl.read_text().splitlines()
            kept = [ln for ln in lines if json.loads(ln).get("ts", 0) >= cutoff]
            if len(kept) != len(lines):
                with self.lock:
                    jl.write_text("".join(ln + "\n" for ln in kept))
        except (OSError, ValueError):
            pass

    def events(self, limit=100):
        with self.lock:
            return list(reversed(self.recent[-limit:]))

    def status(self):
        ms = sorted(self.ms)
        return f"{ms[len(ms) // 2]:.0f} ms per check" if ms else "idle"


def start_detection(feeds, det, stop):
    if not det:
        return None
    eng = DetectionEngine([f for f in feeds if f.detect], det, stop)
    eng.start()
    return eng


# --------------------------------------------------------------------------- recording
def check_rec_dir(rec):
    """Make sure the recording folder exists and is writable before any camera starts."""
    root = Path(rec["dir"]).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".wyzecam-rtsp-write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        die(f"can't write to the recording folder {root}: {e}\n"
            "       (is the NAS share mounted, and writable by you?)")
    rec["dir"] = root
    return root


class Pruner(threading.Thread):
    """Deletes recordings older than `keep_days` (checked at start, then every 30 minutes).
    Only touches .mkv/.mp4 files this tool made, one folder down from the recording folder."""
    def __init__(self, root, keep_days, stop):
        super().__init__(daemon=True)
        self.root, self.keep, self.stop = Path(root), keep_days, stop

    def run(self):
        while not self.stop.is_set():
            cutoff = time.time() - self.keep * 86400
            removed = 0
            for f in [*self.root.glob("*/*_????-??-??_??-??-??.mkv"),
                      *self.root.glob("*/*_????-??-??_??-??-??.mp4")]:
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed += 1
                except OSError:
                    pass
            if removed:
                log(f"recordings: deleted {removed} file(s) older than {self.keep} day(s)")
            self.stop.wait(1800)


# --------------------------------------------------------------------------- browser view
WEB_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wyze Cams</title>
<style>
 html,body{margin:0;background:#111;color:#eee;font:14px system-ui,sans-serif;height:100%}
 #grid{display:grid;gap:4px;padding:4px;box-sizing:border-box;height:100%;
       grid-template-columns:repeat(var(--cols),1fr);grid-auto-rows:1fr}
 .tile{position:relative;background:#000;overflow:hidden;cursor:zoom-in;min-height:0}
 .tile img{width:100%;height:100%;object-fit:contain;display:block}
 .tile .name{position:absolute;left:8px;top:8px;padding:2px 8px;border-radius:4px;
             background:rgba(0,0,0,.55);font-weight:600}
 .tile .name::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;
             background:#3c3;margin-right:6px;vertical-align:1px}
 .tile.down img{filter:grayscale(1) brightness(.35)}
 .tile.down .name::before{background:#c33}
 .tile.zoom{position:fixed;inset:0;z-index:9;cursor:zoom-out;background:#000}
 body.names-off .name{display:none}
 @media (max-width:600px){            /* phones: one camera per row, scroll down for more */
  #grid{height:auto;grid-template-columns:1fr;grid-auto-rows:auto}
  .tile{aspect-ratio:16/9}
  #bar{display:none}
 }
 #bar{position:fixed;right:8px;bottom:8px;opacity:.5;font-size:12px}
 .box{position:absolute;border:3px solid var(--c);border-radius:3px;pointer-events:none;
      box-shadow:0 0 0 1px rgba(0,0,0,.5)}
 .box span{position:absolute;left:-3px;top:-22px;background:var(--c);color:#000;font:600 12px/18px
      system-ui,sans-serif;padding:0 6px;border-radius:3px 3px 3px 0;white-space:nowrap}
 .tile .tags{position:absolute;right:8px;top:8px;display:flex;gap:4px}
 .tile .tags b{background:var(--c);color:#000;font-size:12px;padding:2px 7px;border-radius:4px}
 #evbtn{position:fixed;left:8px;bottom:8px;z-index:20;background:#222;color:#eee;border:1px solid #444;
      border-radius:8px;padding:6px 12px;font:600 13px system-ui,sans-serif;cursor:pointer;display:none}
 #evbtn i{font-style:normal;background:#ff4d4d;color:#000;border-radius:9px;padding:0 6px;margin-left:6px}
 #events{position:fixed;top:0;right:0;bottom:0;width:min(380px,100%);background:#161616;z-index:30;
      border-left:1px solid #333;overflow-y:auto;display:none;padding:12px;box-sizing:border-box}
 #events.open{display:block}
 #events h3{margin:4px 0 12px;font-size:16px;display:flex;justify-content:space-between}
 #events h3 a{color:#aaa;cursor:pointer;font-weight:400}
 .ev{display:flex;gap:10px;margin-bottom:10px;cursor:pointer}
 .ev img{width:136px;aspect-ratio:16/9;object-fit:cover;border-radius:4px;background:#000}
 .ev div{font-size:13px;color:#bbb}
 .ev b{display:block;color:#fff;font-size:14px}
 .ev em{font-style:normal;color:var(--c);font-weight:700}
 #recbtn{position:fixed;left:8px;bottom:8px;z-index:20;background:#222;color:#eee;border:1px solid #444;
      border-radius:8px;padding:6px 12px;font:600 13px system-ui,sans-serif;cursor:pointer;display:none}
 #evbtn.withrec{left:136px}
 #rec{position:fixed;inset:0;background:#111;z-index:35;display:none;flex-direction:column;overflow:hidden}
 #rec.open{display:flex}
 #rechead{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid #2a2a2a}
 #rechead b{font-size:15px;margin-right:6px}
 #rechead button{background:#222;color:#ddd;border:1px solid #3a3a3a;border-radius:6px;padding:4px 10px;
      font:600 13px system-ui,sans-serif;cursor:pointer}
 #rechead button.on{background:#3cf0ff;color:#000;border-color:#3cf0ff}
 #rechead .sp{flex:1}
 #recday{font:600 14px system-ui,sans-serif;min-width:118px;text-align:center}
 #player{flex:1;min-height:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
      background:#000;position:relative}
 #player video{width:100%;height:100%;object-fit:contain;background:#000}
 #player .hint{position:absolute;color:#888;font-size:14px;text-align:center;padding:20px}
 #pcap{display:flex;gap:12px;align-items:center;padding:6px 10px;font-size:13px;color:#bbb;
      border-top:1px solid #2a2a2a;min-height:20px}
 #pcap b{color:#fff} #pcap a{color:#3cf0ff;margin-left:auto}
 #rows{max-height:42vh;overflow-y:auto;padding:6px 10px 12px}
 .rrow{display:flex;align-items:center;gap:10px;margin:6px 0}
 .rrow .rn{width:120px;flex:none;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .bar{position:relative;flex:1;height:34px;background:#1d1d1d;border-radius:5px;cursor:pointer;overflow:hidden}
 .bar .seg{position:absolute;top:0;bottom:0;background:#2f5d73}
 .bar .mk{position:absolute;top:3px;bottom:3px;width:3px;margin-left:-1px;border-radius:2px;background:var(--c);
      box-shadow:0 0 4px var(--c)}
 .bar .ph{position:absolute;top:0;bottom:0;width:2px;background:#fff;display:none}
 .bar .nw{position:absolute;top:0;bottom:0;width:1px;background:#ff4d4d;opacity:.7}
 .ticks{position:relative;height:16px;margin:0 10px 0 140px;font-size:11px;color:#777}
 .ticks span{position:absolute;transform:translateX(-50%);white-space:nowrap}
 .ev .play{color:#3cf0ff;font-weight:700;margin-top:3px;display:inline-block}
 @media (max-width:600px){.rrow .rn{width:70px}.ticks{margin-left:90px}#rows{max-height:38vh}}
 #full{position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:40;display:none;cursor:zoom-out}
 #full img{width:100%;height:100%;object-fit:contain}
</style></head><body>
<div id="grid"></div>
<div id="bar">click a camera to zoom · n = names · f = fullscreen · e = events · r = recordings</div>
<button id="recbtn">▶ Recordings</button>
<button id="evbtn">Events<i id="evn">0</i></button>
<div id="rec">
 <div id="rechead"><b>Recordings</b>
  <button id="dprev" title="previous day">◀</button><span id="recday"></span><button id="dnext" title="next day">▶</button>
  <span class="sp"></span>
  <button data-z="86400" class="on">24 h</button><button data-z="21600">6 h</button><button data-z="3600">1 h</button>
  <button id="pprev" title="earlier">«</button><button id="pnext" title="later">»</button>
  <button id="recclose">✕</button></div>
 <div id="player"><video id="vid" controls playsinline preload="auto"></video>
  <div class="hint" id="phint">Click a timeline below to play from that moment.</div></div>
 <div id="pcap"><span id="pwhat"></span><a id="pdl" href="#" style="display:none">⬇ Download clip</a></div>
 <div class="ticks" id="ticks"></div>
 <div id="rows"></div>
</div>
<div id="events"><h3>Detections <a id="evclose">close ✕</a></h3><div id="evlist"></div></div>
<div id="full"><img alt=""></div>
<script>
const sleep = ms => new Promise(r => setTimeout(r, ms));
let recOpen = false;
async function feed(cam, tile, img) {
  let seq = 0, url = null;
  for (;;) {
    if (recOpen) { await sleep(500); continue; }             // paused while watching recordings
    try {
      const r = await fetch(`cam/${cam.id}.jpg?after=${seq}`, {cache: "no-store"});
      if (r.status !== 200) { tile.classList.add("down"); await sleep(2000); continue; }
      seq = +r.headers.get("X-Seq");
      const u = URL.createObjectURL(await r.blob());
      await new Promise(res => { img.onload = img.onerror = res; img.src = u; });
      if (url) URL.revokeObjectURL(url);
      url = u;
      tile.classList.remove("down");
    } catch (e) { tile.classList.add("down"); await sleep(2000); }
  }
}
const COLORS = {person: "#ff4d4d", dog: "#3cf05a", cat: "#3cf05a", bird: "#3cf05a", car: "#3cb4ff",
                truck: "#3cb4ff", bicycle: "#3cb4ff", motorcycle: "#3cb4ff", package: "#ffb43c"};
const color = l => COLORS[l] || "#ff5cf0";
const nice = l => l.replace(/_/g, " ");
const esc = t => String(t).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const tiles = {};
function drawBoxes(t, dets) {
  t.boxes.forEach(b => b.remove()); t.boxes = []; t.tags.textContent = "";
  const img = t.img, W = t.tile.clientWidth, H = t.tile.clientHeight;
  if (!img.naturalWidth) return;
  const k = Math.min(W / img.naturalWidth, H / img.naturalHeight);       // object-fit: contain
  const w = img.naturalWidth * k, h = img.naturalHeight * k, ox = (W - w) / 2, oy = (H - h) / 2;
  const seen = new Set();
  for (const [label, score, x1, y1, x2, y2] of dets) {
    const b = document.createElement("div");
    b.className = "box"; b.style.setProperty("--c", color(label));
    Object.assign(b.style, {left: ox + x1 * w + "px", top: oy + y1 * h + "px",
                            width: (x2 - x1) * w + "px", height: (y2 - y1) * h + "px"});
    b.innerHTML = `<span>${esc(nice(label))} ${Math.round(score * 100)}%</span>`;
    t.tile.append(b); t.boxes.push(b);
    if (!seen.has(label)) {
      seen.add(label);
      const tag = document.createElement("b");
      tag.style.setProperty("--c", color(label)); tag.textContent = nice(label); t.tags.append(tag);
    }
  }
}
async function detections() {
  for (;;) {
    try {
      const d = await (await fetch("api/detections", {cache: "no-store"})).json();
      for (const id in tiles) drawBoxes(tiles[id], d[id] || []);
    } catch (e) {}
    await sleep(500);
  }
}
const evlist = document.getElementById("evlist"), panel = document.getElementById("events");
const full = document.getElementById("full");
let seenTs = 0;
async function events() {
  for (;;) {
    try {
      const evs = (await (await fetch("api/events?limit=60", {cache: "no-store"})).json())
                    .filter(e => e.type === "start");
      const fresh = evs.filter(e => e.ts > Date.now() / 1000 - 600).length;
      document.getElementById("evn").textContent = fresh;
      document.getElementById("evn").style.display = fresh ? "" : "none";
      evlist.innerHTML = evs.length ? "" : "<div style='color:#888'>No detections yet.</div>";
      for (const e of evs) {
        const row = document.createElement("div");
        row.className = "ev"; row.style.setProperty("--c", color(e.label));
        row.innerHTML = `<img loading="lazy" src="events/${esc(e.snapshot)}" alt="">
          <div><b>${esc(e.camera)}</b><em>${esc(nice(e.label))}</em> ${Math.round(e.score * 100)}%<br>
          ${esc(e.time.replace("T", " "))}${hasRec ? '<br><span class="play">▶ Play recording</span>' : ""}</div>`;
        row.querySelector("img").onclick = ev => { ev.stopPropagation();
          full.querySelector("img").src = `events/${e.snapshot}`; full.style.display = "block"; };
        row.onclick = () => hasRec ? jumpTo(e.cam_id, e.ts - 5) : row.querySelector("img").onclick(new Event("x"));
        evlist.append(row);
      }
    } catch (e) {}
    await sleep(panel.classList.contains("open") ? 3000 : 10000);
  }
}
const toggleEvents = () => panel.classList.toggle("open");
document.getElementById("evbtn").onclick = toggleEvents;
document.getElementById("evclose").onclick = toggleEvents;
full.onclick = () => full.style.display = "none";
fetch("api/cams", {cache: "no-store"}).then(r => r.json()).then(cams => {
  const grid = document.getElementById("grid");
  grid.style.setProperty("--cols", Math.ceil(Math.sqrt(cams.length)) || 1);
  for (const cam of cams) {
    const tile = document.createElement("div"), img = new Image(), name = document.createElement("div");
    const tags = document.createElement("div");
    tile.className = "tile down"; name.className = "name"; name.textContent = cam.name;
    tags.className = "tags";
    tile.title = cam.name; tile.append(img, name, tags); grid.append(tile);
    tile.onclick = () => tile.classList.toggle("zoom");
    tiles[cam.id] = {tile, img, tags, boxes: []};
    feed(cam, tile, img);
  }
  recCams = cams.filter(c => c.rec);
  hasRec = recCams.length > 0;
  if (hasRec) document.getElementById("recbtn").style.display = "block";
  if (cams.some(c => c.detect)) {
    const eb = document.getElementById("evbtn");
    eb.style.display = "block";
    if (hasRec) eb.classList.add("withrec");
    detections(); events();
  }
});

// ---------------------------------------------------------------- recordings / playback
let recCams = [], hasRec = false, recDay = null, viewSpan = 86400, viewStart = 0;
const recData = {};                       // cam id -> {segs: [...], evs: [...]}
let cur = null;                           // what's playing: {cam, i (segment index), seg}
const vid = document.getElementById("vid"), recEl = document.getElementById("rec");
const pad = n => String(n).padStart(2, "0");
const dayOf = ts => { const d = new Date(ts * 1000); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
const dayStart = day => new Date(day + "T00:00:00").getTime() / 1000;
const hhmmss = ts => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const camName = id => (recCams.find(c => c.id === id) || {}).name || id;

async function loadDay(day) {
  recDay = day;
  document.getElementById("recday").textContent = new Date(day + "T12:00:00")
    .toLocaleDateString(undefined, {weekday: "short", month: "short", day: "numeric"});
  let evs = [];
  try { evs = await (await fetch(`api/events?date=${day}`, {cache: "no-store"})).json(); } catch (e) {}
  await Promise.all(recCams.map(async c => {
    let segs = [];
    try { segs = await (await fetch(`api/recordings?cam=${c.id}&date=${day}`, {cache: "no-store"})).json(); } catch (e) {}
    recData[c.id] = {segs, evs: evs.filter(e => e.cam_id === c.id)};
  }));
  render();
}
function clampView() {
  const d0 = dayStart(recDay);
  viewStart = Math.max(d0, Math.min(d0 + 86400 - viewSpan, viewStart));
}
function xpct(t) { return (t - viewStart) / viewSpan * 100; }
function render() {
  clampView();
  const rows = document.getElementById("rows"), ticks = document.getElementById("ticks");
  rows.textContent = ""; ticks.textContent = "";
  const step = viewSpan > 43200 ? 10800 : viewSpan > 7200 ? 3600 : 600;
  for (let t = Math.ceil(viewStart / step) * step; t < viewStart + viewSpan - step / 4; t += step) {
    const sp = document.createElement("span");
    sp.style.left = xpct(t) + "%"; sp.textContent = hhmmss(t).slice(0, 5); ticks.append(sp);
  }
  const now = Date.now() / 1000;
  for (const c of recCams) {
    const d = recData[c.id] || {segs: [], evs: []};
    const row = document.createElement("div"), name = document.createElement("div"), bar = document.createElement("div");
    row.className = "rrow"; name.className = "rn"; name.textContent = c.name; name.title = c.name;
    bar.className = "bar"; bar.dataset.cam = c.id;
    for (const s of d.segs) {
      if (s.end < viewStart || s.start > viewStart + viewSpan) continue;
      const g = document.createElement("div");
      g.className = "seg";
      g.style.left = Math.max(0, xpct(s.start)) + "%";
      g.style.width = Math.max(0.15, Math.min(100, xpct(s.end)) - Math.max(0, xpct(s.start))) + "%";
      bar.append(g);
    }
    for (const e of d.evs) {
      if (e.ts < viewStart || e.ts > viewStart + viewSpan) continue;
      const m = document.createElement("div");
      m.className = "mk"; m.style.left = xpct(e.ts) + "%"; m.style.setProperty("--c", color(e.label));
      m.title = `${nice(e.label)} ${hhmmss(e.ts)}`;
      m.onclick = ev => { ev.stopPropagation(); jumpTo(c.id, e.ts - 5); };
      bar.append(m);
    }
    if (now > viewStart && now < viewStart + viewSpan) {
      const nw = document.createElement("div"); nw.className = "nw"; nw.style.left = xpct(now) + "%"; bar.append(nw);
    }
    const ph = document.createElement("div"); ph.className = "ph"; bar.append(ph);
    bar.onclick = ev => {
      const r = bar.getBoundingClientRect();
      play(c.id, viewStart + (ev.clientX - r.left) / r.width * viewSpan);
    };
    row.append(name, bar); rows.append(row);
  }
  playhead();
}
function playhead() {
  document.querySelectorAll(".bar .ph").forEach(p => p.style.display = "none");
  if (!cur) return;
  const t = cur.seg.start + (vid.currentTime || 0);
  const bar = document.querySelector(`.bar[data-cam="${cur.cam}"]`);
  if (!bar || t < viewStart || t > viewStart + viewSpan) return;
  const p = bar.querySelector(".ph"); p.style.display = "block"; p.style.left = xpct(t) + "%";
  document.getElementById("pwhat").innerHTML = `<b>${esc(camName(cur.cam))}</b> ${hhmmss(t)}`;
}
function play(camId, t, idx) {
  const segs = (recData[camId] || {}).segs || [];
  // Files overlap by a moment (each ends a little after the next begins): take the latest.
  let i = idx !== undefined ? idx : segs.findLastIndex(s => t >= s.start && t < s.end);
  if (i < 0) i = segs.findIndex(s => s.start > t);          // in a gap: start at the next recording
  const hint = document.getElementById("phint");
  if (i < 0) { hint.textContent = "No recording at that time."; hint.style.display = "block"; return; }
  const seg = segs[i];
  cur = {cam: camId, i, seg};
  hint.textContent = "Loading…"; hint.style.display = "block";
  const url = `rec/${camId}/${encodeURIComponent(seg.file)}?v=${Math.floor(seg.end)}`;
  vid.src = url;
  const dl = document.getElementById("pdl"); dl.href = url + "&dl=1"; dl.style.display = "";
  vid.onloadedmetadata = () => {
    vid.currentTime = Math.max(0, Math.min(t - seg.start, (vid.duration || 1) - 0.5));
    vid.play().catch(() => {});
    hint.style.display = "none";
  };
  vid.onerror = () => { hint.textContent = "Couldn't play this recording."; hint.style.display = "block"; };
}
function next() {                                         // roll into the next file
  if (!cur || cur.rolled) return;
  const segs = recData[cur.cam].segs, nx = segs[cur.i + 1];
  if (nx) { cur.rolled = true; play(cur.cam, nx.start, cur.i + 1); }
}
vid.addEventListener("timeupdate", () => {
  playhead();
  // Recordings' audio and video can end a few frames apart, so the browser may stall just
  // short of the end instead of firing "ended": move on when we're within half a second.
  if (vid.duration && vid.duration - vid.currentTime < 0.5) next();
});
vid.addEventListener("ended", next);
async function jumpTo(camId, t) {
  openRec(true);
  const day = dayOf(t);
  if (day !== recDay || !recData[camId]) await loadDay(day);
  if (viewSpan === 86400) viewStart = dayStart(day); else viewStart = t - viewSpan / 2;
  render();
  play(camId, t);
}
function openRec(on) {
  recOpen = on === undefined ? !recEl.classList.contains("open") : on;
  recEl.classList.toggle("open", recOpen);
  panel.classList.remove("open");
  if (recOpen && !recDay) { viewStart = dayStart(dayOf(Date.now() / 1000)); loadDay(dayOf(Date.now() / 1000)); }
  if (!recOpen) vid.pause();
}
function shiftDay(n) {
  const d = new Date(recDay + "T12:00:00"); d.setDate(d.getDate() + n);
  const day = dayOf(d.getTime() / 1000); viewStart = dayStart(day); loadDay(day);
}
document.getElementById("recbtn").onclick = () => openRec(true);
document.getElementById("recclose").onclick = () => openRec(false);
document.getElementById("dprev").onclick = () => shiftDay(-1);
document.getElementById("dnext").onclick = () => shiftDay(1);
document.getElementById("pprev").onclick = () => { viewStart -= viewSpan / 2; render(); };
document.getElementById("pnext").onclick = () => { viewStart += viewSpan / 2; render(); };
document.querySelectorAll("#rechead [data-z]").forEach(b => b.onclick = () => {
  const center = cur ? cur.seg.start + vid.currentTime : viewStart + viewSpan / 2;
  viewSpan = +b.dataset.z; viewStart = center - viewSpan / 2;
  document.querySelectorAll("#rechead [data-z]").forEach(x => x.classList.toggle("on", x === b));
  render();
});
setInterval(() => {                                          // today: keep the timeline fresh
  if (recOpen && recDay === dayOf(Date.now() / 1000)) loadDay(recDay);
}, 30000);
document.addEventListener("keydown", e => {
  if (e.key === "n") document.body.classList.toggle("names-off");
  if (e.key === "f") document.fullscreenElement ? document.exitFullscreen()
                                                 : document.documentElement.requestFullscreen();
  if (e.target.tagName === "INPUT") return;
  if (e.key === "e" && document.getElementById("evbtn").style.display === "block") toggleEvents();
  if (e.key === "r" && hasRec) openRec();
  if (e.key === "Escape") {
    document.querySelectorAll(".zoom").forEach(t => t.classList.remove("zoom"));
    panel.classList.remove("open"); full.style.display = "none";
    if (recOpen) openRec(false);
  }
});
</script></body></html>
"""


REC_NAME = re.compile(r"[^/\\]+_(\d{4}-\d\d-\d\d)_(\d\d)-(\d\d)-(\d\d)\.(mkv|mp4)")


class Recordings:
    """Playback for the browser view: lists each camera's recordings for a day, and turns a
    recording into a browser-friendly MP4 the first time it's played. That's a copy (no
    re-encoding, ~1 s for a 5-minute file) with the index at the front, so the browser can
    jump anywhere in it; MKV recordings play this way too. Copies live in a temp folder
    and are deleted after PLAY_CACHE_HOURS."""
    PLAY_CACHE_HOURS = 2

    def __init__(self, root, events_dir):
        self.root = Path(root) if root else None
        self.events_dir = Path(events_dir) if events_dir else None
        self.cache = Path(tempfile.gettempdir()) / "wyzecam-rtsp-play"
        self.locks = {}
        self.lock = threading.Lock()

    def folder(self, feed):
        return self.root / safe_name(feed.name)

    def days(self, feed):
        out = set()
        if self.root:
            for p in self.folder(feed).glob("*_????-??-??_??-??-??.*"):
                m = REC_NAME.fullmatch(p.name)
                if m:
                    out.add(m.group(1))
        return sorted(out)

    def list(self, feed, day):
        """[{file, start, end, size}] for one day. start comes from the file name (the clock
        time it began); end is when it was last written to."""
        segs = []
        if not self.root:
            return segs
        for p in self.folder(feed).glob(f"*_{day}_??-??-??.*"):
            m = REC_NAME.fullmatch(p.name)
            if not m:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size < 4096:
                continue
            start = time.mktime(time.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
                                              "%Y-%m-%d %H:%M:%S"))
            segs.append({"file": p.name, "start": start, "end": max(start + 1, st.st_mtime),
                         "size": st.st_size})
        segs.sort(key=lambda s: s["start"])
        return segs

    def events(self, day):
        """Detection events (starts) for one day, from events.jsonl."""
        out = []
        jl = self.events_dir / "events.jsonl" if self.events_dir else None
        try:
            with open(jl) as fh:
                for ln in fh:
                    if f'"time": "{day}T' not in ln:
                        continue
                    try:
                        ev = json.loads(ln)
                    except ValueError:
                        continue
                    if ev.get("type") == "start":
                        out.append(ev)
        except (OSError, TypeError):
            pass
        return out[-3000:]

    def source(self, feed, name):
        if not self.root or not REC_NAME.fullmatch(name):
            return None
        p = self.folder(feed) / name
        return p if p.is_file() else None

    def playable(self, src, version=None):
        """Path of a browser-friendly MP4 copy of `src`, made once per version. The page asks
        for a version (the file's end time from the listing), so a file that's still being
        recorded stays the same copy while it's played, instead of changing under the player."""
        st = src.stat()
        ver = re.sub(r"\D", "", str(version))[:16] if version else f"{st.st_size}_{int(st.st_mtime)}"
        key = re.sub(r"[^\w-]", "_", f"{src.parent.name}_{src.stem}_{ver}")
        out = self.cache / f"{key}.mp4"
        if out.is_file():
            return out
        with self.lock:
            lk = self.locks.setdefault(key, threading.Lock())
        with lk:
            if out.is_file():
                return out
            self.cache.mkdir(parents=True, exist_ok=True)
            self._clean()
            tag = []
            try:
                codec = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                        "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                                        str(src)], capture_output=True, text=True, timeout=20).stdout
                if "hevc" in codec:
                    tag = ["-tag:v", "hvc1"]              # Safari/iPhone need this for H.265
            except (OSError, subprocess.TimeoutExpired):
                pass
            tmp = out.with_name(out.stem + ".part.mp4")
            r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0",
                                "-map", "0:a:0?", "-c", "copy", *tag, "-movflags", "+faststart",
                                str(tmp)], capture_output=True, timeout=180)
            if r.returncode != 0 or not tmp.is_file():
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise OSError(r.stderr.decode(errors="replace").strip()[-300:] or "ffmpeg failed")
            tmp.replace(out)
        with self.lock:
            self.locks.pop(key, None)
        return out

    def _clean(self):
        cutoff = time.time() - self.PLAY_CACHE_HOURS * 3600
        for p in self.cache.glob("*.mp4"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


def cam_ids(cams):
    """Short URL ids: the last number of the IP (like -c), or the whole IP if that clashes."""
    last = [c["ip"].rsplit(".", 1)[1] for c in cams]
    return [l if last.count(l) == 1 else c["ip"].replace(".", "-") for l, c in zip(last, cams)]


class WebView:
    """Browser view: a small web server that serves each camera's newest JPEG.
      /              live grid page (works in any browser, phone or TV)
      /cam/ID.jpg    newest snapshot (?after=SEQ waits for a newer one; used by the page)
      /cam/ID.mjpg   MJPEG stream, for Home Assistant, VLC, mpv, etc.
      /api/cams      camera list (JSON)
      /api/recordings?cam=ID&date=YYYY-MM-DD, /api/days?cam=ID, /api/events?date=...
      /rec/ID/FILE   a recording, as a browser-friendly MP4 (supports seeking; ?dl=1 downloads)
    Protected by the RTSP username/password (HTTP Basic auth)."""
    def __init__(self, feeds, port, bind, user, password, stop, engine=None, recs=None):
        import base64
        import hmac
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, unquote, urlsplit
        self.by_id = dict(zip(cam_ids([f.cam for f in feeds]), feeds))
        expect = ("Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()) if password else None
        by_id = self.by_id

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def send_file(self, path, ctype, extra=None):
                """Send a file, honouring a Range request (browsers need that to seek video)."""
                size = path.stat().st_size
                start, end = 0, size - 1
                rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
                if rng and (rng.group(1) or rng.group(2)):
                    if rng.group(1):
                        start = int(rng.group(1))
                        end = min(size - 1, int(rng.group(2))) if rng.group(2) else size - 1
                    else:                                    # "bytes=-N": the last N bytes
                        start = max(0, size - int(rng.group(2)))
                    if start > end or start >= size:
                        return self.send(416, b"", extra={"Content-Range": f"bytes */{size}"})
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, max-age=60")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                with open(path, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = fh.read(min(262144, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)

            def do_GET(self):
                if expect and not hmac.compare_digest(self.headers.get("Authorization", ""), expect):
                    return self.send(401, b"login required\n",
                                     extra={"WWW-Authenticate": 'Basic realm="Wyze Cams"'})
                u = urlsplit(self.path)
                m = re.fullmatch(r"/cam/([\w-]+)\.(jpg|mjpg)", u.path)
                if u.path in ("/", "/index.html"):
                    return self.send(200, WEB_PAGE.encode(), "text/html; charset=utf-8")
                if u.path == "/api/cams":
                    now = time.time()
                    data = [{"id": i, "name": f.name, "ip": f.cam["ip"],
                             "live": bool(f.jpeg_time and now - f.jpeg_time < STALL_SECS),
                             "detect": bool(engine and f.detect),
                             "rec": bool(recs and recs.root)}
                            for i, f in by_id.items()]
                    return self.send(200, json.dumps(data).encode(), "application/json")
                if u.path == "/api/detections":
                    data = {i: f.dets for i, f in by_id.items() if f.detect}
                    return self.send(200, json.dumps(data).encode(), "application/json")
                q = parse_qs(u.query)
                if u.path in ("/api/recordings", "/api/days"):
                    f = by_id.get(q.get("cam", [""])[0])
                    if not f or not recs:
                        return self.send(404, b"not found\n")
                    if u.path == "/api/days":
                        data = recs.days(f)
                    else:
                        day = q.get("date", [time.strftime("%Y-%m-%d")])[0]
                        if not re.fullmatch(r"\d{4}-\d\d-\d\d", day):
                            return self.send(400, b"bad date\n")
                        data = recs.list(f, day)
                    return self.send(200, json.dumps(data).encode(), "application/json")
                if u.path == "/api/events" and "date" in q:
                    day = q["date"][0]
                    if not re.fullmatch(r"\d{4}-\d\d-\d\d", day):
                        return self.send(400, b"bad date\n")
                    data = recs.events(day) if recs else []
                    return self.send(200, json.dumps(data).encode(), "application/json")
                rm = re.fullmatch(r"/rec/([\w-]+)/([^/]+)", u.path)
                if rm:
                    f = by_id.get(rm.group(1))
                    src = recs.source(f, unquote(rm.group(2))) if (f and recs) else None
                    if not src:
                        return self.send(404, b"no such recording\n")
                    try:
                        mp4 = recs.playable(src, q.get("v", [None])[0])
                    except (OSError, subprocess.TimeoutExpired) as e:
                        log(f"playback: couldn't prepare {src.name}: {e}")
                        return self.send(500, b"couldn't prepare this recording\n")
                    extra = {}
                    if q.get("dl"):
                        extra["Content-Disposition"] = f'attachment; filename="{src.stem}.mp4"'
                    return self.send_file(mp4, "video/mp4", extra)
                if u.path == "/api/events":
                    try:
                        limit = max(1, min(300, int(parse_qs(u.query).get("limit", ["100"])[0])))
                    except ValueError:
                        limit = 100
                    data = engine.events(limit) if engine else []
                    return self.send(200, json.dumps(data).encode(), "application/json")
                ev = re.fullmatch(r"/events/(\d{4}-\d\d-\d\d/[\w.\-]+\.jpg)", u.path)
                ev_root = engine.root if engine else (recs.events_dir if recs else None)
                if ev and ev_root:
                    f = ev_root / ev.group(1)
                    try:
                        return self.send(200, f.read_bytes(), "image/jpeg")
                    except OSError:
                        return self.send(404, b"not found\n")
                if not m or m.group(1) not in by_id:
                    return self.send(404, b"not found\n")
                f = by_id[m.group(1)]
                if m.group(2) == "jpg":
                    try:
                        after = int(parse_qs(u.query).get("after", ["0"])[0])
                    except ValueError:
                        after = 0
                    with f.jpeg_cond:
                        f.jpeg_cond.wait_for(lambda: f.jpeg_seq > after or stop.is_set(), timeout=5)
                        img, seq, t = f.jpeg, f.jpeg_seq, f.jpeg_time
                    if img is None or seq <= after or time.time() - t > STALL_SECS:
                        return self.send(503, b"no live video right now\n")
                    return self.send(200, img, "image/jpeg", {"X-Seq": str(seq)})
                # MJPEG stream
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                seq = 0
                try:
                    while not stop.is_set():
                        with f.jpeg_cond:
                            f.jpeg_cond.wait_for(lambda: f.jpeg_seq > seq or stop.is_set(), timeout=5)
                            img, new = f.jpeg, f.jpeg_seq
                        if img is None or new <= seq:
                            continue
                        seq = new
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                         + str(len(img)).encode() + b"\r\n\r\n" + img + b"\r\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):
                if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
                    super().handle_error(request, client_address)   # a browser closing is normal

        try:
            self.httpd = Server((bind, port), Handler)
        except OSError as e:
            die(f"can't start the browser view on port {port}: {e} (try --web 8081)")
        self.port = port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        if not password:
            print(" WARNING: no RTSP password set, so the browser view has NO login.")

    def urls(self):
        host = local_ip() or "localhost"
        return f"http://{host}:{self.port}/"

    def close(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except OSError:
            pass


def start_extras(feeds, rec, web, user, password, stop, det=None):
    """Start the recording pruner, object detection and the browser view (grid and 'serve')."""
    web_view = None
    if rec and rec["keep"] > 0:
        Pruner(rec["dir"], rec["keep"], stop).start()
    engine = start_detection(feeds, det, stop)
    if web:
        events_dir = det["events_dir"] if det else APP_DIR / "events"
        recs = Recordings(rec["dir"] if rec else None, events_dir)
        web_view = WebView(feeds, web["port"], web["bind"], user, password, stop, engine, recs)
    return web_view, engine


def feed_opts(c, rec, web, det):
    """CameraFeed options for recording, the browser view and detection."""
    return dict(rec_root=rec and rec["dir"], rec_segment=rec["segment"] if rec else 300,
                rec_format=rec["format"] if rec else "mkv", web=bool(web),
                detect=bool(det) and (not det["cams"] or c["ip"] in det["cams"]),
                det_every=det["every"] if det else 10)


def print_detection(det, engine):
    if engine:
        print(f" Detecting {', '.join(sorted(det['labels']))} on {len(engine.feeds)} camera(s) "
              f"(model: {Path(det['model']).name}); snapshots + log in {det['events_dir']}")


def print_extras(rec, web_view, n):
    if rec:
        print(f" Recording {n} camera(s) to {rec['dir']}  ({rec['segment'] // 60}-min .{rec['format']} files, "
              f"{'kept %s days' % rec['keep'] if rec['keep'] > 0 else 'kept forever'})")
    if web_view:
        print(f" Browser view: {web_view.urls()}  (log in with your RTSP username/password)")


def run_headless(cams, user, password, path, rec=None, web=None, resync_min=0, det=None):
    """'serve': no window. Record and/or serve the browser view, e.g. on a server or in a
    container. Each camera still has its own ffmpeg (one camera session) and reconnect loop."""
    need("ffmpeg")
    stop = threading.Event()
    cfg = load_saved()
    cfg_by_ip = {c["ip"]: c for c in cfg.get("cameras", [])}
    feeds = [CameraFeed(c, user, password, path, 0, 0, stop, 1, cfg_by_ip.get(c["ip"]),
                        tiles=False, **feed_opts(c, rec, web, det)) for c in cams]
    web_view, engine = start_extras(feeds, rec, web, user, password, stop, det)
    for f in feeds:
        f.start()
    n = len(feeds)
    resync_s = resync_min * 60 if resync_min > 0 else 0
    t0 = time.time()
    next_resync = [t0 + resync_s + i * resync_s / n if resync_s else float("inf") for i in range(n)]

    def shutdown(*_):
        stop_soon(stop)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    bar = "=" * 64
    print(bar)
    print(f" Serving {n} camera(s) with no window: " + ", ".join(c["name"] for c in cams))
    print_extras(rec, web_view, n)
    print_detection(det, engine)
    print(" Press Ctrl+C to stop.")
    print(bar)
    log(f"serve started: {', '.join(c['name'] for c in cams)}"
        + (f"; recording to {rec['dir']}" if rec else "")
        + (f"; browser view on port {web['port']}" if web else "")
        + ("; detection on" if engine else ""))
    last_status = time.time()
    was_live = [False] * n
    try:
        while not stop.is_set():
            now = time.time()
            watchdog_tick(feeds, next_resync, resync_s, now)
            for i, f in enumerate(feeds):
                live = bool(f.last and now - f.last < STALL_SECS)
                if live != was_live[i]:
                    was_live[i] = live
                    log(f"[{f.name}] {'live' if live else 'no live video'}")
            save_if_needed(feeds, cfg, cfg_by_ip)
            if now - last_status > 600:
                last_status = now
                log_status(feeds, now)
                if engine:
                    log(f"detection: {engine.status()}")
            stop.wait(1)
    finally:
        stop.set()
        for f in feeds:
            stop_ffmpeg(f.proc)
        if web_view:
            web_view.close()
        log("serve stopped")


def play_mosaic(cams, user, password, path, fps=15, resync_min=15, gpu_scale=False, rec=None, web=None,
                det=None):
    """One window with every camera tiled. Each camera has its own ffmpeg and reconnect loop,
    so a slow or dead camera never freezes the others. Cameras are resynced (reconnected) on a
    staggered schedule so none drifts behind live; press r to resync all now.
    rec / web: also record each camera and/or serve the browser view, from the same session."""
    need("ffmpeg")
    n = len(cams)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    tw = (1920 // cols) // 4 * 4            # multiples of 4 keep the YUV planes aligned
    th = (tw * 9 // 16) // 4 * 4
    W, H = tw * cols, th * rows
    keep_every = 1 if fps > 10 else 2       # cameras send 20 fps; at <=10 skip every other frame

    stop = threading.Event()
    cfg = load_saved()
    cfg_by_ip = {c["ip"]: c for c in cfg.get("cameras", [])}
    feeds = [CameraFeed(c, user, password, path, tw, th, stop, keep_every, cfg_by_ip.get(c["ip"]),
                        gpu_scale, **feed_opts(c, rec, web, det)) for c in cams]
    web_view, engine = start_extras(feeds, rec, web, user, password, stop, det)
    for f in feeds:
        f.start()

    resync_s = resync_min * 60 if resync_min > 0 else 0
    t0 = time.time()
    next_resync = [t0 + resync_s + i * resync_s / n if resync_s else float("inf") for i in range(n)]

    tmp = tempfile.mkdtemp(prefix="wyzecam-rtsp-")
    conf_path = os.path.join(tmp, "input.conf")
    sock_path = ipc_path(tmp)
    with open(conf_path, "w") as fh:
        fh.write("q quit 9\nr script-message resync\nn script-message names\nh script-message help\n"
                 "LEFT script-message move left\nRIGHT script-message move right\n"
                 "UP script-message move up\nDOWN script-message move down\n"
                 "d script-message hide\nDEL script-message hide\na script-message showall\n"
                 "z script-message zoom\nENTER script-message zoom\ns script-message save\n"
                 "e script-message rename\n"
                 "0 script-message select 0\n"
                 + "".join(f"{k} script-message select {k}\n" for k in range(1, 10)))
    mpv_cmd = ["mpv", "--demuxer=rawvideo", f"--demuxer-rawvideo-w={W}", f"--demuxer-rawvideo-h={H}",
               "--demuxer-rawvideo-mp-format=yuv420p", f"--demuxer-rawvideo-fps={fps}",
               "--profile=low-latency", "--untimed", "--no-cache",
               "--really-quiet" if not VERBOSE else "--msg-level=all=warn",
               f"--input-conf={conf_path}", f"--input-ipc-server={sock_path}",
               "--title=Wyze Cams rtsp", "--force-window=immediate", "-"]

    bar = "=" * 64
    print(bar)
    print(f" Wyze grid: {n} camera(s), {cols}x{rows}")
    print(" Order (left-right, top-bottom): " + ", ".join(c["name"] for c in cams))
    print(bar)
    print(" " + KEY_HELP.replace("\n", "\n "))
    print(bar)
    print(f" Video: {fps} fps grid, {'GPU' if gpu_scale else 'CPU'} scaling.  "
          f"Auto-resync: {'every %s min per camera (staggered)' % resync_min if resync_s else 'off'}."
          "  A dark grey tile = that camera has no live video right now.")
    print(" Click the video window first so it receives the keys.")
    if rec or web_view or engine:
        print_extras(rec, web_view, n)
        print_detection(det, engine)
    print(bar)

    # Pre-render name labels (toggle with n)
    font_px = max(14, th // 18)
    labels = [render_label(c["name"], tw - 16, font_px) for c in cams]
    if any(l is None for l in labels):
        print(" (couldn't render name labels — is fontconfig installed? names hidden)")
    show_names = [all(l is not None for l in labels)]
    log(f"grid started: {', '.join(c['name'] for c in cams)}")

    mpv = subprocess.Popen(mpv_cmd, stdin=subprocess.PIPE)

    ipc = None
    pending = queue.Queue()      # layout keys are applied by the main loop, between frames

    def on_msg(args):
        cmd = args[0] if args else ""
        if cmd in ("select", "move", "hide", "showall", "zoom", "save", "rename"):
            pending.put(args)
        elif cmd == "connected":
            time.sleep(0.5)
            ipc.show(KEY_HELP, 9000)
        elif cmd == "help":
            ipc.show(KEY_HELP, 9000)
        elif cmd == "resync":
            log("resync requested (r key) — reconnecting all cameras")
            ipc.show("Resyncing all cameras…", 2500)
            for f in feeds:
                f.restart(planned=True)
        elif cmd == "names":
            if any(l is None for l in labels):
                ipc.show("Names unavailable (ffmpeg couldn't draw text)", 2500)
                return
            show_names[0] = not show_names[0]
            ipc.show(f"Names {'on' if show_names[0] else 'off'}", 1500)
    ipc = MpvIPC(sock_path, on_msg, stop)

    def shutdown(*_):
        stop_soon(stop)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ---- The output frame -------------------------------------------------------------
    # One W x H picture in YUV420 ("I420") layout, which is what mpv is told to expect:
    #   Y (brightness) plane : W*H bytes, one per pixel
    #   U and V (colour)     : (W/2)*(H/2) bytes each, one per 2x2 block of pixels
    # Each camera tile arrives in the same layout at tile size (tw x th), so painting a tile
    # is just copying its rows into the right place in each of the three planes.
    ysz, csz = W * H, (W // 2) * (H // 2)            # plane sizes of the whole grid
    frame = bytearray(ysz + 2 * csz)
    frame[ysz:] = b"\x80" * (2 * csz)                 # U=V=128 = no colour -> empty tiles are black
    tys, tcs = tw * th, (tw // 2) * (th // 2)        # plane sizes of one tile
    cw, ch, CW = tw // 2, th // 2, W // 2            # colour-plane tile width/height, grid width
    grey_row = b"\x80" * cw                          # a row of "no colour" for greyed tiles
    dim = bytes(i // 3 for i in range(256))          # brightness lookup: 1/3 for stale tiles
    origins = [((i % cols) * tw, (i // cols) * th) for i in range(n)]   # top-left of each tile

    def paint_tile(x0, y0, src, stale):
        """Copy one camera's YUV420 tile into the grid; stale tiles are drawn dark and grey."""
        for r in range(th):                                            # Y rows
            o = (y0 + r) * W + x0
            seg = src[r * tw:(r + 1) * tw]
            frame[o:o + tw] = seg.translate(dim) if stale else seg
        cx0, cy0 = x0 // 2, y0 // 2
        for plane in (0, 1):                                           # U rows, then V rows
            pbase, sbase = ysz + plane * csz, tys + plane * tcs
            for r in range(ch):
                o = pbase + (cy0 + r) * CW + cx0
                frame[o:o + cw] = grey_row if stale else src[sbase + r * cw:sbase + (r + 1) * cw]

    # Lookup tables for the translucent name box: darken brightness, fade colour toward grey.
    shade_y = bytes(int(16 + (v - 16) * LABEL_SHADE) if v > 16 else v for v in range(256))
    shade_c = bytes(int(128 + (v - 128) * LABEL_SHADE) for v in range(256))

    def paint_label(x0, y0, lab):
        """Draw a camera name at the tile's top-left: the video behind the box is darkened
        (so it stays visible but the text is readable), then the white text goes on top."""
        lw, lh, luma, spans = lab
        lx, ly = x0 + 8, y0 + 8
        for r in range(lh):                                     # brightness
            o = (ly + r) * W + lx
            frame[o:o + lw] = frame[o:o + lw].translate(shade_y)      # translucent box
            base = r * lw
            for a, b in spans[r]:                                     # solid text pixels
                frame[o + a:o + b] = luma[base + a:base + b]
        lcw, lch = lw // 2, lh // 2
        for plane in (0, 1):                                    # colour: fade toward grey
            pbase = ysz + plane * csz
            for r in range(lch):
                o = pbase + (ly // 2 + r) * CW + lx // 2
                frame[o:o + lcw] = frame[o:o + lcw].translate(shade_c)

    def paint_border(x0, y0):
        """Yellow frame around the selected tile (4 px)."""
        b = 4
        yel_y, yel_u, yel_v = b"\xd2", b"\x10", b"\x92"
        for r in list(range(b)) + list(range(th - b, th)):            # top + bottom rows
            o = (y0 + r) * W + x0
            frame[o:o + tw] = yel_y * tw
        for r in range(b, th - b):                                     # left + right edges
            o = (y0 + r) * W + x0
            frame[o:o + b] = yel_y * b
            frame[o + tw - b:o + tw] = yel_y * b
        cb = b // 2
        for plane, val in ((0, yel_u), (1, yel_v)):
            pbase = ysz + plane * csz
            for r in range(ch):
                o = pbase + (y0 // 2 + r) * CW + x0 // 2
                if r < cb or r >= ch - cb:
                    frame[o:o + cw] = val * cw
                else:
                    frame[o:o + cb] = val * cb
                    frame[o + cw - cb:o + cw] = val * cb

    def yuv_of(hexcol):
        r, g, b = (int(hexcol[i:i + 2], 16) for i in (1, 3, 5))
        y = 16 + (65.481 * r + 128.553 * g + 24.966 * b) / 255
        u = 128 + (-37.797 * r - 74.203 * g + 112.0 * b) / 255
        v = 128 + (112.0 * r - 93.786 * g - 18.214 * b) / 255
        return bytes([int(y)]), bytes([int(u)]), bytes([int(v)])

    def paint_dets(x0, y0, dets):
        """Outline each detected object on its tile, in its label's colour (4 px)."""
        b = 4
        for label, _score, fx1, fy1, fx2, fy2 in dets:
            cy, cu, cv = yuv_of(label_color(label))
            x1 = x0 + max(0, min(tw - b, int(fx1 * tw))) // 2 * 2
            y1 = y0 + max(0, min(th - b, int(fy1 * th))) // 2 * 2
            x2 = x0 + max(b, min(tw, int(fx2 * tw))) // 2 * 2
            y2 = y0 + max(b, min(th, int(fy2 * th))) // 2 * 2
            if x2 - x1 < 2 * b or y2 - y1 < 2 * b:
                continue
            w = x2 - x1
            for r in list(range(y1, y1 + b)) + list(range(y2 - b, y2)):      # top + bottom
                o = r * W + x1
                frame[o:o + w] = cy * w
            for r in range(y1 + b, y2 - b):                                    # left + right
                o = r * W
                frame[o + x1:o + x1 + b] = cy * b
                frame[o + x2 - b:o + x2] = cy * b
            cw2, cb = w // 2, b // 2
            for plane, val in ((0, cu), (1, cv)):
                pbase = ysz + plane * csz
                for r in range(y1 // 2, y2 // 2):
                    o = pbase + r * CW
                    if r < y1 // 2 + cb or r >= y2 // 2 - cb:
                        frame[o + x1 // 2:o + x1 // 2 + cw2] = val * cw2
                    else:
                        frame[o + x1 // 2:o + x1 // 2 + cb] = val * cb
                        frame[o + x2 // 2 - cb:o + x2 // 2] = val * cb

    # ---- Live layout: which camera sits in which slot ------------------------------------
    view = list(range(n))         # feed index shown in each slot, left-right, top-bottom
    hidden = []                   # feed indices hidden with d (kept in hiding order)
    sel = [None]                  # selected slot (1-9 keys), or None
    zoomed = [False]
    renaming = [False]
    layout_ver = [0]

    def relayout():
        """Clear the canvas and force every tile to repaint at its new position."""
        frame[:ysz] = b"\x10" * ysz
        frame[ysz:] = b"\x80" * (2 * csz)
        layout_ver[0] += 1

    def zoom_to(slot):
        x0, y0 = origins[slot]
        ipc.command("vf", "remove", "@zoom")
        ipc.command("vf", "add", f"@zoom:crop=w={tw}:h={th}:x={x0}:y={y0}")

    def handle(args):
        cmd = args[0]
        if cmd == "select":
            k = int(args[1]) if len(args) > 1 else 0
            if k == 0 or k > len(view) or sel[0] == k - 1:
                sel[0] = None
                ipc.show("No camera selected", 1200)
            else:
                sel[0] = k - 1
                ipc.show(f"{cams[view[k - 1]]['name']} selected — arrows move, d hides, e renames, z zooms", 4000)
                if zoomed[0]:
                    zoom_to(sel[0])
        elif cmd in ("move", "hide", "rename") and sel[0] is None:
            ipc.show("Press 1-9 to pick a camera first", 2000)
        elif cmd == "move":
            step = {"left": -1, "right": 1, "up": -cols, "down": cols}[args[1]]
            t = sel[0] + step
            if 0 <= t < len(view) and not (step in (-1, 1) and t // cols != sel[0] // cols):
                view[sel[0]], view[t] = view[t], view[sel[0]]
                sel[0] = t
                relayout()
        elif cmd == "hide":
            fi = view.pop(sel[0])
            hidden.append(fi)
            if not (feeds[fi].rec_root or feeds[fi].web or feeds[fi].detect):  # keep recording/serving/detecting
                feeds[fi].paused = True
                feeds[fi].restart(planned=True)            # disconnect it while hidden
            sel[0] = None
            relayout()
            ipc.show(f"Hidden: {cams[fi]['name']}  (a = show all, s = save)", 2500)
        elif cmd == "showall":
            if not hidden:
                ipc.show("All cameras are already shown", 1500)
                return
            for fi in hidden:
                feeds[fi].paused = False
                view.append(fi)
            ipc.show(f"Showing {len(hidden)} hidden camera(s) again", 2000)
            hidden.clear()
            relayout()
        elif cmd == "rename":
            if renaming[0]:
                ipc.show("Finish the open rename first", 2000)
                return
            fi = view[sel[0]]
            renaming[0] = True

            def ask():                                  # runs in its own thread: never stalls video
                try:
                    new = ask_name(cams[fi]["name"], cams[fi]["ip"], ipc)
                    if new and new != cams[fi]["name"]:
                        pending.put(("named", fi, new, render_label(new, tw - 16, font_px)))
                    else:
                        ipc.show("Rename cancelled", 1500)
                finally:
                    renaming[0] = False
            threading.Thread(target=ask, daemon=True).start()
        elif cmd == "named":                            # the dialog came back with a name
            fi, new, lab = args[1], args[2], args[3]
            old = cams[fi]["name"]
            cams[fi]["name"] = feeds[fi].name = new
            labels[fi] = lab
            c = cfg_by_ip.get(cams[fi]["ip"])
            if c is not None:
                c["name"] = new
                save(cfg)                               # a rename is always saved right away
            layout_ver[0] += 1                          # repaint so the new label shows
            log(f"renamed camera {cams[fi]['ip']}: {old} -> {new}")
            ipc.show(f"Renamed: {old} → {new}", 2500)
        elif cmd == "zoom":
            if zoomed[0]:
                ipc.command("vf", "remove", "@zoom")
                zoomed[0] = False
            elif view:
                zoom_to(sel[0] if sel[0] is not None else 0)
                zoomed[0] = True
                ipc.show("Zoomed — z to return to the grid", 1500)
        elif cmd == "save":
            if not cfg_by_ip:
                ipc.show("Nothing to save (no cameras.json)", 2000)
                return
            shown_cfg = [cfg_by_ip[cams[fi]["ip"]] for fi in view if cams[fi]["ip"] in cfg_by_ip]
            hid_cfg = [cfg_by_ip[cams[fi]["ip"]] for fi in hidden if cams[fi]["ip"] in cfg_by_ip]
            for c in shown_cfg:
                c["enabled"] = True
            for c in hid_cfg:
                c["enabled"] = False
            rest = [c for c in cfg["cameras"] if c not in shown_cfg and c not in hid_cfg]
            cfg["cameras"] = shown_cfg + rest + hid_cfg
            save(cfg)
            log("layout saved from the grid: " + ", ".join(c["name"] for c in shown_cfg))
            ipc.show("Layout saved — it will be used next time", 2000)

    interval = 1.0 / fps
    nxt = time.time()
    last_status = time.time()
    was_stale = [False] * n
    painted = [None] * n          # per slot: what was last painted there
    try:
        while not stop.is_set() and mpv.poll() is None:
            while not pending.empty():                           # keys pressed since last frame
                handle(pending.get_nowait())
            now = time.time()
            watchdog_tick(feeds, next_resync, resync_s, now)

            # ---- paint tiles: only those whose picture, grey state, label, position or ----
            # ---- selection changed since the last paint                                  ----
            for slot, i in enumerate(view):
                f = feeds[i]
                x0, y0 = origins[slot]
                src = f.frame
                stale = now - f.last > STALL_SECS
                if src is not None and stale != was_stale[i]:
                    was_stale[i] = stale
                    log(f"[{f.name}] tile greyed — no live video" if stale else f"[{f.name}] live again")
                state = (i, f.frames, stale, show_names[0], layout_ver[0], sel[0] == slot, f.dets_ver)
                if state == painted[slot]:
                    continue                                     # nothing new since last paint
                painted[slot] = state
                if src is not None:
                    paint_tile(x0, y0, src, stale)
                    if f.dets and not stale:
                        paint_dets(x0, y0, f.dets)
                if show_names[0] and labels[i]:
                    paint_label(x0, y0, labels[i])
                if sel[0] == slot:
                    paint_border(x0, y0)

            try:
                mpv.stdin.write(frame)
            except (BrokenPipeError, OSError):
                break

            save_if_needed(feeds, cfg, cfg_by_ip)
            if now - last_status > 600:                          # health summary every 10 min
                last_status = now
                log_status(feeds, now)

            nxt += interval
            delay = nxt - time.time()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.time()
    finally:
        stop.set()
        for f in feeds:
            stop_ffmpeg(f.proc)
        if web_view:
            web_view.close()
        try:
            mpv.stdin.close()
        except OSError:
            pass
        if mpv.poll() is None:
            mpv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
        log("grid stopped")


def play(args, password, cams=None):
    if args.command != "serve":
        need("mpv")
    cfg = load_saved()
    if cams is None:
        cams = cfg.get("cameras", [])
        if not cams:
            die(f"no saved cameras — run:  {RUN} --scan")
    user = args.user
    if args.cam:                            # explicit choice wins, even over "enabled": false
        enabled, disabled = match_cams(cams, args.cam), []
    else:
        enabled = [c for c in cams if c.get("enabled", True)]
        disabled = [c for c in cams if not c.get("enabled", True)]
    gone = match_cams(cams, args.exclude) if args.exclude else []
    enabled = [c for c in enabled if c not in gone]
    if args.working_only:
        play_list = [c for c in enabled if c.get("ok", True)]
        skipped = [c for c in enabled if not c.get("ok", True)]
    else:
        play_list, skipped = enabled, []
    print(f"Playing {len(play_list)} of {len(cams)} saved camera(s):")
    for c in play_list:
        print(f"  + {c['name']:<14} {c['ip']}")
    for c in skipped:
        print(f"  - {c['name']:<14} {c['ip']}  skipped: scan check failed ({c.get('error') or 'unknown'})")
    for c in disabled:
        print(f"  - {c['name']:<14} {c['ip']}  skipped: enabled=false in cameras.json")
    for c in cams:
        if c not in play_list and c not in skipped and c not in disabled:
            print(f"  - {c['name']:<14} {c['ip']}  {'excluded (-x)' if c in gone else 'not selected (-c)'}")
    cams = play_list
    if not cams:
        die("no cameras to play (check -c/-x, the scan results, or the password)")
    global VERBOSE
    VERBOSE = args.verbose
    if args.verbose:
        COMMON[:] = [o for o in COMMON if o != "--really-quiet"] + ["--msg-level=all=warn"]
    path = SUB if args.sub else MAIN
    rec = None
    if args.record:
        need("ffmpeg")
        rec = {"dir": args.record, "keep": args.keep, "segment": max(1, args.segment) * 60,
               "format": args.rec_format}
        check_rec_dir(rec)
    web = None
    if args.web is not None:
        web = {"port": args.web, "bind": args.web_bind}
    det = None
    if args.detect:
        need("ffmpeg")
        need_ai()
        labels = {t.strip().lower().replace(" ", "_") for t in args.detect_labels.split(",") if t.strip()}
        model = get_model(args.detect_model)
        detector = Detector(model)                 # load now: fail early if the model is bad
        known = set(detector.labels)
        unknown = labels - known
        if unknown:
            die(f"this model doesn't know: {', '.join(sorted(unknown))}. It knows: "
                + ", ".join(sorted(known)) + ("\n       (packages need a package-trained model: "
                                               "--detect-model your_model.onnx)"
                                               if "package" in unknown else ""))
        det_cams = {c["ip"] for c in match_cams(cams, args.detect_cams)} if args.detect_cams else set()
        fps = max(0.2, min(10.0, args.detect_fps))
        det = {"model": model, "detector": detector, "labels": labels, "score": args.detect_score, "fps": fps,
               "every": max(1, round(20 / fps)), "webhook": args.detect_webhook,
               "cams": det_cams, "keep": args.detect_keep,
               "events_dir": Path(args.detect_dir).expanduser() if args.detect_dir
               else APP_DIR / "events"}
    if args.command == "serve":
        if not rec and not web:
            web = {"port": WEB_PORT, "bind": args.web_bind}   # serve alone = browser view
        run_headless(cams, user, password, path, rec, web,
                     resync_min=args.resync if args.resync is not None else 0, det=det)
    elif args.windows:
        if rec or web or det:
            die("--record / --web / --detect work with the grid or 'serve', not --windows")
        play_windows(cams, user, password, path)
    else:
        play_mosaic(cams, user, password, path, fps=args.fps,
                    resync_min=args.resync if args.resync is not None else 10,
                    gpu_scale=args.gpu, rec=rec, web=web, det=det)


# --------------------------------------------------------------------------- Home Assistant
def export_ha(args):
    """Print what to type into Home Assistant for each camera (no login needed)."""
    cfg = load_saved()
    cams = cfg.get("cameras", [])
    if not cams:
        die(f"no saved cameras — run:  {RUN} --scan")
    cams = match_cams(cams, args.cam) if args.cam else [c for c in cams if c.get("enabled", True)]
    if args.exclude:
        gone = match_cams(cams, args.exclude)
        cams = [c for c in cams if c not in gone]
    user = quote(cfg.get("user") or "USERNAME", safe="")
    host = local_ip() or "THIS-PC-IP"
    port = args.web or WEB_PORT
    ids = cam_ids(cams)
    print("Home Assistant setup, one camera at a time.\n")
    print("OPTION A — straight from the camera (no need to keep this script running):")
    print("  Settings > Devices & services > Add integration > Generic Camera, then fill in:")
    print("    Stream source URL : (below; replace PASSWORD with your RTSP password)")
    print("    Still image URL   : leave empty")
    print("    RTSP transport    : TCP")
    print("    Verify SSL        : OFF  (Wyze cams use a self-signed certificate)\n")
    for c in cams:
        path = c.get("stream") or MAIN
        print(f"  {c['name']:<20} rtsps://{user}:PASSWORD@{c['ip']}:{c.get('port', DEFAULT_PORT)}/{path}")
    print("\n  Tip: each viewer is one more camera session. If a camera starts refusing")
    print("  connections, use OPTION B for it instead.\n")
    print(f"OPTION B — through this script's browser view ({RUN} serve, or --web):")
    print("  Uses the script's existing camera session, so it adds no load on the camera.")
    print("  Settings > Devices & services > Add integration > MJPEG IP Camera, then fill in:")
    print("    Username / Password : your RTSP username and password\n")
    for i, c in zip(ids, cams):
        print(f"  {c['name']:<20} MJPEG URL:      http://{host}:{port}/cam/{i}.mjpg")
        print(f"  {'':<20} Still image URL: http://{host}:{port}/cam/{i}.jpg")
    print(f"\n  ({host} is this computer's address; use its fixed/reserved IP if it has one.)")


# --------------------------------------------------------------------------- test
ERR_PAT = re.compile(r"error while decoding|concealing|corrupt|decode_slice|Invalid NAL|missing picture|"
                     r"Missing reference|co located POCs", re.I)


def measure(url, hwaccel, secs=12, w=320, h=180):
    """Decode a stream for `secs` seconds through the same pipeline the grid uses
    (raw frames on a pipe — Wyze streams have no timestamps, so ffmpeg's own
    counters can't be trusted). Returns (fps, decode_errors, note)."""
    cmd = ["ffmpeg", "-loglevel", "warning", *input_args(url, hwaccel),
           "-an", "-vf", f"scale={w}:{h}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    p = popen_ffmpeg(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    size = w * h * 3
    times, errs, last_err = [], [0], [""]

    def read_out():
        buf = 0
        while True:
            chunk = p.stdout.read(65536)
            if not chunk:
                return
            buf += len(chunk)
            while buf >= size:
                buf -= size
                times.append(time.time())

    def read_err():
        for raw in p.stderr:
            line = raw.decode(errors="replace").strip()
            if ERR_PAT.search(line):
                errs[0] += 1
            elif line:
                last_err[0] = redact(line)

    ts = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for t in ts:
        t.start()
    try:
        p.wait(timeout=secs)
    except subprocess.TimeoutExpired:
        stop_ffmpeg(p)
    for t in ts:
        t.join(timeout=2)
    if len(times) < 2:
        return 0.0, errs[0], last_err[0] or "no video within the time limit"
    fps = (len(times) - 1) / (times[-1] - times[0]) if times[-1] > times[0] else 0.0
    return fps, errs[0], ""


def find_cam(cams, target):
    t = target.lower()
    for c in cams:
        if t in (c["name"].lower(), c["ip"], c["ip"].rsplit(".", 1)[1]):
            return c
    die(f"no saved camera matches '{target}' (use its name, IP, or last number of its IP)")


def match_cams(cams, spec):
    """Cameras matching a comma-separated list of names, IPs, last IP numbers, or partial names.
    Dies with the list of saved cameras if a term matches nothing."""
    picked = []
    for term in [t.strip() for t in spec.split(",") if t.strip()]:
        t = term.lower()
        exact = [c for c in cams if t in (c["name"].lower(), c["ip"], c["ip"].rsplit(".", 1)[1])]
        hits = exact or [c for c in cams if t in c["name"].lower()]
        if not hits:
            names = ", ".join(f"{c['name']} ({c['ip'].rsplit('.', 1)[1]})" for c in cams)
            die(f"no saved camera matches '{term}'. Saved cameras: {names}")
        picked += [c for c in hits if c not in picked]
    return picked


def list_cams():
    cams = load_saved().get("cameras", [])
    if not cams:
        print(f"No saved cameras yet — run:  {RUN} --scan")
        return
    print(f"{'NAME':<20} {'IP':<15} {'STREAM':<8} {'SHOWN':<6} LAST SCAN")
    for c in cams:
        print(f"{c['name']:<20} {c['ip']:<15} {c.get('stream') or 'stream0':<8} "
              f"{'yes' if c.get('enabled', True) else 'no':<6} {'ok' if c.get('ok', True) else 'failed'}")


def test_camera(args, password):
    need("ffmpeg")
    if not args.target:
        die(f"which camera? e.g.  {RUN} test 243   or   {RUN} test \"Front Door\"")
    cfg = load_saved()
    cam = find_cam(cfg.get("cameras", []), args.target)
    print(f"Testing {cam['name']} ({cam['ip']}) — 4 runs of ~12s each, please wait ...\n")
    results = []
    first = True
    for path in (MAIN, SUB):
        for hw in (True, False):
            if not first:
                time.sleep(3)          # let the camera release the previous session
            first = False
            url = rtsp_url(cam["ip"], cam["port"], args.user, password, path)
            fps, errs, note = measure(url, hw)
            label = f"{path:<8} {'GPU (VA-API)' if hw else 'CPU':<13}"
            print(f"  {label} {fps:5.1f} fps   {errs:3d} decode errors   {note}")
            results.append({"stream": path, "hwaccel": hw, "fps": fps, "errs": errs})

    good = [r for r in results if r["fps"] >= 12 and r["errs"] == 0]
    if good:
        best = good[0]                                        # order prefers main stream + GPU
    else:
        working = [r for r in results if r["fps"] > 0]
        if not working:
            die("no combination produced video. If you see 'End of file' / 'Connection reset', the camera "
                "is refusing new sessions: close other viewers, wait ~2 minutes (or restart the camera "
                "in the Wyze app), then test again")
        best = min(working, key=lambda r: (r["errs"], -r["fps"]))

    cam["stream"], cam["hwaccel"] = best["stream"], best["hwaccel"]
    save(cfg)
    print(f"\nSaved for {cam['name']}: stream={best['stream']}, "
          f"decoder={'GPU (VA-API)' if best['hwaccel'] else 'CPU'}  "
          f"({best['fps']:.1f} fps, {best['errs']} errors)")
    if best["errs"] > 0:
        print("Still some errors with every setting: the camera's Wi-Fi link is the bottleneck. "
              "A closer access point or extender is the real fix.")


# --------------------------------------------------------------------------- name
def name_cameras(args, password):
    """Walk through each saved camera: show it live in a small window, ask for a name."""
    need("mpv")
    cfg = load_saved()
    cams = cfg.get("cameras", [])
    if not cams:
        die(f"no saved cameras — run:  {RUN} --scan")
    if args.target:
        cams = [find_cam(cams, args.target)]
    print("Each camera opens in a small window. Type a name and press Enter")
    print("(Enter alone keeps the current name, '-' hides the camera from the grid).\n")
    for c in cams:
        url = rtsp_url(c["ip"], c["port"], args.user, password, c.get("stream") or SUB)
        viewer = subprocess.Popen(["mpv", "--no-audio", "--profile=low-latency", "--really-quiet",
                                   "--geometry=640x360", "--autofit=640x360", "--ontop",
                                   f"--title=Which camera is this?  ({c['ip']})", url],
                                  stdin=subprocess.DEVNULL)
        try:
            ans = input(f"  {c['ip']:<15} name [{c['name']}]: ").strip()
        except EOFError:
            ans = ""
        finally:
            viewer.terminate()
            try:
                viewer.wait(timeout=3)
            except subprocess.TimeoutExpired:
                viewer.kill()
        if ans == "-":
            c["enabled"] = False
            print("    hidden from the grid (set \"enabled\": true in cameras.json to bring it back)")
        elif ans:
            c["name"] = ans
            c["enabled"] = True
    save(cfg)
    print(f"\nSaved to {CONF_FILE}")


# --------------------------------------------------------------------------- arrange
def arrange_cameras(args):
    """Set the grid order and which cameras are shown, in one step."""
    cfg = load_saved()
    cams = cfg.get("cameras", [])
    if not cams:
        die(f"no saved cameras — run:  {RUN} --scan")
    while True:
        print("\nCurrent grid (left-right, top-bottom):")
        shown = [c for c in cams if c.get("enabled", True)]
        hidden = [c for c in cams if not c.get("enabled", True)]
        for i, c in enumerate(cams, 1):
            mark = "shown " if c.get("enabled", True) else "HIDDEN"
            print(f"  {i:>2}. {mark}  {c['name']:<20} {c['ip']}")
        print(f"\n{len(shown)} shown, {len(hidden)} hidden.")
        print("Type the numbers of the cameras to SHOW, in the order you want them,")
        print("e.g.  3,1,2   (cameras you leave out are hidden, not deleted).")
        print("Enter alone = keep as is.   'all' = show every camera in the current order.")
        try:
            ans = input("> ").strip().lower()
        except EOFError:
            ans = ""
        if not ans:
            break
        if ans == "all":
            for c in cams:
                c["enabled"] = True
            continue
        try:
            nums = [int(x) for x in re.split(r"[,\s]+", ans) if x]
        except ValueError:
            print("  Please type numbers separated by commas, e.g. 3,1,2")
            continue
        bad = [x for x in nums if not 1 <= x <= len(cams)]
        if bad or len(set(nums)) != len(nums):
            print(f"  Use each number once, between 1 and {len(cams)}.")
            continue
        picked = [cams[x - 1] for x in nums]
        rest = [c for c in cams if c not in picked]
        for c in picked:
            c["enabled"] = True
        for c in rest:
            c["enabled"] = False
        cams[:] = picked + rest                   # shown first in your order, hidden after
    cfg["cameras"] = cams
    save(cfg)
    print(f"Saved to {CONF_FILE}. The grid will use this order and selection.")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        prog="wyzecam-rtsp.py", description="View your Wyze RTSP cameras in one live grid.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:" + __doc__.split("QUICK START")[1].split("OTHER COMMANDS")[0].rstrip())
    ap.add_argument("command", nargs="?",
                    choices=["play", "scan", "name", "arrange", "test", "serve", "ha", "all"],
                    default="play", help="play (default) | scan | name | arrange | test | "
                                         "serve (no window: --record/--web) | ha (Home Assistant "
                                         "setup) (all = scan then play)")
    ap.add_argument("target", nargs="?", help="camera for 'test'/'name': name, IP, or last number of the IP")
    ap.add_argument("-c", "--cam", metavar="LIST",
                    help="only these cameras: comma-separated names, IPs, or last IP numbers")
    ap.add_argument("-x", "--exclude", metavar="LIST", help="hide these cameras (same format as --cam)")
    ap.add_argument("--scan", action="store_true", help="rescan the network before playing")
    ap.add_argument("--list", action="store_true", help="list saved cameras and exit")
    ap.add_argument("--working-only", action="store_true",
                    help="skip cameras whose last scan check failed (default: try them all)")
    ap.add_argument("--resync", type=int, default=None, metavar="MIN",
                    help="reconnect each camera every MIN minutes (staggered) to stay live; 0 = off "
                         "(default 10 in the grid, off for 'serve')")
    ap.add_argument("--record", metavar="DIR",
                    help="also record every camera into DIR (e.g. a mounted NAS share), "
                         "one folder per camera")
    ap.add_argument("--keep", type=int, default=REC_KEEP_DAYS, metavar="DAYS",
                    help=f"delete recordings older than DAYS (default {REC_KEEP_DAYS}, 0 = keep forever)")
    ap.add_argument("--segment", type=int, default=REC_SEGMENT_MIN, metavar="MIN",
                    help=f"length of each recording file in minutes (default {REC_SEGMENT_MIN})")
    ap.add_argument("--rec-format", choices=["mkv", "mp4"], default="mkv",
                    help="recording file type: mkv (default; survives crashes, plays in VLC/mpv) or "
                         "mp4 (fragmented; also plays in browsers, phones and Home Assistant)")
    ap.add_argument("--web", type=int, nargs="?", const=WEB_PORT, metavar="PORT",
                    help=f"also serve the browser view on PORT (default {WEB_PORT})")
    ap.add_argument("--detect", action="store_true",
                    help="local AI detection of people and pets (needs: pip install onnxruntime numpy)")
    ap.add_argument("--detect-labels", default=DET_LABELS, metavar="LIST",
                    help=f"what to detect (default {DET_LABELS}; e.g. add car,bird,truck)")
    ap.add_argument("--detect-model", default="tiny", metavar="MODEL",
                    help="nano (fastest), tiny (default), s (most accurate), or a path to your "
                         "own ONNX model (YOLOX or YOLOv8/v9 layout; class names in <model>.txt)")
    ap.add_argument("--detect-cams", metavar="LIST", help="detect only on these cameras (default: all)")
    ap.add_argument("--detect-fps", type=float, default=DET_FPS, metavar="N",
                    help=f"checks per second per camera (default {DET_FPS})")
    ap.add_argument("--detect-score", type=float, default=DET_SCORE, metavar="0-1",
                    help=f"minimum confidence (default {DET_SCORE})")
    ap.add_argument("--detect-webhook", metavar="URL",
                    help="POST each new detection as JSON to URL (e.g. a Home Assistant webhook)")
    ap.add_argument("--detect-dir", metavar="DIR",
                    help="where snapshots and events.jsonl go (default: events/ in the app folder)")
    ap.add_argument("--detect-keep", type=int, default=DET_KEEP_DAYS, metavar="DAYS",
                    help=f"keep snapshots/events this many days (default {DET_KEEP_DAYS}, 0 = forever)")
    ap.add_argument("--web-bind", default="0.0.0.0", metavar="ADDR",
                    help="address for the browser view (default 0.0.0.0 = your whole LAN; "
                         "127.0.0.1 = this computer only)")
    ap.add_argument("--sub", action="store_true", help="use the lighter substream for every camera")
    ap.add_argument("--fps", type=int, default=15, choices=[10, 15, 20], help="grid frame rate (default 15)")
    ap.add_argument("--windows", action="store_true", help="one window per camera instead of one grid")
    ap.add_argument("--gpu", action="store_true",
                    help="GPU scaling via VA-API video processing (falls back per camera if unsupported)")
    ap.add_argument("--verbose", action="store_true", help="show mpv/ffmpeg warnings")
    ap.add_argument("--subnet", help="subnet to scan, e.g. 192.168.1.0/24 (default: auto-detect)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"RTSPS port (default {DEFAULT_PORT})")
    ap.add_argument("--all", action="store_true", help=argparse.SUPPRESS)   # old flag; now the default
    args = ap.parse_args()

    APP_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(APP_DIR)

    if args.list:                       # needs no login
        list_cams()
        return
    if args.command == "arrange":       # needs no login
        arrange_cameras(args)
        return
    if args.command == "ha":            # needs no login
        export_ha(args)
        return

    args.user = os.environ.get("WYZE_RTSP_USER")
    if not args.user:
        try:
            hint = f" [{DEFAULT_USER}]" if DEFAULT_USER else ""
            args.user = input(f"RTSP username{hint}: ").strip() or DEFAULT_USER
        except EOFError:
            args.user = DEFAULT_USER
    password = os.environ.get("WYZE_RTSP_PASS")
    if not password:
        try:
            hint = " [Enter = default]" if DEFAULT_PASS else ""
            password = getpass.getpass(f"RTSP password{hint}: ") or DEFAULT_PASS
        except EOFError:
            password = DEFAULT_PASS

    if args.command == "scan":
        scan(args, password)
    elif args.command == "test":
        test_camera(args, password)
    elif args.command == "name":
        name_cameras(args, password)
    else:                               # play (default) or all
        first_run = not load_saved().get("cameras")
        if args.scan or args.command == "all" or first_run:
            if first_run and not args.scan:
                print("No saved cameras yet — scanning the network first.")
            if not scan(args, password):
                return
            print()
        play(args, password)

if __name__ == "__main__":
    main()
