#!/usr/bin/env python3
"""Builds the jRadio station mirror: Radio Browser's catalogue, cut down to what
the radio can use, split by country, with the pictures of the stations most
likely to be looked for already scaled to the 160 px the radio keeps.

Why a mirror at all: radio-browser.info's API answers from one German host,
and from Russia - where most jRadio radios are - that host is sometimes not
reachable at all (all three Russian check-host.net nodes got 502 on
2026-10-11, the day before most of them got through). GitHub Pages is
reachable from there, and it lets a page read both JSON and pictures, so the
search and the picture finder in the radio's web interface can run from here.

Usage:
    python3 tools/build_mirror.py --out site --cache cache

`--cache` keeps the scaled pictures and what was learned about each picture
address between runs, so a daily run fetches only new or changed pictures. In
the workflow it is restored with actions/cache.
"""

import argparse
import concurrent.futures
import gzip
import hashlib
import io
import json
import os
import random
import shutil
import sys
import time
import urllib.error
import urllib.request

from PIL import Image

SERVER_LIST = "https://all.api.radio-browser.info/json/servers"
FALLBACK_SERVERS = ["de1.api.radio-browser.info", "all.api.radio-browser.info"]
USER_AGENT = "jRadio-station-mirror/1 (+https://github.com/jmper-ha/jradio-stations)"
# Some sites answer anything that does not look like a browser with a 403.
PICTURE_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# The radio's own limits: station_catalog_entry_t holds a name and an address
# of this many bytes including the terminator, and refuses longer ones.
NAME_BYTES = 96
URL_BYTES = 256

# Whose pictures are kept here. Every station of the countries most jRadio
# users are in, and the most voted of everyone else; the rest still get a
# picture through the radio itself, as before.
PICTURE_COUNTRIES = {"RU", "UA", "BY", "KZ"}
PICTURE_TOP_WORLD = 5000
PICTURE_SIZE = 160
# The radio takes 32 KB a picture; PNG first, JPEG if a photo will not fit.
PICTURE_PNG_MAX = 24 * 1024
PICTURE_DOWNLOAD_MAX = 8 * 1024 * 1024
PICTURE_TIMEOUT = 15
PICTURE_WORKERS = 32
# A picture that failed is tried again after a week, one that worked is
# fetched again after a month in case the station changed it.
RETRY_FAILED_S = 7 * 24 * 3600
REFRESH_OK_S = 30 * 24 * 3600


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


def http_get(url, user_agent, timeout, limit=None, gzipped=False):
    headers = {"User-Agent": user_agent}
    if gzipped:
        headers["Accept-Encoding"] = "gzip"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        compressed = response.headers.get("Content-Encoding", "") == "gzip"
        if limit is None:
            body = response.read()
        else:
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError("too large")
    return gzip.decompress(body) if compressed else body


# The whole catalogue in one answer is some 70 MB, and the catalogue's own
# host cut it off halfway three times running on 2026-10-11 - the very
# unreliability this mirror is for. So it comes in pages, compressed, and a
# page that fails is asked for again, from another mirror if there is one.
PAGE = 10000
PAGE_ATTEMPTS = 4


def catalogue():
    try:
        servers = [entry["name"] for entry in json.loads(http_get(SERVER_LIST, USER_AGENT, 30))]
    except Exception as error:  # noqa: BLE001 - any failure falls back the same way
        log("server list failed:", error)
        servers = []
    servers = list(dict.fromkeys(servers + FALLBACK_SERVERS))
    random.shuffle(servers)
    out = []
    offset = 0
    while True:
        page = None
        for attempt in range(PAGE_ATTEMPTS):
            server = servers[attempt % len(servers)]
            url = (f"https://{server}/json/stations?hidebroken=true&order=stationuuid"
                   f"&offset={offset}&limit={PAGE}")
            try:
                page = json.loads(http_get(url, USER_AGENT, 120, gzipped=True))
                break
            except Exception as error:  # noqa: BLE001
                log(f"page at {offset} from {server} failed: {error}")
                time.sleep(5 * (attempt + 1))
        if page is None:
            raise SystemExit(f"the catalogue page at {offset} never came")
        out.extend(page)
        log(f"catalogue: {len(out)} stations")
        if len(page) < PAGE:
            return out
        offset += PAGE


def fit_bytes(text, limit):
    """The longest prefix under `limit` bytes, cut between characters."""
    encoded = text.encode("utf-8")
    if len(encoded) < limit:
        return text
    return encoded[:limit - 1].decode("utf-8", "ignore").rstrip()


def web_address(value):
    return isinstance(value, str) and value.startswith(("http://", "https://")) and \
        " " not in value and len(value) > 8


def stations(raw):
    """What the radio can use, in the shape the page reads: one array a
    station, [name, url, codec, bitrate, country, tags, votes, favicon]. An
    array rather than an object halves the file."""
    seen = set()
    out = []
    for item in raw:
        name = " ".join((item.get("name") or "").replace("\t", " ").split())
        url = (item.get("url_resolved") or item.get("url") or "").strip()
        if not name or not web_address(url) or "\t" in url or \
                len(url.encode("utf-8")) >= URL_BYTES or url in seen:
            continue
        seen.add(url)
        favicon = (item.get("favicon") or "").strip()
        tags = [tag.strip().lower() for tag in (item.get("tags") or "").split(",") if tag.strip()]
        out.append([
            fit_bytes(name, NAME_BYTES),
            url,
            (item.get("codec") or "").upper(),
            int(item.get("bitrate") or 0),
            (item.get("countrycode") or "").upper(),
            ",".join(tags[:4]),
            int(item.get("votes") or 0),
            favicon if web_address(favicon) and len(favicon) < 512 else "",
        ])
    out.sort(key=lambda station: station[6], reverse=True)
    return out


