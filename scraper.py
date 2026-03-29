"""
Starpets.gg Targeted Hunt Scraper
=================================
Exclusively searches for pets defined in config.json using the site's search bar
to avoid parameter-based bot detection. Sends alerts via ntfy.sh.

Config format for tag filters:
  { "pet_name": "(MFR) Unicorn", "target_price": 1.00 }
  Tags: M=Mega, N=Neon, F=Fly, R=Ride
  Order: [M|N][F][R]  (Mega/Neon first, then Fly, then Ride)
  Mega and Neon are mutually exclusive.
  Filter matches pets with equal or better attributes.
"""

import csv
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
import requests

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Force UTF-8 output on Windows consoles
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
CSV_PATH = SCRIPT_DIR / "price_history.csv"
CONFIG_PATH = SCRIPT_DIR / "config.json"
SCREENSHOT_DIR = SCRIPT_DIR / "screenshots"

TARGET_URL = "https://starpets.gg"

# ntfy configuration
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

def send_alert(message):
    """Send a notification to ntfy.sh if a topic is configured."""
    if NTFY_TOPIC:
        try:
            url = f"https://ntfy.sh/{NTFY_TOPIC}"
            headers = {
                "Title": "Starpets Price Alert",
                "Priority": "high",
                "Tags": "money_with_wings,star"
            }
            requests.post(url, data=message.encode('utf-8'), headers=headers)
        except Exception as e:
            print(f"  [WARN] Could not send ntfy notification: {e}")


# ── Helpers ─────────────────────────────────────────────────────────────────
def parse_price(raw: str) -> float | None:
    """Extract a numeric price from strings like '0.08 $', '$1.16', or '0,29 €'."""
    normalized = raw.replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)", normalized)
    if match:
        return float(match.group(1))
    return None


def parse_pet_filter(pet_name_raw: str) -> tuple[str, set]:
    """Parse '(MFR) Unicorn' into ('Unicorn', {'M','F','R'}).
    If no tag prefix, returns the name as-is with an empty tag set."""
    match = re.match(r'^\(([MNFR]+)\)\s+(.+)$', pet_name_raw.strip(), re.IGNORECASE)
    if match:
        tags = set(match.group(1).upper())
        base_name = match.group(2).strip()
        return base_name, tags
    return pet_name_raw.strip(), set()


def build_tag_string(tags) -> str:
    """Build ordered tag prefix like '(MFR)' from a set/list of tags.
    Order: [M|N] then [F] then [R]. Returns '' if no tags."""
    if not tags:
        return ""
    ordered = ""
    if 'M' in tags:
        ordered += 'M'
    elif 'N' in tags:
        ordered += 'N'
    if 'F' in tags:
        ordered += 'F'
    if 'R' in tags:
        ordered += 'R'
    return f"({ordered})" if ordered else ""


def tags_meet_filter(pet_tags: set, filter_tags: set) -> bool:
    """Check if pet_tags are equal to or better than filter_tags.

    Hierarchy (low to high):
      Glow:  (none) < Neon (N) < Mega (M)
      Fly:   (none) < Fly (F)
      Ride:  (none) < Ride (R)

    A pet passes when it has *at least* the requested attributes;
    higher-tier substitutes are accepted (e.g. Mega satisfies a Neon filter).
    """
    if not filter_tags:
        return True  # No tag filter -> accept any variant

    # Glow tier
    if 'M' in filter_tags:
        if 'M' not in pet_tags:
            return False
    elif 'N' in filter_tags:
        if 'N' not in pet_tags and 'M' not in pet_tags:
            return False

    # Fly
    if 'F' in filter_tags and 'F' not in pet_tags:
        return False

    # Ride
    if 'R' in filter_tags and 'R' not in pet_tags:
        return False

    return True


def load_alerts() -> list[dict]:
    """Load alert targets from config.json."""
    if not CONFIG_PATH.exists():
        print(f"[!] Warning: {CONFIG_PATH} not found.")
        return []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("alerts", [])
    except Exception as e:
        print(f"[!] Error loading config.json: {e}")
        return []


