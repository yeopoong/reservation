import argparse
import datetime
import os
import re
import time

from playwright.sync_api import sync_playwright


SEARCH_URL = "https://paramus.cps.golf/onlineresweb/search-teetime"
LOGIN_URL = "https://paramus.cps.golf/onlineresweb/auth/login"
FINALIZE_SELECTOR = 'button:has-text("Finalize Reservation")'

EMAIL = os.environ.get("PARAMUS_EMAIL")
PASSWORD = os.environ.get("PARAMUS_PASSWORD")
HEADLESS_ENV = os.environ.get("PARAMUS_HEADLESS", "false").strip().lower()


def time_str_to_float(time_str: str) -> float:
    h, m = time_str.split(":")
    return int(h) + int(m) / 60.0


def is_valid_time(time_text: str, am_pm: str, start_str: str, end_str: str) -> bool:
    time_text = time_text.strip()
    if not time_text:
        return False

    parts = time_text.split(":")
    if len(parts) != 2:
        return False

    hours, minutes = int(parts[0]), int(parts[1])
    if am_pm == "P" and hours != 12:
        hours += 12
    elif am_pm == "A" and hours == 12:
        hours = 0

    current = hours + minutes / 60.0
    return time_str_to_float(start_str) <= current <= time_str_to_float(end_str)


def require_credentials() -> tuple[str, str]:
    """Load login credentials from the environment or fail fast."""
    email = EMAIL
    password = PASSWORD
    if not email or not password:
        raise RuntimeError(
            "Missing PARAMUS_EMAIL or PARAMUS_PASSWORD environment variable."
        )
    return email, password


def use_headless_browser() -> bool:
    """Return whether Playwright should use headless mode."""
    return HEADLESS_ENV in {"1", "true", "yes", "on"}


def should_stop(stop_event=None) -> bool:
    """Return True when an external stop signal has been requested."""
    return bool(stop_event and stop_event.is_set())


def interruptible_sleep(seconds: float, stop_event=None, step: float = 0.2) -> bool:
    """Sleep in short steps so the booking loop can stop quickly.

    Returns False if a stop was requested during the sleep.
    """
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if should_stop(stop_event):
            return False
        remaining = deadline - time.time()
        time.sleep(min(step, remaining))
    return not should_stop(stop_event)


def click_target_date(page, target_offset_days: int) -> bool:
    """Click the nth visible enabled calendar day in the desktop calendar."""
    try:
        return page.evaluate(
            """(targetOffsetDays) => {
                const calendar = document.querySelector(
                    'app-search-teetime-filters app-ngx-dates-picker.hidemobile'
                );
                if (!calendar) return false;

                const enabledButtons = Array.from(
                    calendar.querySelectorAll('button.btn-day-unit:not([disabled])')
                ).filter(btn => {
                    const rect = btn.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });

                if (enabledButtons.length <= targetOffsetDays) {
                    return false;
                }

                const targetButton = enabledButtons[targetOffsetDays];
                targetButton.click();
                return true;
            }""",
            target_offset_days,
        )
    except Exception:
        pass
    return False


def get_selected_day_text(page) -> str | None:
    """Return the selected day number from the visible desktop calendar."""
    try:
        return page.evaluate(
            """() => {
                const calendar = document.querySelector(
                    'app-search-teetime-filters app-ngx-dates-picker.hidemobile'
                );
                if (!calendar) return null;

                const selected = Array.from(
                    calendar.querySelectorAll('.day-background-upper.is-selected')
                ).find(el => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });

                return selected?.textContent?.trim() || null;
            }"""
        )
    except Exception:
        return None


def wait_for_search_results_settle(page, timeout_ms: int = 5000):
    """Wait until the tee-time result area finishes reacting to a filter change."""
    spinner_selectors = [
        "ngx-spinner",
        ".ngx-spinner-overlay",
        ".mat-progress-spinner",
        ".mat-spinner",
    ]

    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 3000))
    except Exception:
        pass

    try:
        page.wait_for_function(
            """(selectors) => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0
                        && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };

                const spinnerVisible = selectors.some(selector =>
                    Array.from(document.querySelectorAll(selector)).some(isVisible)
                );
                if (spinnerVisible) return false;

                const list = document.querySelector('app-search-teetime-list');
                if (!list) return false;

                const teeButtons = Array.from(
                    list.querySelectorAll('button.btn-teesheet')
                ).filter(isVisible);
                const noTimes = /No tee times available/i.test(list.textContent || '');

                if (teeButtons.length > 0) {
                    return teeButtons.every(btn => {
                        const timeText = btn.querySelector('time')?.innerText?.trim() || '';
                        return timeText.length > 0;
                    });
                }

                return noTimes;
            }""",
            spinner_selectors,
            timeout=timeout_ms,
        )
    except Exception:
        time.sleep(0.8)