def picture_key(favicon):
    return hashlib.sha1(favicon.encode("utf-8")).hexdigest()[:16]


def scale(body):
    """The picture at most 160 px, on black - the panel has no alpha - as PNG,
    or JPEG when a photograph will not fit. Returns (bytes, extension)."""
    image = Image.open(io.BytesIO(body))
    image.load()
    if image.mode in ("P", "LA", "PA") or (image.mode == "RGBA"):
        image = image.convert("RGBA")
        ground = Image.new("RGBA", image.size, (0, 0, 0, 255))
        image = Image.alpha_composite(ground, image)
    image = image.convert("RGB")
    image.thumbnail((PICTURE_SIZE, PICTURE_SIZE), Image.LANCZOS)
    png = io.BytesIO()
    image.save(png, "PNG", optimize=True)
    if png.tell() <= PICTURE_PNG_MAX:
        return png.getvalue(), "png"
    for quality in (85, 70, 55):
        jpeg = io.BytesIO()
        image.save(jpeg, "JPEG", quality=quality, optimize=True)
        if jpeg.tell() <= 32 * 1024:
            return jpeg.getvalue(), "jpg"
    raise ValueError("does not fit")


def fetch_picture(favicon, icon_dir):
    key = picture_key(favicon)
    try:
        body = http_get(favicon, PICTURE_USER_AGENT, PICTURE_TIMEOUT, PICTURE_DOWNLOAD_MAX)
        data, extension = scale(body)
    except Exception as error:  # noqa: BLE001 - a site can fail in any way
        return favicon, None, type(error).__name__
    name = f"{key}.{extension}"
    with open(os.path.join(icon_dir, name), "wb") as file:
        file.write(data)
    return favicon, name, None


def pictures(chosen, cache_dir):
    """Scaled pictures for `chosen` favicon addresses, from the cache where it
    is fresh, fetched where not. Returns {favicon: file name}."""
    icon_dir = os.path.join(cache_dir, "icons")
    os.makedirs(icon_dir, exist_ok=True)
    state_path = os.path.join(cache_dir, "pictures.json")
    try:
        with open(state_path, encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, ValueError):
        state = {}
    now = time.time()
    wanted = []
    for favicon in chosen:
        entry = state.get(favicon)
        if entry is None:
            wanted.append(favicon)
        elif entry.get("file") and now - entry["at"] < REFRESH_OK_S and \
                os.path.exists(os.path.join(icon_dir, entry["file"])):
            continue
        elif not entry.get("file") and now - entry["at"] < RETRY_FAILED_S:
            continue
        else:
            wanted.append(favicon)
    log(f"pictures: {len(chosen)} wanted, {len(wanted)} to fetch")
    failures = {}
    with concurrent.futures.ThreadPoolExecutor(PICTURE_WORKERS) as pool:
        for done, (favicon, name, error) in enumerate(
                pool.map(lambda favicon: fetch_picture(favicon, icon_dir), wanted), 1):
            if name is not None:
                state[favicon] = {"file": name, "at": now}
            else:
                old = state.get(favicon, {})
                # A picture that worked before keeps its file through one bad day.
                state[favicon] = {"file": old.get("file"), "at": now} if old.get("file") and \
                    os.path.exists(os.path.join(icon_dir, old["file"])) else {"at": now}
                failures[error] = failures.get(error, 0) + 1
            if done % 500 == 0:
                log(f"  {done}/{len(wanted)}")
    log("picture failures:", failures)
    with open(state_path, "w", encoding="utf-8") as file:
        json.dump(state, file)
    return {favicon: state[favicon]["file"] for favicon in chosen
            if state.get(favicon, {}).get("file")}


def write_json(path, value):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--no-pictures", action="store_true")
    args = parser.parse_args()

    started = time.time()
    usable = stations(catalogue())
    log(f"{len(usable)} usable stations")

    chosen = []
    for rank, station in enumerate(usable):
        if station[7] and (station[4] in PICTURE_COUNTRIES or rank < PICTURE_TOP_WORLD):
            chosen.append(station[7])
    chosen = list(dict.fromkeys(chosen))
    files = {} if args.no_pictures else pictures(chosen, args.cache)

    if os.path.exists(args.out):
        shutil.rmtree(args.out)
    os.makedirs(os.path.join(args.out, "stations"))
    os.makedirs(os.path.join(args.out, "icons"))
    used = set()
    # The ninth column: the picture's file here, "" when there is none.
    for station in usable:
        name = files.get(station[7], "")
        station.append(name)
        if name:
            used.add(name)
    for name in used:
        shutil.copy(os.path.join(args.cache, "icons", name), os.path.join(args.out, "icons", name))

    by_country = {}
    for station in usable:
        by_country.setdefault(station[4] or "ZZ", []).append(station)
    for code, items in by_country.items():
        write_json(os.path.join(args.out, "stations", f"{code}.json"), items)
    write_json(os.path.join(args.out, "stations", "all.json"), usable)
    write_json(os.path.join(args.out, "countries.json"),
               sorted(([code, len(items)] for code, items in by_country.items() if code != "ZZ"),
                      key=lambda pair: -pair[1]))
    meta = {
        "format": 1,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stations": len(usable),
        "pictures": len(used),
        "columns": ["name", "url", "codec", "bitrate", "country", "tags", "votes",
                    "favicon", "icon"],
        "source": "https://www.radio-browser.info/",
    }
    write_json(os.path.join(args.out, "meta.json"), meta)
    shutil.copy(os.path.join(os.path.dirname(__file__), "..", "index.html"),
                os.path.join(args.out, "index.html"))
    log(f"done in {time.time() - started:.0f} s: {meta}")


if __name__ == "__main__":
    main()