def append_to_csv(items: list[dict]) -> None:
    """Append scraped items to price_history.csv."""
    if not items:
        return
    file_exists = CSV_PATH.exists() and CSV_PATH.stat().st_size > 0
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "pet_name", "tags", "price_eur"])
        for item in items:
            writer.writerow([
                item["timestamp"],
                item["pet_name"],
                item.get("tag_str", ""),
                item["price_eur"],
            ])


# ── SVG badge extraction JS ────────────────────────────────────────────────
EXTRACT_JS = """
() => {
    const cards = document.querySelectorAll('a[href*="/adopt-me/shop/"]');
    const results = [];
    const TAG_COLORS = {
        '#7e10d4': 'M',
        '#40bb18': 'N',
        '#108ed5': 'F',
        '#d51057': 'R'
    };
    for (let i = 0; i < Math.min(cards.length, 10); i++) {
        const card = cards[i];
        const nameEl = card.querySelector('h3');
        const tags = [];
        const fills = card.querySelectorAll('svg rect[fill], svg circle[fill]');
        for (const el of fills) {
            const fill = (el.getAttribute('fill') || '').toLowerCase();
            if (TAG_COLORS[fill] && !tags.includes(TAG_COLORS[fill])) {
                tags.push(TAG_COLORS[fill]);
            }
        }
        results.push({
            "text": card.innerText.trim(),
            "card_name": nameEl ? nameEl.innerText.trim() : null,
            "tags": tags
        });
    }
    return results;
}
"""


# ── Scraping ────────────────────────────────────────────────────────────────
def hunt() -> list[dict]:
    """Search for each pet in config.json and return found items."""
    all_found_items: list[dict] = []
    alerts = load_alerts()

    if not alerts:
        print("[!] No alerts configured. Nothing to hunt.")
        return []

    print(f"[*] Starting Hunt Mode for {len(alerts)} items...")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # 1. Nav to Home
        print(f"[*] Navigating to {TARGET_URL} ...")
        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
        except PlaywrightTimeout:
            print("[WARN] Home page load timed out -- attempting to proceed anyway.")

        # 2. Ensure Currency is Euro (€)
        try:
            print("[*] Checking currency setting...")
            currency_btn = page.locator("header").locator("button, div").filter(
                has_text=re.compile(r"[\$€]")
            ).first
            if currency_btn.is_visible():
                current_text = currency_btn.inner_text()
                if "$" in current_text or "USD" in current_text:
                    print("[*] Switching currency to EUR...")
                    currency_btn.click()
                    page.wait_for_selector("text='EUR'", timeout=5000).click()
                    page.wait_for_timeout(2000)
                else:
                    print("[*] Currency already seems to be EUR.")
        except Exception as e:
            print(f"[WARN] Could not verify/switch currency: {e}")

        for alert in alerts:
            raw_pet_name = alert.get("pet_name", "").strip()
            target_price = alert.get("target_price")

            if not raw_pet_name:
                continue

            # Parse tag filter: "(MFR) Cat" -> base="Cat", tags={'M','F','R'}
            base_name, required_tags = parse_pet_filter(raw_pet_name)
            tag_label = build_tag_string(required_tags)
            display_name = f"{tag_label} {base_name}" if tag_label else base_name

            print(f"\n[>] Hunting for: {display_name} (Target <= {target_price}€)")

            try:
                # 3. Human-like Search (base name only – tags aren't searchable)
                search_area = page.locator("text='Quick search'").first
                if search_area.is_visible():
                    search_area.click()

                search_box = page.get_by_placeholder("Quick search")
                search_box.focus()
                search_box.fill("")
                search_box.type(base_name, delay=100)
                page.keyboard.press("Enter")

                # 4. Wait for results
                page.wait_for_timeout(4000)

                # ── Screenshot ──────────────────────────────────────────
                SCREENSHOT_DIR.mkdir(exist_ok=True)
                sanitized = re.sub(r'[^\w\-_\. ]', '_', base_name)
                page.screenshot(path=str(SCREENSHOT_DIR / f"hunt_{sanitized}.png"))

                # 5. Extract data + SVG badge colours
                raw_items = page.evaluate(EXTRACT_JS)

                hunt_count = 0
                for item_data in raw_items:
                    raw_text = item_data["text"]
                    card_name = item_data["card_name"]
                    pet_tags = set(item_data.get("tags", []))
                    pet_tag_str = build_tag_string(pet_tags)

                    lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
                    if len(lines) < 2:
                        continue

                    # Identify price line
                    price_line = None
                    name_parts = []
                    currency_symbols = ["$", "€", "EUR", "USD"]

                    for line in lines:
                        has_currency = any(sym in line for sym in currency_symbols)
                        if has_currency or (re.search(r"\d", line) and line == lines[-1]):
                            price_line = line
                        else:
                            name_parts.append(line)

                    if price_line is None:
                        continue
                    price = parse_price(price_line)
                    if price is None:
                        continue

                    found_pet_name = card_name if card_name else " ".join(name_parts)

                    # --- NAME FILTER (skip recommendations) ---
                    if base_name.lower() not in found_pet_name.lower():
                        print(f"  [SKIP] '{found_pet_name}' – not a name match")
                        continue

                    # --- TAG FILTER (equal or better) ---
                    if not tags_meet_filter(pet_tags, required_tags):
                        disp = f"{pet_tag_str} {found_pet_name}" if pet_tag_str else found_pet_name
                        print(f"  [SKIP] {disp} – tags don't meet filter {tag_label or '(none)'}")
                        continue

                    full_disp = f"{pet_tag_str} {found_pet_name}" if pet_tag_str else found_pet_name
                    print(f"  [+] {full_disp} @ {price:.2f}€")

                    all_found_items.append({
                        "timestamp": timestamp,
                        "pet_name": found_pet_name,
                        "price_eur": price,
                        "tags": list(pet_tags),
                        "tag_str": pet_tag_str,
                        "config_entry": raw_pet_name,
                    })
                    hunt_count += 1

                if hunt_count == 0:
                    print(f"  [?] No matching results for '{display_name}'")
                else:
                    print(f"  [*] Found {hunt_count} matching listings.")

            except Exception as e:
                print(f"  [ERR] Failed hunting '{display_name}': {e}")

        browser.close()

    return all_found_items


