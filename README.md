# jradio-stations

A daily mirror of the open [Radio Browser](https://www.radio-browser.info/)
station catalogue for the web interface of [jRadio](https://github.com/jmper-ha/jradio),
published at https://jmper-ha.github.io/jradio-stations/.

**Why.** The radio's station search asks Radio Browser's API from the browser,
and from Russia - where most jRadio radios are - that API is sometimes not
reachable at all. GitHub Pages is, and it lets a page read both the JSON and
the pictures. So once a day GitHub Actions fetches the catalogue (from outside
any such block), keeps what the radio can store, splits it by country, and
scales the pictures of the stations most likely to be looked for.

**What is published.**

| File | What |
|---|---|
| `meta.json` | when it was built, how many stations and pictures, the column names |
| `countries.json` | `[code, stations]`, most stations first |
| `stations/<code>.json`, `stations/ZZ.json` | one country's stations, most voted first; ZZ is no country |
| `stations/all.json` | every station, most voted first |
| `icons/<hash>.png` or `.jpg` | pictures at most 160 px, on black |

A station is an array: `[name, url, codec, bitrate, country, tags, votes,
favicon, icon]` - `favicon` is the station's own picture address, `icon` the
file under `icons/` or `""`. Names are cut to fit the radio's 96 bytes; stream
addresses of 256 bytes or more are left out, as the radio cannot store them.

Pictures are kept for every station of Russia, Ukraine, Belarus and
Kazakhstan, and for the 5000 most voted elsewhere. Other stations' pictures
are fetched by the radio itself, as before.

**Running it.** `python3 tools/build_mirror.py --out site --cache cache`
(needs Pillow). The cache keeps the scaled pictures between runs.

The catalogue is Radio Browser's, an open database anyone may use; this is
only a copy of it in another shape.