def get_selected_players_label(page) -> str | None:
    """Return the currently selected label in the visible Players toggle group."""
    try:
        return page.evaluate(
            """() => {
                const toggles = Array.from(
                    document.querySelectorAll('app-search-teetime-filters mat-button-toggle')
                ).filter(t => {
                    const rect = t.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });
                const selected = toggles.find(t => t.classList.contains('mat-button-toggle-checked'));
                return selected?.textContent?.trim() || null;
            }"""
        )
    except Exception:
        return None


def get_results_debug_snapshot(page) -> dict:
    """Capture a compact snapshot of the current filter and result-list state."""
    try:
        return page.evaluate(
            """() => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0
                        && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };

                const calendar = document.querySelector(
                    'app-search-teetime-filters app-ngx-dates-picker.hidemobile'
                );
                const selectedDay = Array.from(
                    calendar?.querySelectorAll('.day-background-upper.is-selected') || []
                ).find(isVisible)?.textContent?.trim() || null;

                const players = Array.from(
                    document.querySelectorAll('app-search-teetime-filters mat-button-toggle')
                ).filter(isVisible);
                const selectedPlayers = players.find(t =>
                    t.classList.contains('mat-button-toggle-checked')
                )?.textContent?.trim() || null;

                const list = document.querySelector('app-search-teetime-list');
                const teeButtons = Array.from(
                    list?.querySelectorAll('button.btn-teesheet') || []
                ).filter(isVisible);
                const teeTexts = teeButtons.slice(0, 5).map(btn => ({
                    raw: (btn.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
                    time: btn.querySelector('time')?.innerText?.trim() || ''
                }));

                const visibleSpinners = [
                    ...Array.from(document.querySelectorAll('ngx-spinner')),
                    ...Array.from(document.querySelectorAll('.ngx-spinner-overlay')),
                    ...Array.from(document.querySelectorAll('.mat-progress-spinner')),
                    ...Array.from(document.querySelectorAll('.mat-spinner')),
                ].filter(isVisible).length;

                const listText = (list?.textContent || '').replace(/\\s+/g, ' ').trim();

                return {
                    selectedDay,
                    selectedPlayers,
                    teeButtonCount: teeButtons.length,
                    visibleSpinners,
                    listText: listText.slice(0, 200),
                    firstTeeTexts: teeTexts,
                    url: window.location.href,
                };
            }"""
        )
    except Exception as exc:
        return {"snapshot_error": str(exc)}


def format_debug_snapshot(snapshot: dict) -> str:
    """Render the snapshot into a concise single-line log fragment."""
    if not snapshot:
        return "snapshot=unavailable"

    if "snapshot_error" in snapshot:
        return f"snapshot_error={snapshot['snapshot_error']}"

    return (
        f"day={snapshot.get('selectedDay')} "
        f"players={snapshot.get('selectedPlayers')} "
        f"teeButtons={snapshot.get('teeButtonCount')} "
        f"spinners={snapshot.get('visibleSpinners')} "
        f"url={snapshot.get('url')} "
        f"list='{snapshot.get('listText')}' "
        f"firstTeeTexts={snapshot.get('firstTeeTexts')}"
    )


def click_visible_players_button(page, label: str):
    """Click the visible Players button with the requested label."""
    button = page.locator(
        "app-search-teetime-filters button.mat-button-toggle-button:visible",
        has_text=label,
    ).first
    button.wait_for(timeout=5000)
    button.click()


