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

RECORDING AND BROWSER VIEW (grid or 'serve'; each camera still uses ONE camera session)
  --record DIR       record every camera into DIR/<camera name>/ (.mkv, video copied as-is)
  --keep DAYS        delete recordings older than DAYS (default 7, 0 = keep forever)
  --segment MIN      minutes per recording file (default 5)
  --web [PORT]       browser view at http://this-pc:PORT/ (default 8080), RTSP login required
  --web-bind ADDR    127.0.0.1 = only this computer (default: whole LAN)

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
Needs: python3, ffmpeg (ffmpeg + ffprobe), mpv.
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
# Recording (--record): video is copied as-is (no re-encoding) into .mkv files of this length.
REC_SEGMENT_MIN = 5
REC_KEEP_DAYS = 7       # recordings older than this are deleted (--keep, 0 = keep forever)
# Browser view (--web): JPEG snapshots, at most this wide, of every Nth camera frame.
WEB_PORT = 8080
WEB_WIDTH = 960
WEB_EVERY = 2           # Wyze cams send ~20 fps -> ~10 fps in the browser
WEB_QUALITY = 6         # JPEG quality: 2 (best, biggest) .. 31 (worst, smallest)


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
        stop.set()
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
                      tiles=True, rec_pattern=None, rec_segment=300, jpeg_port=None):
    """One ffmpeg per camera, i.e. ONE camera session, with up to three outputs:
      tiles       decode, keep every Nth frame (counted, since Wyze streams have no usable
                  timestamps), scale to the tile, raw YUV420 frames on stdout (the grid)
      rec_pattern copy the video as-is (no re-encoding) into clock-aligned .mkv segments
      jpeg_port   JPEG snapshots for the browser view, sent to a local TCP port
    gpu_scale: decode AND shrink on the GPU (VA-API), copy only the small tile back."""
    gpu = hwaccel and gpu_scale and IS_LINUX and jpeg_port is None   # VA-API (Linux) only
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
                "-segment_format", "matroska", "-reset_timestamps", "1", "-strftime", "1",
                rec_pattern]
    if jpeg_port:
        cmd += ["-map", "0:v:0", "-an",
                # fresh, strictly increasing timestamps: the JPEG encoder rejects repeats
                "-vf", f"select='not(mod(n\\,{WEB_EVERY}))',settb=1/1000,setpts=N*100,"
                       f"scale='min({WEB_WIDTH},iw)':-2",
                "-fps_mode", "passthrough", "-pix_fmt", "yuvj420p", "-c:v", "mjpeg",
                "-q:v", str(WEB_QUALITY), "-f", "image2pipe", f"tcp://127.0.0.1:{jpeg_port}"]
    if not tiles and not rec_pattern and not jpeg_port:
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
                 gpu_scale=False, tiles=True, rec_root=None, rec_segment=300, web=False):
        super().__init__(daemon=True)
        self.tiles = tiles         # False = headless (no grid): no raw frames are decoded for us
        self.rec_root = rec_root   # Path: record into rec_root/<camera name>/
        self.rec_segment = rec_segment
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
        return str(folder / f"{safe_name(self.name)}_%Y-%m-%d_%H-%M-%S.mkv")

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

    def _progress_reader(self, out, first_frame):
        """Headless: ffmpeg's -progress lines ('frame=123') tell us video is still arriving."""
        last_n = 0
        for raw in out:
            line = raw.decode(errors="replace").strip()
            if line.startswith("frame="):
                try:
                    n = int(line[6:])
                except ValueError:
                    continue
                if n > last_n:
                    self.frames += n - last_n
                    last_n = n
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
            listener, jt = None, None
            if self.web:                   # local port for ffmpeg's JPEG output
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                jt = threading.Thread(target=self._jpeg_reader, args=(listener,), daemon=True)
                jt.start()
            self.proc = popen_ffmpeg(
                camera_ffmpeg_cmd(self.url(), self.w, self.h, self.hwaccel, self.keep_every,
                                  self.gpu_scale, tiles=self.tiles, rec_pattern=self.rec_pattern(),
                                  rec_segment=self.rec_segment,
                                  jpeg_port=listener.getsockname()[1] if listener else None),
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
                if listener is not None:
                    try:
                        listener.close()       # unblocks accept() if ffmpeg never connected
                    except OSError:
                        pass
                    jt.join(timeout=2)
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
    Only touches .mkv files this tool made, one folder down from the recording folder."""
    def __init__(self, root, keep_days, stop):
        super().__init__(daemon=True)
        self.root, self.keep, self.stop = Path(root), keep_days, stop

    def run(self):
        while not self.stop.is_set():
            cutoff = time.time() - self.keep * 86400
            removed = 0
            for f in self.root.glob("*/*_????-??-??_??-??-??.mkv"):
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
</style></head><body>
<div id="grid"></div>
<div id="bar">click a camera to zoom · n = names · f = fullscreen</div>
<script>
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function feed(cam, tile, img) {
  let seq = 0, url = null;
  for (;;) {
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
fetch("api/cams", {cache: "no-store"}).then(r => r.json()).then(cams => {
  const grid = document.getElementById("grid");
  grid.style.setProperty("--cols", Math.ceil(Math.sqrt(cams.length)) || 1);
  for (const cam of cams) {
    const tile = document.createElement("div"), img = new Image(), name = document.createElement("div");
    tile.className = "tile down"; name.className = "name"; name.textContent = cam.name;
    tile.title = cam.name; tile.append(img, name); grid.append(tile);
    tile.onclick = () => tile.classList.toggle("zoom");
    feed(cam, tile, img);
  }
});
document.addEventListener("keydown", e => {
  if (e.key === "n") document.body.classList.toggle("names-off");
  if (e.key === "f") document.fullscreenElement ? document.exitFullscreen()
                                                 : document.documentElement.requestFullscreen();
  if (e.key === "Escape") document.querySelectorAll(".zoom").forEach(t => t.classList.remove("zoom"));
});
</script></body></html>
"""


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
    Protected by the RTSP username/password (HTTP Basic auth)."""
    def __init__(self, feeds, port, bind, user, password, stop):
        import base64
        import hmac
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs, urlsplit
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
                             "live": bool(f.jpeg_time and now - f.jpeg_time < STALL_SECS)}
                            for i, f in by_id.items()]
                    return self.send(200, json.dumps(data).encode(), "application/json")
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


def start_extras(feeds, rec, web, user, password, stop):
    """Start the recording pruner and the browser view (shared by the grid and 'serve')."""
    web_view = None
    if rec and rec["keep"] > 0:
        Pruner(rec["dir"], rec["keep"], stop).start()
    if web:
        web_view = WebView(feeds, web["port"], web["bind"], user, password, stop)
    return web_view


def print_extras(rec, web_view, n):
    if rec:
        print(f" Recording {n} camera(s) to {rec['dir']}  ({rec['segment'] // 60}-min files, "
              f"{'kept %s days' % rec['keep'] if rec['keep'] > 0 else 'kept forever'})")
    if web_view:
        print(f" Browser view: {web_view.urls()}  (log in with your RTSP username/password)")


def run_headless(cams, user, password, path, rec=None, web=None, resync_min=0):
    """'serve': no window. Record and/or serve the browser view, e.g. on a server or in a
    container. Each camera still has its own ffmpeg (one camera session) and reconnect loop."""
    need("ffmpeg")
    stop = threading.Event()
    cfg = load_saved()
    cfg_by_ip = {c["ip"]: c for c in cfg.get("cameras", [])}
    feeds = [CameraFeed(c, user, password, path, 0, 0, stop, 1, cfg_by_ip.get(c["ip"]),
                        tiles=False, rec_root=rec and rec["dir"],
                        rec_segment=rec["segment"] if rec else 300, web=bool(web)) for c in cams]
    web_view = start_extras(feeds, rec, web, user, password, stop)
    for f in feeds:
        f.start()
    n = len(feeds)
    resync_s = resync_min * 60 if resync_min > 0 else 0
    t0 = time.time()
    next_resync = [t0 + resync_s + i * resync_s / n if resync_s else float("inf") for i in range(n)]

    def shutdown(*_):
        stop.set()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    bar = "=" * 64
    print(bar)
    print(f" Serving {n} camera(s) with no window: " + ", ".join(c["name"] for c in cams))
    print_extras(rec, web_view, n)
    print(" Press Ctrl+C to stop.")
    print(bar)
    log(f"serve started: {', '.join(c['name'] for c in cams)}"
        + (f"; recording to {rec['dir']}" if rec else "")
        + (f"; browser view on port {web['port']}" if web else ""))
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
            stop.wait(1)
    finally:
        stop.set()
        for f in feeds:
            stop_ffmpeg(f.proc)
        if web_view:
            web_view.close()
        log("serve stopped")


def play_mosaic(cams, user, password, path, fps=15, resync_min=15, gpu_scale=False, rec=None, web=None):
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
                        gpu_scale, rec_root=rec and rec["dir"],
                        rec_segment=rec["segment"] if rec else 300, web=bool(web)) for c in cams]
    web_view = start_extras(feeds, rec, web, user, password, stop)
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
    if rec or web_view:
        print_extras(rec, web_view, n)
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
        stop.set()
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
            if not (feeds[fi].rec_root or feeds[fi].web):   # recording / browser view: stay connected
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
                state = (i, f.frames, stale, show_names[0], layout_ver[0], sel[0] == slot)
                if state == painted[slot]:
                    continue                                     # nothing new since last paint
                painted[slot] = state
                if src is not None:
                    paint_tile(x0, y0, src, stale)
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
        rec = {"dir": args.record, "keep": args.keep, "segment": max(1, args.segment) * 60}
        check_rec_dir(rec)
    web = None
    if args.web is not None:
        web = {"port": args.web, "bind": args.web_bind}
    if args.command == "serve":
        if not rec and not web:
            web = {"port": WEB_PORT, "bind": args.web_bind}   # serve alone = browser view
        run_headless(cams, user, password, path, rec, web,
                     resync_min=args.resync if args.resync is not None else 0)
    elif args.windows:
        if rec or web:
            die("--record / --web work with the grid or 'serve', not --windows")
        play_windows(cams, user, password, path)
    else:
        play_mosaic(cams, user, password, path, fps=args.fps,
                    resync_min=args.resync if args.resync is not None else 10,
                    gpu_scale=args.gpu, rec=rec, web=web)


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
    ap.add_argument("--web", type=int, nargs="?", const=WEB_PORT, metavar="PORT",
                    help=f"also serve the browser view on PORT (default {WEB_PORT})")
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
