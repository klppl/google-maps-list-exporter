# export-places-google-maps

Extract every place from a public Google Maps shared list into a JSON file.

Google Maps has no public API for shared lists, so this drives a real Chrome browser via [Playwright](https://playwright.dev/python/), opens the list, scrolls until every card is loaded, then visits each place to read its exact coordinates from the URL.

## Output

A JSON array, one entry per place:

```json
[
  {
    "title": "Ales stenar",
    "lat": 55.3828668,
    "lng": 14.0540091,
    "image_path": "https://lh3.googleusercontent.com/..."
  }
]
```

`image_path` is included when the card has a thumbnail. The URL's size suffix (e.g. `=w80-h106-k-no`) is rewritten to `=s1600` so the link points at a high-res image instead of the small thumbnail. Pass `--category foo` to add `"category": "foo"` to every entry.

## Requirements

- Python 3.10+
- Google Chrome (recommended — Google blocks sign-in from Playwright's bundled Chromium)
- Linux/macOS (works on Windows too but untested)

Install Chrome on Debian/Ubuntu:

```bash
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
```

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

(`playwright install chromium` is only needed if you intend to use `--no-chrome`. It's harmless to install anyway.)

## Sign in once

Google sometimes shows a login wall when an automated browser clicks around. Sign in once and the session is saved to `./profile/`:

```bash
.venv/bin/python extract.py --login
```

A Chrome window opens. Sign in to your Google account, then press Enter in the terminal.

## Extract

Pass the shared list URL (the short `maps.app.goo.gl/...` form works):

```bash
.venv/bin/python extract.py "https://maps.app.goo.gl/XXXXXXXXX" -o places.json
```

The file is written incrementally — each successful entry is flushed immediately, so a Ctrl-C never loses progress.

### Useful flags

| Flag | Default | Description |
|---|---|---|
| `-o, --output` | `places.json` | Output file |
| `--category NAME` | (none) | Add a `category` field with this value to every entry |
| `--limit N` | (all) | Only process the first N entries (handy for a smoke test) |
| `--show` | hidden | Show the browser window instead of running headless |
| `--profile-dir PATH` | `./profile` | Chrome profile dir (where the login session is stored) |
| `--no-chrome` | off | Use Playwright's bundled Chromium instead of system Chrome |
| `--login` | — | Open the browser for a one-time Google sign-in, then exit |

### Failures

If any entries can't be processed (the panel rarely glitches), they're written to `<output>_failed.json` with the reason:

```json
[
  { "index": 42, "title": "Some place", "image": "...", "reason": "no !3d!4d in URL (...)" }
]
```

You can re-run by hand for those, or look them up in Maps directly.

## How it works

1. Open the list URL in Chrome (with a persistent profile so login state sticks).
2. Find the side-panel scroll container and scroll it until the card count stops growing — gives the total.
3. Snapshot every card's title and thumbnail in one DOM pass.
4. For each card: scroll it into view, click it, wait for the URL to update with the place's `!3d{lat}!4d{lng}` segment, parse coordinates, click "Back".
5. Flush results to disk after every entry.

A few stealth tweaks (`--disable-blink-features=AutomationControlled`, hiding `navigator.webdriver`, etc.) keep Google's automation detector quiet enough that the public list view works without signing in. Sign-in is mostly insurance against the login wall that Google occasionally throws up between clicks.

## Caveats

- **DOM is volatile.** Google reshuffles class names every few months. The script relies on `button.SMP2wb`, `div.fontHeadlineSmall`, and `img.WkIe8` for cards/titles/thumbs. If extraction silently returns nothing, those probably changed — run with `--show` and update the selectors at the top of `extract.py`.
- **Speed.** Roughly 2 seconds per place because each one requires a click-and-back round trip. A 200-place list takes ~7–10 minutes.
- **Coordinate-only pins.** Saved places that are just a dropped pin (no business listing) sometimes don't put `!3d!4d` in the URL and end up in the failures file. Look them up manually.
- **Login wall.** If Google decides to interrupt with a sign-in prompt mid-run, the script pauses and asks you to sign in in the visible window, then continues. This is rare once the profile is logged in.

## License

MIT