def apply_players_filter(page, target_players: int) -> bool:
    """Select Players without issuing an incorrect first search like 3 -> 4."""
    target_label = str(target_players)
    current_label = get_selected_players_label(page)

    if current_label != target_label:
        click_visible_players_button(page, target_label)
    else:
        # If the same player count is already selected, re-fire the filter through
        # "Any" instead of a wrong player count so the first refreshed query is valid.
        click_visible_players_button(page, "Any")
        time.sleep(0.2)
        click_visible_players_button(page, target_label)

    try:
        page.wait_for_function(
            f"""() => Array.from(document.querySelectorAll('mat-button-toggle')).some(
                t => t.classList.contains('mat-button-toggle-checked')
                     && t.textContent.trim() === '{target_label}'
            )""",
            timeout=5000,
        )
        wait_for_search_results_settle(page)
        return True
    except Exception:
        time.sleep(0.5)
        selected = get_selected_players_label(page) == target_label
        if selected:
            wait_for_search_results_settle(page)
        return selected


def select_players_and_date(page, target_players: int, target_offset_days: int) -> bool:
    """Apply the filters in the real UI order: date, then Players."""
    page.wait_for_selector("app-search-teetime-filters button.mat-button-toggle-button:visible", timeout=10000)

    try:
        page.wait_for_function(
            """() => document.querySelectorAll(
                'app-search-teetime-filters app-ngx-dates-picker.hidemobile button.btn-day-unit:not([disabled])'
            ).length <= 5""",
            timeout=5000,
        )
    except Exception:
        time.sleep(1)

    before_day = get_selected_day_text(page)
    if not click_target_date(page, target_offset_days):
        return False

    try:
        page.wait_for_function(
            """(previousDay) => {
                const calendar = document.querySelector(
                    'app-search-teetime-filters app-ngx-dates-picker.hidemobile'
                );
                if (!calendar) return false;
                const selected = Array.from(
                    calendar.querySelectorAll('.day-background-upper.is-selected')
                ).find(el => {
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                });
                const currentDay = selected?.textContent?.trim() || null;
                return currentDay && currentDay !== previousDay;
            }""",
            before_day,
            timeout=3000,
        )
    except Exception:
        time.sleep(0.5)

    wait_for_search_results_settle(page)

    # Re-apply Players after the date click so the tee sheet refreshes using
    # the requested player filter before we inspect any tee times.
    if not apply_players_filter(page, target_players):
        return False

    return True


