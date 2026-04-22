#!/usr/bin/env python3
"""Extract places from a public Google Maps shared list into JSON.

First-time setup (sign in once so Google stops gating with a login wall):
    python extract.py --login

Then extract:
    python extract.py "<your shared list URL>" -o places.json
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page


COORD_FROM_DATA = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
PLACE_URL_READY = re.compile(r"!3d-?\d+\.\d+!4d-?\d+\.\d+")
IMG_SIZE_SUFFIX = re.compile(r"=[a-z0-9-]+$", re.IGNORECASE)


def upgrade_image(url: str) -> str:
    """Replace Google's thumbnail size cap (e.g. =w80-h106-k-no) with a high-res spec.

    Returns '' for Google's no-thumbnail placeholder, which is not a real image.
    """
    if not url or "result-no-thumbnail" in url:
        return ""
    return IMG_SIZE_SUFFIX.sub("=s1600", url)


def extract_hero_image(page: Page) -> str:
    """Read the hero photo URL from the currently-open place panel.

    Picks the largest visible image hosted on Google's photo CDN.
    """
    return page.evaluate(
        r"""() => {
            const ok = s => s && (s.includes('googleusercontent.com') || s.includes('ggpht.com'))
                && !s.includes('result-no-thumbnail');
            let best = '', bestArea = 0;
            for (const img of document.querySelectorAll('img')) {
                if (!ok(img.src)) continue;
                const r = img.getBoundingClientRect();
                const area = r.width * r.height;
                if (r.width >= 100 && area > bestArea) { best = img.src; bestArea = area; }
            }
            for (const el of document.querySelectorAll('[style*="background-image"]')) {
                const m = (el.getAttribute('style') || '').match(/url\(["']?(https?:[^"')\s]+)/);
                if (!m || !ok(m[1])) continue;
                const r = el.getBoundingClientRect();
                const area = r.width * r.height;
                if (r.width >= 100 && area > bestArea) { best = m[1]; bestArea = area; }
            }
            return best;
        }"""
    )

CARD_SELECTOR = "button.SMP2wb"
TITLE_SELECTOR = "div.fontHeadlineSmall"
IMG_SELECTOR = "img.WkIe8"
BACK_SELECTORS = (
    "button[aria-label='Tillbaka']",
    "button[aria-label='Back']",
)
DEFAULT_PROFILE = Path(__file__).parent / "profile"


def parse_coords(url: str) -> tuple[float | None, float | None]:
    """Pull the place's exact pin coords (the !3d!4d pair) out of a Maps URL."""
    m = COORD_FROM_DATA.search(url)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None, None


def dismiss_consent(page: Page) -> None:
    for label in ("Acceptera alla", "Godkänn alla", "Accept all", "I agree"):
        try:
            page.get_by_role("button", name=re.compile(label, re.I)).first.click(timeout=2000)
            return
        except (PWTimeout, Exception):
            continue


def is_login_wall(page: Page) -> bool:
    return "accounts.google.com" in page.url or "/ServiceLogin" in page.url


def wait_for_login_recovery(page: Page, list_url: str) -> None:
    print("\nGoogle is showing a login wall. Sign in manually in the browser,", file=sys.stderr)
    print("then press Enter here to continue...", file=sys.stderr)
    input()
    page.goto(list_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(CARD_SELECTOR, timeout=20000)


def ensure_scroller(page: Page) -> None:
    """(Re)attach `window.__scroller` to the panel that holds the cards."""
    page.evaluate(
        f"""() => {{
            if (window.__scroller && document.contains(window.__scroller)) return;
            const card = document.querySelector({CARD_SELECTOR!r});
            let el = card;
            while (el) {{
                const ov = getComputedStyle(el).overflowY;
                if (ov === 'auto' || ov === 'scroll') {{ window.__scroller = el; return; }}
                el = el.parentElement;
            }}
        }}"""
    )


def scroll_until_stable(page: Page, idle_rounds: int = 5, pause_ms: int = 1200) -> int:
    page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    ensure_scroller(page)
    last = -1
    idle = 0
    while idle < idle_rounds:
        count = page.locator(CARD_SELECTOR).count()
        page.evaluate("() => window.__scroller && window.__scroller.scrollTo(0, window.__scroller.scrollHeight)")
        page.wait_for_timeout(pause_ms)
        if count == last:
            idle += 1
        else:
            idle = 0
            last = count
    return page.locator(CARD_SELECTOR).count()


def ensure_card_loaded(page: Page, index: int, max_scrolls: int = 40) -> bool:
    """Scroll the list panel until card[index] is mounted in the DOM."""
    ensure_scroller(page)
    for _ in range(max_scrolls):
        count = page.evaluate(f"document.querySelectorAll({CARD_SELECTOR!r}).length")
        if count > index:
            return True
        page.evaluate("() => window.__scroller && window.__scroller.scrollTo(0, window.__scroller.scrollHeight)")
        page.wait_for_timeout(700)
    return False


def click_back(page: Page) -> None:
    for sel in BACK_SELECTORS:
        try:
            page.locator(sel).first.click(timeout=1500)
            return
        except (PWTimeout, Exception):
            continue
    page.go_back(wait_until="domcontentloaded")


def ensure_on_list(page: Page, list_url: str) -> None:
    """Make sure the list panel is visible — recover via re-navigation if not."""
    try:
        page.wait_for_selector(CARD_SELECTOR, timeout=4000)
        return
    except PWTimeout:
        pass
    page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
    try:
        page.wait_for_selector(CARD_SELECTOR, timeout=20000)
    except PWTimeout:
        pass


def scroll_card_into_view(page: Page, index: int) -> None:
    """Force the i-th card into the panel's viewport so .nth(i) is attached."""
    page.evaluate(
        f"""(idx) => {{
            const cards = document.querySelectorAll({CARD_SELECTOR!r});
            if (cards[idx]) cards[idx].scrollIntoView({{block: 'center'}});
        }}""",
        index,
    )
    page.wait_for_timeout(150)


def click_card(page: Page, index: int, attempts: int = 3) -> None:
    """Click a card with retry — Maps occasionally drops a loading overlay over it."""
    last_exc: Exception | None = None
    for n in range(attempts):
        try:
            card = page.locator(CARD_SELECTOR).nth(index)
            card.scroll_into_view_if_needed(timeout=3000)
            card.click(timeout=4000)
            return
        except (PWTimeout, Exception) as e:
            last_exc = e
            page.wait_for_timeout(700)
            scroll_card_into_view(page, index)
    # Final fallback: dispatch a synthetic click via JS (bypasses overlay).
    try:
        page.evaluate(
            f"(idx) => document.querySelectorAll({CARD_SELECTOR!r})[idx].click()",
            index,
        )
        return
    except Exception:
        if last_exc:
            raise last_exc
        raise


STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['sv-SE', 'sv', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""


def open_browser(p, profile_dir: Path, headless: bool, channel: str | None = "chrome"):
    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
        ignore_default_args=["--enable-automation"],
        locale="sv-SE",
        viewport={"width": 1280, "height": 900},
    )
    if channel:
        kwargs["channel"] = channel
    ctx = p.chromium.launch_persistent_context(**kwargs)
    ctx.add_init_script(STEALTH_INIT)
    return ctx


def login(profile_dir: Path, channel: str | None) -> None:
    with sync_playwright() as p:
        ctx = open_browser(p, profile_dir, headless=False, channel=channel)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/", wait_until="domcontentloaded")
        print(f"\nSign in to Google in the browser window. Session saved to {profile_dir}", file=sys.stderr)
        print("Press Enter here when you're done...", file=sys.stderr)
        input()
        ctx.close()
    print("Saved. You can now run the extractor without --login.", file=sys.stderr)


def extract(url: str, profile_dir: Path, headless: bool = True,
            debug_dir: Path | None = None, limit: int | None = None,
            channel: str | None = "chrome",
            output_path: Path | None = None,
            failures_path: Path | None = None,
            category: str | None = None) -> tuple[list[dict], list[dict]]:

    def flush(items: list[dict], path: Path | None) -> None:
        if path is None:
            return
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)

    with sync_playwright() as p:
        ctx = open_browser(p, profile_dir, headless=headless, channel=channel)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        dismiss_consent(page)
        page.wait_for_timeout(1500)

        list_url = page.url
        print(f"Resolved URL: {list_url}", file=sys.stderr)
        print(f"Page title:   {page.title()}", file=sys.stderr)

        if is_login_wall(page):
            wait_for_login_recovery(page, list_url)
            list_url = page.url

        try:
            total = scroll_until_stable(page)
        except PWTimeout:
            print("Could not find any list cards.", file=sys.stderr)
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(debug_dir / "fail.png"), full_page=True)
                (debug_dir / "fail.html").write_text(page.content(), encoding="utf-8")
            ctx.close()
            return [], []

        print(f"Found {total} cards", file=sys.stderr)
        if limit:
            total = min(total, limit)

        snapshot = page.evaluate(
            f"""() => Array.from(document.querySelectorAll({CARD_SELECTOR!r})).map(b => {{
                const t = b.querySelector({TITLE_SELECTOR!r});
                const img = b.querySelector({IMG_SELECTOR!r});
                return {{ title: t ? t.innerText.trim() : '', image: img ? img.src : '' }};
            }})"""
        )

        places: list[dict] = []
        failures: list[dict] = []

        def fail(idx: int, t: str, img: str, reason: str) -> None:
            failures.append({"index": idx, "title": t, "image": img, "reason": reason})
            flush(failures, failures_path)
            print(f"  [{idx+1}/{total}] {t}: {reason}", file=sys.stderr)

        for i in range(total):
            title = snapshot[i]["title"]
            image = snapshot[i]["image"]
            if not title:
                fail(i, title, image, "empty title in card snapshot")
                continue

            # Make sure we're back on the list view before this iteration.
            ensure_on_list(page, list_url)
            if not ensure_card_loaded(page, i):
                fail(i, title, image, "card never appeared in DOM after scrolling")
                continue
            scroll_card_into_view(page, i)

            url_before = page.url
            try:
                click_card(page, i)

                try:
                    page.wait_for_function(
                        "(prev) => location.href !== prev && /!3d-?\\d+\\.\\d+!4d-?\\d+\\.\\d+/.test(location.href)",
                        arg=url_before, timeout=10000,
                    )
                except PWTimeout:
                    if is_login_wall(page):
                        wait_for_login_recovery(page, list_url)
                        fail(i, title, image, "login wall — retry this entry")
                        continue

                lat, lng = parse_coords(page.url)
                if lat is None or lng is None:
                    fail(i, title, image, f"no !3d!4d in URL ({page.url[:160]})")
                else:
                    page.wait_for_timeout(500)  # let the hero photo render
                    hero = extract_hero_image(page)
                    chosen = upgrade_image(hero) or upgrade_image(image)
                    entry: dict = {
                        "title": title[:120],
                        "lat": lat,
                        "lng": lng,
                    }
                    if category:
                        entry["category"] = category
                    if chosen:
                        entry["image_path"] = chosen
                    places.append(entry)
                    flush(places, output_path)
                    print(f"  [{i+1}/{total}] {title} -> {lat},{lng}", file=sys.stderr)
            except Exception as e:
                fail(i, title, image, f"error {e}")
            finally:
                click_back(page)
                page.wait_for_timeout(random.randint(400, 900))

        ctx.close()
        return places, failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", nargs="?", help="Public Google Maps shared list URL")
    ap.add_argument("-o", "--output", default="places.json")
    ap.add_argument("--show", action="store_true", help="Show the browser window")
    ap.add_argument("--debug-dir", default="debug")
    ap.add_argument("--limit", type=int, default=None, help="Process only the first N cards")
    ap.add_argument("--profile-dir", default=str(DEFAULT_PROFILE),
                    help="Persistent Chrome profile directory (keeps you signed in)")
    ap.add_argument("--login", action="store_true",
                    help="Open the browser for a one-time Google sign-in, then exit")
    ap.add_argument("--no-chrome", action="store_true",
                    help="Use bundled Chromium instead of system Chrome")
    ap.add_argument("--category", default=None,
                    help="Add this string as a 'category' field on every place")
    args = ap.parse_args()

    profile_dir = Path(args.profile_dir)
    channel = None if args.no_chrome else "chrome"

    if args.login:
        login(profile_dir, channel=channel)
        return 0

    if not args.url:
        ap.error("URL is required (or pass --login)")

    out_path = Path(args.output)
    fail_path = out_path.with_name(out_path.stem + "_failed.json")

    places, failures = extract(args.url, profile_dir=profile_dir, headless=not args.show,
                               debug_dir=Path(args.debug_dir), limit=args.limit, channel=channel,
                               output_path=out_path, failures_path=fail_path,
                               category=args.category)
    print(f"Wrote {len(places)} places to {out_path}", file=sys.stderr)
    if failures:
        print(f"Wrote {len(failures)} failures to {fail_path}", file=sys.stderr)
    return 0 if places else 1


if __name__ == "__main__":
    raise SystemExit(main())