# ── Main ────────────────────────────────────────────────────────────────────
def main() -> None:
    print("=" * 60)
    print("  [*] Starpets.gg Targeted Hunt Scraper")
    print("=" * 60)

    alerts = load_alerts()
    items = hunt()

    if items:
        # Build lookup: config_entry -> target_price
        target_prices = {
            a["pet_name"].strip(): a.get("target_price", 0)
            for a in alerts if "pet_name" in a
        }

        best_deals: dict[str, dict] = {}
        for item in items:
            config_entry = item.get("config_entry", item["pet_name"])
            price = item["price_eur"]
            max_allowed = target_prices.get(config_entry)
            if max_allowed is None or price > max_allowed:
                continue
            if config_entry not in best_deals or price < best_deals[config_entry]["price_eur"]:
                best_deals[config_entry] = item

        # Notify the winners
        if best_deals:
            print(f"\n[!] Winner's Circle: Found {len(best_deals)} champion deals:")
            for config_entry, deal in best_deals.items():
                price = deal["price_eur"]
                tag_str = deal.get("tag_str", "")
                found_name = deal["pet_name"]
                full_found = f"{tag_str} {found_name}" if tag_str else found_name
                message = f"🎯 CHEAPEST {config_entry} found: {full_found} for {price:.2f}€"
                print(f"  [ALERT] {message}")
                send_alert(message)
        else:
            print("\n[INFO] No items passed the budget/filter criteria.")

        append_to_csv(items)
        print(f"\n[SAVE] Logged {len(items)} listings to {CSV_PATH}")
    else:
        print("\n[INFO] Hunt complete. No data to log.")

    print("[DONE] Finished!")


if __name__ == "__main__":
    main()