def run_booking(
    dry_run: bool = False,
    force_run: bool = False,
    log_callback=None,
    target_offset_days: int = 2,
    target_start_time: str = "11:30",
    target_end_time: str = "15:00",
    target_players: int = 4,
    email: str | None = None,
    password: str | None = None,
    stop_event=None,
):
    def logger(msg: str, is_important: bool = False):
        print(msg, flush=True)
        if log_callback and is_important:
            try:
                log_callback(msg)
            except Exception:
                pass

    def stop_if_requested(context: str) -> bool:
        if should_stop(stop_event):
            logger(
                f"[{datetime.datetime.now()}] Stop requested during {context}. Exiting booking flow.",
                is_important=True,
            )
            return True
        return False

    if not (email and password):
        email, password = require_credentials()

    if stop_if_requested("startup"):
        return

    if not force_run:
        now = datetime.datetime.now()
        open_time = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now >= open_time:
            open_time += datetime.timedelta(days=1)
        trigger = open_time - datetime.timedelta(seconds=30)
        wait_seconds = (trigger - datetime.datetime.now()).total_seconds()
        if wait_seconds > 0:
            logger(
                f"[{datetime.datetime.now()}] Waiting {wait_seconds:.1f}s "
                f"until {trigger} (6:59:30 AM) to open browser...",
                is_important=True,
            )
            if not interruptible_sleep(wait_seconds, stop_event):
                logger(
                    f"[{datetime.datetime.now()}] Stop requested before browser launch.",
                    is_important=True,
                )
                return
        else:
            logger("Target time has passed.", is_important=True)
    else:
        logger(f"[{datetime.datetime.now()}] FORCE RUN ENABLED. Skipping 7 AM wait.", is_important=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=use_headless_browser())
        page = browser.new_context().new_page()

        logger(f"[{datetime.datetime.now()}] Navigating to login page...")
        page.goto(LOGIN_URL)
        try:
            page.wait_for_selector("mat-form-field", timeout=20000)
            page.wait_for_selector('input[type="email"]', timeout=10000)
            logger(f"[{datetime.datetime.now()}] Injecting email and clicking NEXT via JS...")
            page.evaluate(
                """(email) => {
                    let el = document.querySelector('input[type="email"]');
                    if (!el) return;
                    el.removeAttribute('readonly');
                    el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
                    el.value = email;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
                    setTimeout(() => {
                        let btn = Array.from(document.querySelectorAll('button'))
                            .find(b => b.textContent?.includes('NEXT'));
                        if (btn) btn.click();
                    }, 500);
                }""",
                email,
            )
            logger(f"[{datetime.datetime.now()}] Waiting up to 20s for password UI...")
            page.wait_for_selector('input[type="password"]', timeout=20000)
            logger(f"[{datetime.datetime.now()}] Injecting password and clicking SIGN IN via JS...")
            page.evaluate(
                """(password) => {
                    let el = document.querySelector('input[type="password"]');
                    if (!el) return;
                    el.removeAttribute('readonly');
                    el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
                    el.value = password;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
                    setTimeout(() => {
                        let btn = Array.from(document.querySelectorAll('button'))
                            .find(b => b.textContent?.includes('SIGN IN'));
                        if (btn) btn.click();
                    }, 500);
                }""",
                password,
            )
            logger(f"[{datetime.datetime.now()}] ✅ Credentials submitted.")
        except Exception as e:
            logger(f"[{datetime.datetime.now()}] Login failed or already logged in ... {e}", is_important=True)

        # Setup native dialog handler to auto-accept "Too early to book" alerts
        page.on("dialog", lambda dialog: dialog.accept())

        page.wait_for_url("**/search-teetime", timeout=30000)
        logger(f"[{datetime.datetime.now()}] ✅ Login successful. On search page.", is_important=True)

        target_date = datetime.date.today() + datetime.timedelta(days=target_offset_days)
        offset_text = f"T+{target_offset_days}"
        logger(
            f"[{datetime.datetime.now()}] Pre-selecting date {target_date} ({offset_text}) "
            f"then {target_players} Players...",
            is_important=True,
        )
        for click_attempt in range(5):
            if stop_if_requested("initial filter application"):
                return
            if select_players_and_date(page, target_players, target_offset_days):
                if force_run:
                    logger(
                        f"[{datetime.datetime.now()}] Filters applied for {offset_text} / {target_players} Players.",
                        is_important=True,
                    )
                else:
                    logger(
                        f"[{datetime.datetime.now()}] Filters applied for {offset_text} / {target_players} Players. Waiting for 7 AM...",
                        is_important=True,
                    )
                break
            logger(f"[{datetime.datetime.now()}] Date click failed, retrying ({click_attempt + 1}/5).")
            if not interruptible_sleep(1, stop_event):
                return
        else:
            raise RuntimeError(
                f"Failed to apply date/player filters after 5 attempts ({offset_text}, {target_players} Players)."
            )

        if not force_run:
            now = datetime.datetime.now()
            exact_7am = now.replace(hour=7, minute=0, second=0, microsecond=0)
            if now < exact_7am:
                wait_seconds = (exact_7am - now).total_seconds() - 0.2
                if wait_seconds > 0:
                    logger(f"[{datetime.datetime.now()}] Holding... waiting until 06:59:59.8 AM.", is_important=True)
                    if not interruptible_sleep(wait_seconds, stop_event):
                        return

        if force_run:
            logger(
                f"[{datetime.datetime.now()}] Force-run mode: using the already applied date and Players filters.",
                is_important=True,
            )
        else:
            logger(
                f"[{datetime.datetime.now()}] 7 AM! Re-applying date then Players to refresh filtered tee times...",
                is_important=True,
            )
            if select_players_and_date(page, target_players, target_offset_days):
                logger(
                    f"[{datetime.datetime.now()}] Re-applied {offset_text} then {target_players} Players.",
                    is_important=True,
                )

        logger(f"[{datetime.datetime.now()}] Entering booking loop!", is_important=True)

        skipped_times: set[str] = set()
        expected_guests = target_players - 1

        for attempt in range(1, 301):
            try:
                if stop_if_requested("booking loop"):
                    break

                try:
                    page.wait_for_selector("button.btn-teesheet", timeout=200)
                except Exception:
                    pass

                times = page.locator("button.btn-teesheet")
                count = times.count()

                if count == 0:
                    snapshot = get_results_debug_snapshot(page)
                    logger(
                        f"[{datetime.datetime.now()}] (Attempt {attempt}) Found 0 times. "
                        f"Re-applying date -> Players filters... "
                        f"{format_debug_snapshot(snapshot)}"
                    )
                    select_players_and_date(page, target_players, target_offset_days)
                    continue

                try:
                    page.wait_for_function(
                        "() => !!document.querySelector('button.btn-teesheet time')?.innerText?.trim()",
                        timeout=3000,
                    )
                except Exception:
                    pass

                logger(f"[{datetime.datetime.now()}] (Attempt {attempt}) Found {count} tee times!", is_important=True)

                target_btn = None
                target_time_key = None
                for i in range(count):
                    try:
                        card = times.nth(i)
                        info = card.evaluate(
                            """el => ({
                                t: el.querySelector('time')?.innerText?.trim() ?? '',
                                raw: el.innerText ?? ''
                            })"""
                        )
                        time_text = info["t"]
                        if not time_text:
                            continue

                        am_pm_compact = re.sub(r"\s+", "", info["raw"][:20]).upper()
                        am_pm = "A" if "AM" in am_pm_compact else "P"
                        time_key = f"{time_text}{am_pm}"
                        if time_key in skipped_times:
                            continue

                        if is_valid_time(time_text, am_pm, target_start_time, target_end_time):
                            logger(f"[{datetime.datetime.now()}] ✅ Found matching time: {time_text} {am_pm}", is_important=True)
                            target_btn = card
                            target_time_key = time_key
                            break
                    except Exception as e:
                        continue
                
                if target_btn:
                    # Press Escape to clear any rogue "Too Early" Angular modals from previous clicks
                    page.keyboard.press("Escape")
                    target_btn.click(force=True)
                    logger(f"[{datetime.datetime.now()}] ✅ Time selected. Finalizing...", is_important=True)
                else:
                    logger(
                        f"[{datetime.datetime.now()}] Tee times exist but none match "
                        f"{target_start_time}-{target_end_time}. Retrying..."
                    )
                    if not interruptible_sleep(2, stop_event):
                        break
                    continue

                try:
                    page.wait_for_selector(FINALIZE_SELECTOR, timeout=2000)
                except Exception:
                    snapshot = get_results_debug_snapshot(page)
                    logger(
                        f"[{datetime.datetime.now()}] Finalize button didn't appear. Continuing loop... "
                        f"{format_debug_snapshot(snapshot)}"
                    )
                    page.keyboard.press("Escape")
                    continue

                try:
                    page.wait_for_function(
                        f"""() => Array.from(document.querySelectorAll('mat-checkbox')).filter(
                            cb => cb.querySelector('label')?.textContent?.trim() === 'Guest'
                        ).length >= {expected_guests}""",
                        timeout=6000,
                    )
                except Exception:
                    pass
                if not interruptible_sleep(0.3, stop_event):
                    break

                guest_boxes = page.locator("mat-checkbox").filter(has_text="Guest")
                actual_guests = guest_boxes.count()
                logger(f"[{datetime.datetime.now()}] Found {actual_guests} Guest checkbox(es) on page (need {expected_guests}).")

                if actual_guests < expected_guests:
                    snapshot = get_results_debug_snapshot(page)
                    logger(
                        f"[{datetime.datetime.now()}] Slot has only {actual_guests} guest spot(s), "
                        f"need {expected_guests}. Skipping... "
                        f"timeKey={target_time_key} {format_debug_snapshot(snapshot)}"
                    )
                    if target_time_key:
                        skipped_times.add(target_time_key)
                        logger(f"[{datetime.datetime.now()}] Marked '{target_time_key}' as skipped ({len(skipped_times)} total).")
                    try:
                        page.goto(SEARCH_URL)
                        page.wait_for_url("**/search-teetime", timeout=30000)
                        select_players_and_date(page, target_players, target_offset_days)
                    except Exception as nav_err:
                        logger(f"[{datetime.datetime.now()}] Navigation back failed: {nav_err}")
                    continue

                def count_checked_guests() -> int:
                    return page.evaluate(
                        """() => Array.from(document.querySelectorAll('mat-checkbox')).filter(
                            cb => cb.querySelector('label')?.textContent?.trim() === 'Guest'
                                  && cb.querySelector("input[type='checkbox']")?.checked
                        ).length"""
                    )

                def click_unchecked_guests():
                    page.evaluate(
                        """() => Array.from(document.querySelectorAll('mat-checkbox')).filter(
                            cb => cb.querySelector('label')?.textContent?.trim() === 'Guest'
                                  && !cb.querySelector("input[type='checkbox']")?.checked
                        ).forEach(cb => cb.querySelector('label')?.click())"""
                    )

                for i in range(actual_guests):
                    cb = guest_boxes.nth(i)
                    if not cb.locator('input[type="checkbox"]').is_checked():
                        cb.locator("label").click(force=True)
                        if not interruptible_sleep(0.2, stop_event):
                            break

                if stop_if_requested("guest selection"):
                    break

                if not interruptible_sleep(0.5, stop_event):
                    break

                for _retry in range(2):
                    if count_checked_guests() >= actual_guests:
                        break
                    click_unchecked_guests()
                    if not interruptible_sleep(0.5, stop_event):
                        break

                final_checked = count_checked_guests()
                logger(
                    f"[{datetime.datetime.now()}] ✅ Guests checked: {final_checked}/{expected_guests} confirmed.",
                    is_important=True,
                )

                if final_checked < expected_guests:
                    snapshot = get_results_debug_snapshot(page)
                    logger(
                        f"[{datetime.datetime.now()}] Guest selection incomplete. "
                        f"Need {expected_guests}, but only {final_checked} confirmed. "
                        f"timeKey={target_time_key} {format_debug_snapshot(snapshot)}",
                        is_important=True,
                    )
                    if target_time_key:
                        skipped_times.add(target_time_key)
                    try:
                        page.goto(SEARCH_URL)
                        page.wait_for_url("**/search-teetime", timeout=30000)
                        select_players_and_date(page, target_players, target_offset_days)
                    except Exception as nav_err:
                        logger(f"[{datetime.datetime.now()}] Navigation back failed: {nav_err}")
                    continue

                if dry_run:
                    logger(
                        f"[{datetime.datetime.now()}] ✅ DRY RUN: Skipping 'Finalize Reservation' button click.",
                        is_important=True,
                    )
                    interruptible_sleep(10, stop_event)
                    break
                else:
                    try:
                        # Finalize — use Playwright locator for reliability
                        page.locator('button:has-text("Finalize Reservation")').click(timeout=5000)
                        logger(f"[{datetime.datetime.now()}] ✅ Booking finalized!", is_important=True)
                        interruptible_sleep(5, stop_event)
                        break # BREAK out of retry loop on success!
                    except Exception as e:
                        logger(f"[{datetime.datetime.now()}] Could not click finalize button: {e}", is_important=True)
                        continue

            except Exception as e:
                logger(f"[{datetime.datetime.now()}] Unexpected error: {e}", is_important=True)
                logger(f"[{datetime.datetime.now()}] Refreshing to try again...")
                if not interruptible_sleep(2, stop_event):
                    break

        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paramus Golf Booking Bot")
    parser.add_argument("--dry-run", action="store_true", help="Skip final booking click")
    parser.add_argument("--force-run", action="store_true", help="Run immediately without waiting for 7 AM")
    parser.add_argument("--offset", type=int, default=2, help="Days ahead to book (0=today)")
    parser.add_argument("--start-time", type=str, default="11:30", help="Earliest tee time (HH:MM 24h)")
    parser.add_argument("--end-time", type=str, default="15:00", help="Latest tee time (HH:MM 24h)")
    parser.add_argument("--players", type=int, default=4, help="Number of players (1-4)")
    args = parser.parse_args()

    print("=======================================", flush=True)
    print(" Paramus Golf Bot Started              ", flush=True)
    print(f" Dry Run   : {args.dry_run}", flush=True)
    print(f" Force Run : {args.force_run}", flush=True)
    print(f" Offset    : T+{args.offset} (Days)", flush=True)
    print(f" Time      : {args.start_time} ~ {args.end_time}", flush=True)
    print(f" Players   : {args.players}", flush=True)
    print("=======================================", flush=True)

    run_booking(
        dry_run=args.dry_run,
        force_run=args.force_run,
        target_offset_days=args.offset,
        target_start_time=args.start_time,
        target_end_time=args.end_time,
        target_players=args.players,
    )
