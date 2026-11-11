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


# --------------------------------------------------------------------------- helpers
def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def popen_ffmpeg(cmd, **kw):
    """Start ffmpeg so that stop_ffmpeg() can later stop it politely on any OS.
    On Windows that means giving it a stdin pipe (it quits cleanly when sent 'q')."""
    if IS_WIN:
        kw["stdin"] = subprocess.PIPE
    return subprocess.Popen(cmd, **kw)


def stop_ffmpeg(p, grace=3):
    """Stop ffmpeg politely so it sends RTSP TEARDOWN and the camera frees the session.
    A hard kill leaves the session open on the camera until it times out, and Wyze cams
    refuse new viewers once too many dead sessions pile up.
    Linux/macOS: SIGINT. Windows: has no SIGINT for child processes, so send ffmpeg's own
    'q' (quit) key on its stdin. Either way, kill it if it hasn't exited after `grace` s."""
    if p is None or p.poll() is not None:
        return
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


def input_args(url, hwaccel=True, gpu_frames=False):
    """ffmpeg input options shared by the grid and the 'test' command.
    gpu_frames keeps decoded frames on the GPU (for scaling there)."""
    a = ["-hide_banner"]
    if not IS_WIN:
        a.append("-nostdin")       # Windows keeps stdin: that's how we ask ffmpeg to quit ('q')
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


def camera_ffmpeg_cmd(url, tile_w, tile_h, hwaccel=True, keep_every=2, gpu_scale=False):
    """One ffmpeg per camera: decode, keep every Nth frame (counted, since Wyze streams have
    no usable timestamps), scale to the tile, emit raw YUV420 frames.
    gpu_scale: decode AND shrink on the GPU (VA-API), copy only the small tile back."""
    gpu = hwaccel and gpu_scale and IS_LINUX      # GPU scaling is VA-API (Linux) only
    vf = []
    if keep_every > 1:
        vf.append(f"select='not(mod(n\\,{keep_every}))'")
    if gpu:
        vf += [f"scale_vaapi=w={tile_w}:h={tile_h}:format=nv12", "hwdownload", "format=nv12"]
        # -pix_fmt yuv420p below converts the small tile on the CPU (cheap at tile size)
    else:
        vf.append(f"scale={tile_w}:{tile_h}")
    return ["ffmpeg", "-loglevel", "error" if not VERBOSE else "warning", *input_args(url, hwaccel, gpu),
            "-an", "-vf", ",".join(vf), "-fps_mode", "passthrough",
            "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"]


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
                 gpu_scale=False):
        super().__init__(daemon=True)
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
            first_frame = None             # time of this session's first frame
            self.proc = popen_ffmpeg(
                camera_ffmpeg_cmd(self.url(), self.w, self.h, self.hwaccel, self.keep_every,
                                  self.gpu_scale),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            et = threading.Thread(target=self._stderr_reader, args=(self.proc.stderr, errlines), daemon=True)
            et.start()
            out = self.proc.stdout
            try:
                while not self.stop.is_set():
                    n = 0
                    while n < size:                    # read straight into a reused buffer
                        r = out.readinto(mv[n:])
                        if not r:
                            raise EOFError
                        n += r
                    self.frame = bytes(buf)
                    self.last = time.time()
                    if first_frame is None:
                        first_frame = self.last
                    self.frames += 1
            except (EOFError, OSError, ValueError):
                pass
            finally:
                stop_ffmpeg(self.proc)
                et.join(timeout=2)
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


def play_mosaic(cams, user, password, path, fps=15, resync_min=15, gpu_scale=False):
    """One window with every camera tiled. Each camera has its own ffmpeg and reconnect loop,
    so a slow or dead camera never freezes the others. Cameras are resynced (reconnected) on a
    staggered schedule so none drifts behind live; press r to resync all now."""
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
                        gpu_scale) for c in cams]
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

            if any(f.save_needed for f in feeds):
                for f in feeds:
                    f.save_needed = False
                if cfg_by_ip:
                    save(cfg)
                    log("saved the new stream settings to cameras.json")

            if now - last_status > 600:                          # health summary every 10 min
                last_status = now
                log("status: " + "; ".join(
                    f"{f.name} {'LIVE' if f.last and now - f.last < STALL_SECS else 'DOWN'} "
                    f"(drops {f.drops})" for f in feeds))

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
        try:
            mpv.stdin.close()
        except OSError:
            pass
        if mpv.poll() is None:
            mpv.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
        log("grid stopped")


def play(args, password, cams=None):
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
    if args.windows:
        play_windows(cams, user, password, path)
    else:
        play_mosaic(cams, user, password, path, fps=args.fps, resync_min=args.resync,
                    gpu_scale=args.gpu)


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
    ap.add_argument("command", nargs="?", choices=["play", "scan", "name", "arrange", "test", "all"],
                    default="play", help="play (default) | scan | name | arrange | test (all = scan then play)")
    ap.add_argument("target", nargs="?", help="camera for 'test'/'name': name, IP, or last number of the IP")
    ap.add_argument("-c", "--cam", metavar="LIST",
                    help="only these cameras: comma-separated names, IPs, or last IP numbers")
    ap.add_argument("-x", "--exclude", metavar="LIST", help="hide these cameras (same format as --cam)")
    ap.add_argument("--scan", action="store_true", help="rescan the network before playing")
    ap.add_argument("--list", action="store_true", help="list saved cameras and exit")
    ap.add_argument("--working-only", action="store_true",
                    help="skip cameras whose last scan check failed (default: try them all)")
    ap.add_argument("--resync", type=int, default=10, metavar="MIN",
                    help="reconnect each camera every MIN minutes (staggered) to stay live; 0 = off "
                         "(default 10)")
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
