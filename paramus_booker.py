from __future__ import annotations

import argparse
import datetime
import os
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync


FINALIZE_SELECTOR = 'button:has-text("Finalize Reservation")'
DEBUG_DIR = Path(__file__).with_name("debug")

SUPPORTED_SITES = {"paramus"}
SITE_CONFIG = {
    "paramus": {
        "display_name": "Paramus",
        "search_url": "https://paramus.cps.golf/onlineresweb/search-teetime",
        "login_url": "https://paramus.cps.golf/onlineresweb/auth/login",
        "username_env": "PARAMUS_EMAIL",
        "password_env": "PARAMUS_PASSWORD",
        "headless_env": "PARAMUS_HEADLESS",
        "default_course": None,
    }
}


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


def normalize_site(site: str) -> str:
    site_key = (site or "paramus").strip().lower()
    if site_key not in SUPPORTED_SITES:
        raise RuntimeError(f"Unsupported site '{site}'. Choose from: {', '.join(sorted(SUPPORTED_SITES))}.")
    return site_key


def get_site_config(site: str) -> dict:
    return SITE_CONFIG[normalize_site(site)]


def resolve_default_course(site: str, target_course: str | None) -> str | None:
    if target_course:
        return target_course
    return get_site_config(site).get("default_course")


def require_credentials(site: str = "paramus") -> tuple[str, str]:
    """Load login credentials from the environment or fail fast."""
    config = get_site_config(site)
    email = os.environ.get(config["username_env"])
    password = os.environ.get(config["password_env"])
    if not email or not password:
        raise RuntimeError(
            f"Missing {config['username_env']} or {config['password_env']} environment variable."
        )
    return email, password


def use_headless_browser(site: str = "paramus") -> bool:
    """Return whether Playwright should use headless mode."""
    config = get_site_config(site)
    headless_env = os.environ.get(config["headless_env"], "false").strip().lower()
    return headless_env in {"1", "true", "yes", "on"}


def get_dry_run_hold_seconds() -> int:
    """Return how long dry-run should keep the reservation screen open."""
    try:
        return max(0, int(os.environ.get("DRY_RUN_HOLD_SECONDS", "300")))
    except Exception:
        return 300


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


def click_target_date(page, target_date: datetime.date, target_offset_days: int) -> bool:
    """Select the requested date using the desktop calendar, then mobile arrows as fallback."""
    try:
        clicked = page.evaluate(
            """(targetDay) => {
                const calendar = document.querySelector(
                    'app-search-teetime-filters app-ngx-dates-picker.hidemobile'
                );
                if (!calendar) return false;

                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };

                const dayUnits = Array.from(calendar.querySelectorAll('.day-unit')).filter(isVisible);
                const enabledDayUnits = dayUnits.filter(unit => {
                    const button = unit.querySelector('button.btn-day-unit:not([disabled])');
                    const label = unit.querySelector('.day-background-upper');
                    return button && label && isVisible(button) && isVisible(label);
                });

                const targetUnit = enabledDayUnits.find(unit => {
                    const label = unit.querySelector('.day-background-upper')?.textContent?.trim();
                    return label === String(targetDay);
                });
                if (!targetUnit) {
                    return false;
                }

                const targetButton = targetUnit.querySelector('button.btn-day-unit:not([disabled])');
                if (!targetButton) return false;
                targetButton.click();
                return true;
            }""",
            target_date.day,
        )
        if clicked:
            return True
    except Exception:
        pass

    if target_offset_days <= 0:
        return False

    try:
        for _ in range(target_offset_days):
            right_button = page.locator(
                "app-search-teetime-filters .mobile__container.showmobile .right-chevron-button button:visible"
            ).first
            if right_button.count() == 0:
                return False
            right_button.click(force=True, timeout=1500)
            page.wait_for_timeout(250)
        return True
    except Exception:
        pass

    return False


def get_selected_day_text(page) -> str | None:
    """Return the selected date text from desktop calendar or mobile label."""
    try:
        selected = page.evaluate(
            """() => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };

                const calendar = document.querySelector(
                    'app-search-teetime-filters app-ngx-dates-picker.hidemobile'
                );
                const selected = Array.from(
                    calendar?.querySelectorAll('.day-background-upper.is-selected') || []
                ).find(isVisible);
                if (selected) {
                    return selected.textContent?.trim() || null;
                }

                const mobileLabel = Array.from(
                    document.querySelectorAll(
                        'app-search-teetime-filters .mobile__container.showmobile mat-label'
                    )
                ).find(isVisible);
                return mobileLabel?.textContent?.trim() || null;
            }"""
        )
        return selected
    except Exception:
        return None


def is_target_date_selected(page, target_date: datetime.date) -> bool:
    """Return whether the search filter currently shows the target date."""
    selected = get_selected_day_text(page)
    if not selected:
        return False
    normalized = selected.strip()
    return normalized == str(target_date.day) or normalized in {
        f"{target_date.month}/{target_date.day}/{target_date.year % 100:02d}",
        f"{target_date.month}/{target_date.day}/{target_date.year}",
    }


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


def wait_for_checkout_ready(page, expected_guests: int, timeout_ms: int = 15000) -> bool:
    """Wait for the checkout/refine booking page to finish loading guest/finalize UI."""
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
    except Exception:
        pass

    try:
        page.wait_for_function(
            f"""() => {{
                const isVisible = el => {{
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                }};
                const spinnerVisible = [
                    ...Array.from(document.querySelectorAll('ngx-spinner')),
                    ...Array.from(document.querySelectorAll('.ngx-spinner-overlay')),
                    ...Array.from(document.querySelectorAll('.mat-progress-spinner')),
                    ...Array.from(document.querySelectorAll('.mat-spinner')),
                ].some(isVisible);
                if (spinnerVisible) return false;

                const guestCount = Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(input => {{
                    const scope = input.closest('mat-checkbox, label, div, li, tr') || input.parentElement;
                    return /\\bGuest\\b/i.test(scope?.innerText || scope?.textContent || '');
                }}).length;
                const continueButton = Array.from(document.querySelectorAll('button')).some(
                    btn => isVisible(btn) && /Continue/i.test(btn.innerText || btn.textContent || '')
                );
                const finalize = Array.from(document.querySelectorAll('button')).some(
                    btn => isVisible(btn) && /Finalize Reservation/i.test(btn.innerText || btn.textContent || '')
                );
                return guestCount >= {expected_guests} || continueButton || finalize;
            }}""",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def count_guest_checkboxes(page) -> int:
    """Count visible guest checkbox controls across Paramus and Bergen checkout UIs."""
    try:
        return page.evaluate(
            """() => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };
                return Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(input => {
                    const scope = input.closest('mat-checkbox, label, div, li, tr') || input.parentElement;
                    return (isVisible(input) || isVisible(scope))
                        && /\\bGuest\\b/i.test(scope?.innerText || scope?.textContent || '');
                }).length;
            }"""
        )
    except Exception:
        return 0


def count_checked_guest_checkboxes(page) -> int:
    """Count checked guest checkbox controls across checkout UI variants."""
    try:
        return page.evaluate(
            """() => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };
                return Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(input => {
                    const scope = input.closest('mat-checkbox, label, div, li, tr') || input.parentElement;
                    return (isVisible(input) || isVisible(scope)) && input.checked
                        && /\\bGuest\\b/i.test(scope?.innerText || scope?.textContent || '');
                }).length;
            }"""
        )
    except Exception:
        return 0


def click_unchecked_guest_checkboxes(page):
    """Click any unchecked Guest checkbox without depending on Angular Material markup."""
    page.evaluate(
        """() => {
            const isVisible = el => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none';
            };
            for (const input of Array.from(document.querySelectorAll('input[type="checkbox"]'))) {
                const scope = input.closest('mat-checkbox, label, div, li, tr') || input.parentElement;
                if (!(isVisible(input) || isVisible(scope)) || input.checked) continue;
                if (!/\\bGuest\\b/i.test(scope?.innerText || scope?.textContent || '')) continue;

                const clickable = input.closest('mat-checkbox')?.querySelector('label')
                    || input.closest('label')
                    || scope.querySelector('label')
                    || input;
                clickable.click();
            }
        }"""
    )


def settle_after_filter_change(page, timeout_ms: int = 7000):
    """Wait for filter-triggered reloads, then clear any resulting no-times popup."""
    for _ in range(3):
        wait_for_search_results_settle(page, timeout_ms=timeout_ms)
        if not dismiss_search_overlays(page):
            break
        page.wait_for_timeout(300)


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


def get_filter_select_value(page, filter_control_selector: str) -> str | None:
    """Return the visible value text for a Material select filter."""
    try:
        return page.evaluate(
            """(selector) => {
                const root = document.querySelector(selector);
                if (!root) return null;
                const value = root.querySelector('.mat-select-value-text, .mat-select-value');
                return (value?.innerText || value?.textContent || '').replace(/\\s+/g, ' ').trim() || null;
            }""",
            filter_control_selector,
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
                const mobileDateLabel = Array.from(
                    document.querySelectorAll(
                        'app-search-teetime-filters .mobile__container.showmobile mat-label'
                    )
                ).find(isVisible)?.textContent?.trim() || null;
                const desktopEnabledDays = Array.from(
                    document.querySelectorAll(
                        'app-search-teetime-filters app-ngx-dates-picker.hidemobile button.btn-day-unit:not([disabled])'
                    )
                ).filter(isVisible).length;
                const mobileNextEnabled = Array.from(
                    document.querySelectorAll(
                        'app-search-teetime-filters .mobile__container.showmobile .right-chevron-button button'
                    )
                ).some(btn => isVisible(btn) && !btn.disabled);

                const players = Array.from(
                    document.querySelectorAll('app-search-teetime-filters mat-button-toggle')
                ).filter(isVisible);
                const selectedPlayers = players.find(t =>
                    t.classList.contains('mat-button-toggle-checked')
                )?.textContent?.trim() || null;
                const filterValue = selector => {
                    const root = document.querySelector(selector);
                    if (!root) return null;
                    const value = root.querySelector('.mat-select-value-text, .mat-select-value');
                    return (value?.innerText || value?.textContent || '').replace(/\\s+/g, ' ').trim() || null;
                };
                const selectedCourse = filterValue('app-search-teetime-filters .course-filter-control');
                const selectedHoles = filterValue('app-search-teetime-filters .hole-filter-control');

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
                const visibleDialogs = [
                    ...Array.from(document.querySelectorAll('mat-dialog-container')),
                    ...Array.from(document.querySelectorAll('[role="dialog"]')),
                    ...Array.from(document.querySelectorAll('.modal')),
                    ...Array.from(document.querySelectorAll('.cdk-overlay-pane')),
                ].filter(isVisible);
                const dialogTexts = visibleDialogs.slice(0, 3).map(dialog =>
                    (dialog.innerText || dialog.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 160)
                );
                const visibleBackdrops = Array.from(
                    document.querySelectorAll('.cdk-overlay-backdrop, .modal-backdrop')
                ).filter(isVisible).length;

                const listText = (list?.textContent || '').replace(/\\s+/g, ' ').trim();

                return {
                    selectedDay,
                    mobileDateLabel,
                    desktopEnabledDays,
                    mobileNextEnabled,
                    selectedPlayers,
                    selectedCourse,
                    selectedHoles,
                    teeButtonCount: teeButtons.length,
                    visibleSpinners,
                    visibleBackdrops,
                    dialogTexts,
                    listText: listText.slice(0, 200),
                    firstTeeTexts: teeTexts,
                    url: window.location.href,
                };
            }"""
        )
    except Exception as exc:
        return {"snapshot_error": str(exc)}


def save_debug_artifacts(page, prefix: str, logger=None) -> str | None:
    """Persist the current page so popup/filter failures can be inspected later."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix).strip("_") or "debug"
        base_path = DEBUG_DIR / f"{timestamp}_{safe_prefix}"
        html_path = base_path.with_suffix(".html")
        png_path = base_path.with_suffix(".png")
        html_path.write_text(page.content(), encoding="utf-8")
        try:
            page.screenshot(path=str(png_path), full_page=True, timeout=5000)
        except Exception:
            png_path = None
        message = f"Saved debug page: {html_path}"
        if png_path:
            message += f" / {png_path}"
        if logger:
            logger(f"[{datetime.datetime.now()}] {message}", is_important=True)
        return str(html_path)
    except Exception as exc:
        if logger:
            logger(f"[{datetime.datetime.now()}] Failed to save debug artifacts: {exc}", is_important=True)
        return None


def format_debug_snapshot(snapshot: dict) -> str:
    """Render the snapshot into a concise single-line log fragment."""
    if not snapshot:
        return "snapshot=unavailable"

    if "snapshot_error" in snapshot:
        return f"snapshot_error={snapshot['snapshot_error']}"

    return (
        f"day={snapshot.get('selectedDay')} "
        f"mobileDate={snapshot.get('mobileDateLabel')} "
        f"desktopEnabledDays={snapshot.get('desktopEnabledDays')} "
        f"mobileNextEnabled={snapshot.get('mobileNextEnabled')} "
        f"players={snapshot.get('selectedPlayers')} "
        f"course={snapshot.get('selectedCourse')} "
        f"holes={snapshot.get('selectedHoles')} "
        f"teeButtons={snapshot.get('teeButtonCount')} "
        f"spinners={snapshot.get('visibleSpinners')} "
        f"backdrops={snapshot.get('visibleBackdrops')} "
        f"dialogs={snapshot.get('dialogTexts')} "
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
    button.click(force=True)


def click_continue_to_review_if_present(page) -> bool:
    """Advance Bergen-style refine booking screens to Review & Confirm."""
    continue_selectors = [
        'button:has-text("Continue")',
        '[role="button"]:has-text("Continue")',
    ]
    for selector in continue_selectors:
        try:
            button = page.locator(f"{selector}:visible").first
            if button.count() > 0:
                button.click(force=True, timeout=3000)
                return True
        except Exception:
            continue
    return False


def confirm_final_reservation_prompt(page) -> bool:
    """Accept the final Please Confirm modal without dismissing the reservation flow."""
    has_confirm_prompt = False
    try:
        has_confirm_prompt = page.evaluate(
            """() => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };
                return Array.from(document.querySelectorAll(
                    'mat-dialog-container, [role="dialog"], .cdk-overlay-pane'
                )).some(el => isVisible(el) && /please\\s+confirm/i.test(el.innerText || el.textContent || ''));
            }"""
        )
    except Exception:
        has_confirm_prompt = False
    if not has_confirm_prompt:
        return True

    confirm_labels = [
        "Confirm",
        "Yes",
        "OK",
        "Ok",
        "Book",
        "Reserve",
        "Finalize",
        "Submit",
    ]
    for _ in range(4):
        clicked = False
        for label in confirm_labels:
            selectors = [
                f'mat-dialog-container button:has-text("{label}")',
                f'mat-dialog-container [role="button"]:has-text("{label}")',
                f'[role="dialog"] button:has-text("{label}")',
                f'[role="dialog"] [role="button"]:has-text("{label}")',
                f'.cdk-overlay-pane button:has-text("{label}")',
                f'.cdk-overlay-pane [role="button"]:has-text("{label}")',
            ]
            for selector in selectors:
                try:
                    button = page.locator(f"{selector}:visible").first
                    if button.count() > 0:
                        button.click(force=True, timeout=2000)
                        page.wait_for_timeout(500)
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break

        if not clicked:
            try:
                clicked = page.evaluate(
                    """(labels) => {
                        const isVisible = el => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none';
                        };
                        const normalizedLabels = labels.map(label => label.toLowerCase());
                        const roots = Array.from(document.querySelectorAll(
                            'mat-dialog-container, [role="dialog"], .cdk-overlay-pane'
                        )).filter(isVisible);
                        for (const root of roots) {
                            const text = (root.innerText || root.textContent || '').toLowerCase();
                            if (!/please\\s+confirm|confirm|reservation/i.test(text)) continue;
                            const buttons = Array.from(root.querySelectorAll('button, [role="button"], a'))
                                .filter(isVisible);
                            const target = buttons.find(button => {
                                const buttonText = (button.innerText || button.textContent || button.getAttribute('aria-label') || '')
                                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                                return normalizedLabels.some(label =>
                                    buttonText === label || buttonText.includes(label)
                                );
                            });
                            if (target) {
                                target.click();
                                return true;
                            }
                        }
                        return false;
                    }""",
                    confirm_labels,
                )
                if clicked:
                    page.wait_for_timeout(500)
            except Exception:
                clicked = False

        if not clicked:
            return False

        try:
            page.wait_for_function(
                """() => {
                    const isVisible = el => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none';
                    };
                    return !Array.from(document.querySelectorAll(
                        'mat-dialog-container, [role="dialog"], .cdk-overlay-pane'
                    )).some(el => isVisible(el) && /please\\s+confirm/i.test(el.innerText || el.textContent || ''));
                }""",
                timeout=2500,
            )
            return True
        except Exception:
            continue

    return True


def force_remove_blocking_overlays(page) -> bool:
    """Remove stubborn dialog/backdrop elements after normal close attempts fail."""
    try:
        return page.evaluate(
            """() => {
                const selectors = [
                    'mat-dialog-container',
                    '[role="dialog"]',
                    '.modal',
                    '.modal-backdrop',
                    '.cdk-overlay-backdrop'
                ];
                let removed = 0;

                for (const selector of selectors) {
                    for (const el of Array.from(document.querySelectorAll(selector))) {
                        const pane = el.closest('.cdk-overlay-pane');
                        const target = pane || el;
                        if (target && target.parentElement) {
                            target.remove();
                            removed += 1;
                        }
                    }
                }

                document.body.classList.remove('cdk-global-scrollblock', 'modal-open');
                document.body.style.overflow = '';
                document.body.style.pointerEvents = '';

                for (const container of Array.from(document.querySelectorAll('.cdk-overlay-container'))) {
                    const hasUsefulOverlay = container.querySelector('mat-option, .mat-select-panel');
                    const text = (container.innerText || container.textContent || '').trim();
                    if (!hasUsefulOverlay && !text) {
                        container.innerHTML = '';
                    }
                }

                return removed > 0;
            }"""
        )
    except Exception:
        return False


def close_course_notes_dialog(page) -> bool:
    """Close Bergen's course-notes dialog when it appears after course changes."""
    try:
        clicked = page.evaluate(
            """() => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };
                const dialog = Array.from(document.querySelectorAll('app-search-teesheet-notes-dialog'))
                    .find(isVisible);
                if (!dialog) return false;
                const close = Array.from(dialog.querySelectorAll('button, [role="button"]'))
                    .find(el => /close/i.test(el.innerText || el.textContent || el.getAttribute('aria-label') || ''));
                if (close) {
                    close.click();
                    return true;
                }
                return false;
            }"""
        )
        if clicked:
            try:
                page.wait_for_function(
                    """() => !Array.from(document.querySelectorAll('app-search-teesheet-notes-dialog'))
                        .some(el => {
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none';
                        })""",
                    timeout=1200,
                )
                return True
            except Exception:
                pass

        # Some Angular Material dialogs do not close from a synthetic click in
        # headless mode. If the notes dialog is still visible, remove the whole
        # overlay so it cannot block filter clicks.
        return page.evaluate(
            """() => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };
                const dialogs = Array.from(document.querySelectorAll('app-search-teesheet-notes-dialog'))
                    .filter(isVisible);
                if (!dialogs.length) return false;
                for (const dialog of dialogs) {
                    const wrapper = dialog.closest('.cdk-global-overlay-wrapper');
                    const pane = dialog.closest('.cdk-overlay-pane');
                    const container = dialog.closest('mat-dialog-container');
                    const target = wrapper || pane || container || dialog;
                    target.remove();
                }
                for (const backdrop of Array.from(document.querySelectorAll('.cdk-overlay-backdrop'))) {
                    backdrop.remove();
                }
                document.body.classList.remove('cdk-global-scrollblock');
                document.body.style.overflow = '';
                document.body.style.pointerEvents = '';
                return true;
            }"""
        )
    except Exception:
        return False


def dismiss_search_overlays(page, max_rounds: int = 4) -> bool:
    """Close course notes or modal backdrops that block filter interaction."""
    dismissed = False
    if close_course_notes_dialog(page):
        dismissed = True
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    button_texts = [
        "Close",
        "OK",
        "Ok",
        "Got it",
        "Continue",
        "I Agree",
        "Agree",
        "Accept",
        "Dismiss",
        "No Thanks",
        "Cancel",
        "X",
        "x",
    ]
    scoped_selectors = [
        "mat-dialog-container",
        "[role='dialog']",
        ".modal",
        ".cdk-overlay-pane",
        ".cdk-overlay-container",
    ]

    for _ in range(max_rounds):
        clicked_this_round = False

        for text in button_texts:
            selectors = [
                f'button:has-text("{text}")',
                f'[role="button"]:has-text("{text}")',
            ]
            for scope in scoped_selectors:
                selectors.extend([
                    f'{scope} button:has-text("{text}")',
                    f'{scope} [role="button"]:has-text("{text}")',
                ])
            for selector in selectors:
                try:
                    locator = page.locator(f"{selector}:visible").first
                    if locator.count() > 0:
                        locator.click(force=True, timeout=1000)
                        page.wait_for_timeout(250)
                        dismissed = True
                        clicked_this_round = True
                        break
                except Exception:
                    continue
            if clicked_this_round:
                break

        if not clicked_this_round:
            try:
                clicked_this_round = page.evaluate(
                    """() => {
                        const isVisible = el => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none';
                        };
                        const labels = [
                            'close', 'ok', 'got it', 'continue', 'i agree',
                            'agree', 'accept', 'dismiss', 'no thanks', 'cancel', 'x'
                        ];
                        const roots = Array.from(document.querySelectorAll(
                            'mat-dialog-container, [role="dialog"], .modal, .cdk-overlay-pane, .cdk-overlay-container'
                        )).filter(isVisible);
                        for (const root of roots) {
                            const candidates = Array.from(root.querySelectorAll(
                                'button, [role="button"], a, mat-icon'
                            )).filter(isVisible);
                            const target = candidates.find(el => {
                                const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || '')
                                    .replace(/\\s+/g, ' ').trim().toLowerCase();
                                return labels.some(label => text === label || text.includes(label));
                            });
                            if (target) {
                                target.click();
                                return true;
                            }
                        }
                        return false;
                    }"""
                )
                if clicked_this_round:
                    page.wait_for_timeout(250)
                    dismissed = True
            except Exception:
                clicked_this_round = False

        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

        if not clicked_this_round:
            break

    if force_remove_blocking_overlays(page):
        dismissed = True
        try:
            page.wait_for_timeout(250)
        except Exception:
            pass

    try:
        page.wait_for_function(
            """() => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };
                const blocking = [
                    ...Array.from(document.querySelectorAll('mat-dialog-container')),
                    ...Array.from(document.querySelectorAll('[role="dialog"]')),
                    ...Array.from(document.querySelectorAll('.modal')),
                    ...Array.from(document.querySelectorAll('.cdk-overlay-backdrop.cdk-overlay-backdrop-showing')),
                    ...Array.from(document.querySelectorAll('.modal-backdrop')),
                ].filter(isVisible);
                return blocking.length === 0;
            }""",
            timeout=2500,
        )
    except Exception:
        pass
    return dismissed


def click_filter_option_by_text(page, option_text: str) -> bool:
    """Click a visible option-like element by text inside the filter area or overlay."""
    selectors = [
        f'app-search-teetime-filters button:has-text("{option_text}")',
        f'app-search-teetime-filters [role="button"]:has-text("{option_text}")',
        f'app-search-teetime-filters mat-button-toggle:has-text("{option_text}")',
        f'.cdk-overlay-container mat-option:has-text("{option_text}")',
        f'.cdk-overlay-container button:has-text("{option_text}")',
        f'.cdk-overlay-container [role="option"]:has-text("{option_text}")',
    ]
    for selector in selectors:
        try:
            locator = page.locator(f"{selector}:visible").first
            if locator.count() > 0:
                locator.click(force=True, timeout=1500)
                return True
        except Exception:
            continue
    return False


def select_mat_option(page, filter_control_selector: str, target_text: str, multiple: bool = False) -> bool:
    """Select an Angular Material option from a specific filter control."""
    try:
        opened = page.evaluate(
            """(selector) => {
                const root = document.querySelector(selector);
                if (!root) return false;
                const trigger = root.querySelector('mat-select .mat-select-trigger, mat-select');
                if (!trigger) return false;
                trigger.click();
                return true;
            }""",
            filter_control_selector,
        )
        if not opened:
            return False
        page.wait_for_selector(".cdk-overlay-container mat-option:visible", timeout=3000)
        target_option = page.locator(".cdk-overlay-container mat-option:visible", has_text=target_text).first
        if target_option.count() == 0:
            page.keyboard.press("Escape")
            dismiss_search_overlays(page)
            return False

        if multiple:
            # Bergen course is a multi-select. Clear the previous "Multiple
            # Courses Selected" state before selecting the requested course.
            deselect_all = page.locator(".cdk-overlay-container mat-option:visible", has_text="Deselect All").first
            if deselect_all.count() > 0:
                deselect_all.click(force=True, timeout=2000)
            else:
                page.evaluate(
                    """() => {
                        const isVisible = el => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = window.getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0
                                && style.visibility !== 'hidden'
                                && style.display !== 'none';
                        };
                        const options = Array.from(document.querySelectorAll('.cdk-overlay-container mat-option'))
                            .filter(isVisible);
                        for (const option of options) {
                            const isSelected = option.classList.contains('mat-selected')
                                || option.getAttribute('aria-selected') === 'true'
                                || option.querySelector('.mat-pseudo-checkbox-checked');
                            if (isSelected) {
                                option.click();
                            }
                        }
                    }"""
                )
            page.wait_for_timeout(200)

        page.wait_for_selector(f'.cdk-overlay-container mat-option:has-text("{target_text}"):visible', timeout=3000)
        target_option = page.locator(".cdk-overlay-container mat-option:visible", has_text=target_text).first
        is_selected = False
        try:
            is_selected = target_option.evaluate(
                """option => option.classList.contains('mat-selected')
                    || option.getAttribute('aria-selected') === 'true'
                    || !!option.querySelector('.mat-pseudo-checkbox-checked')"""
            )
        except Exception:
            pass
        if not is_selected:
            target_option.click(force=True, timeout=2000)
            page.wait_for_timeout(300)

        done_buttons = [
            '.cdk-overlay-container button:has-text("Done")',
            '.cdk-overlay-container button:has-text("Apply")',
            '.cdk-overlay-container [role="button"]:has-text("Done")',
            '.cdk-overlay-container [role="button"]:has-text("Apply")',
        ]
        clicked_done = False
        for selector in done_buttons:
            try:
                done = page.locator(f"{selector}:visible").first
                if done.count() > 0:
                    done.click(force=True, timeout=1500)
                    clicked_done = True
                    break
            except Exception:
                continue
        if not clicked_done:
            page.keyboard.press("Escape")

        page.wait_for_timeout(700)
        dismiss_search_overlays(page)
        return True
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        dismiss_search_overlays(page)
        return False


def open_filter_dropdown(page, label_keywords: list[str]) -> bool:
    """Open a filter dropdown by finding a nearby trigger for the given label keywords."""
    try:
        return page.evaluate(
            """(keywords) => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };

                const hasKeyword = text => keywords.some(keyword =>
                    (text || '').toLowerCase().includes(keyword.toLowerCase())
                );

                const roots = Array.from(document.querySelectorAll('app-search-teetime-filters *'))
                    .filter(isVisible);

                for (const root of roots) {
                    const text = (root.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!hasKeyword(text)) continue;

                    const container = root.closest('mat-form-field, .mat-form-field, .filter-item, .row, .col, div');
                    const scope = container || root.parentElement || root;
                    const trigger = scope.querySelector(
                        '.mat-select-trigger, [role="combobox"], mat-select, button, [role="button"]'
                    );
                    if (trigger && isVisible(trigger)) {
                        trigger.click();
                        return true;
                    }
                }
                return false;
            }""",
            label_keywords,
        )
    except Exception:
        return False


def apply_holes_filter(page, target_holes: str = "18 Holes") -> bool:
    """Select the requested Holes option, defaulting to 18 Holes."""
    expected = target_holes.lower()
    dismiss_search_overlays(page)
    if select_mat_option(page, "app-search-teetime-filters .hole-filter-control", target_holes):
        settle_after_filter_change(page)
        return (get_filter_select_value(page, "app-search-teetime-filters .hole-filter-control") or "").lower() == expected

    if click_filter_option_by_text(page, target_holes):
        settle_after_filter_change(page)
        return (get_filter_select_value(page, "app-search-teetime-filters .hole-filter-control") or "").lower() == expected

    opened = open_filter_dropdown(page, ["holes", "hole"])
    if opened:
        page.wait_for_timeout(300)
        if click_filter_option_by_text(page, target_holes) or click_filter_option_by_text(page, "18"):
            settle_after_filter_change(page)
            return (get_filter_select_value(page, "app-search-teetime-filters .hole-filter-control") or "").lower() == expected
    return False


def apply_course_filter(page, target_course: str | None) -> bool:
    """Select the requested Course by visible label text."""
    if not target_course:
        return True

    dismiss_search_overlays(page)
    if select_mat_option(page, "app-search-teetime-filters .course-filter-control", target_course, multiple=True):
        settle_after_filter_change(page)
        return target_course.lower() in (
            get_filter_select_value(page, "app-search-teetime-filters .course-filter-control") or ""
        ).lower()

    return False


def apply_players_filter(page, target_players: int) -> bool:
    """Select Players without issuing an incorrect first search like 3 -> 4."""
    dismiss_search_overlays(page)
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
        settle_after_filter_change(page)
        return True
    except Exception:
        time.sleep(0.5)
        selected = get_selected_players_label(page) == target_label
        if selected:
            settle_after_filter_change(page)
        return selected


def select_search_filters(
    page,
    target_players: int,
    target_offset_days: int,
    site: str = "paramus",
    target_course: str | None = None,
    target_holes: str = "18 Holes",
) -> bool:
    """Apply search filters without forcing Bergen-only controls on Paramus."""
    site_key = normalize_site(site)
    target_date = datetime.date.today() + datetime.timedelta(days=target_offset_days)
    page.wait_for_selector("app-search-teetime-filters button.mat-button-toggle-button:visible", timeout=10000)
    dismiss_search_overlays(page)

    try:
        page.wait_for_function(
            """() => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };

                const desktopEnabled = Array.from(
                    document.querySelectorAll(
                        'app-search-teetime-filters app-ngx-dates-picker.hidemobile button.btn-day-unit:not([disabled])'
                    )
                ).some(isVisible);
                const mobileNext = Array.from(
                    document.querySelectorAll(
                        'app-search-teetime-filters .mobile__container.showmobile .right-chevron-button button'
                    )
                ).some(isVisible);
                return desktopEnabled || mobileNext;
            }""",
            timeout=8000,
        )
    except Exception:
        time.sleep(1)

    before_day = get_selected_day_text(page)
    if not click_target_date(page, target_date, target_offset_days):
        return False

    try:
        page.wait_for_function(
            """(previousDay) => {
                const isVisible = el => {
                    if (!el) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none';
                };

                const calendar = document.querySelector(
                    'app-search-teetime-filters app-ngx-dates-picker.hidemobile'
                );
                const selected = Array.from(
                    calendar?.querySelectorAll('.day-background-upper.is-selected') || []
                ).find(isVisible);
                const currentDay = selected?.textContent?.trim() || Array.from(
                    document.querySelectorAll(
                        'app-search-teetime-filters .mobile__container.showmobile mat-label'
                    )
                ).find(isVisible)?.textContent?.trim() || null;
                return !!currentDay && currentDay !== previousDay;
            }""",
            before_day,
            timeout=5000,
        )
    except Exception:
        time.sleep(0.8)

    settle_after_filter_change(page)

    if site_key == "bergen":
        if not apply_holes_filter(page, target_holes):
            return False
        if not apply_course_filter(page, target_course):
            return False

    if site_key == "bergen" and not is_target_date_selected(page, target_date):
        dismiss_search_overlays(page)
        before_day = get_selected_day_text(page)
        if not click_target_date(page, target_date, target_offset_days):
            return False
        try:
            page.wait_for_function(
                """(previousDay) => {
                    const isVisible = el => {
                        if (!el) return false;
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none';
                    };

                    const calendar = document.querySelector(
                        'app-search-teetime-filters app-ngx-dates-picker.hidemobile'
                    );
                    const selected = Array.from(
                        calendar?.querySelectorAll('.day-background-upper.is-selected') || []
                    ).find(isVisible);
                    const currentDay = selected?.textContent?.trim() || Array.from(
                        document.querySelectorAll(
                            'app-search-teetime-filters .mobile__container.showmobile mat-label'
                        )
                    ).find(isVisible)?.textContent?.trim() || null;
                    return !!currentDay && currentDay !== previousDay;
                }""",
                before_day,
                timeout=5000,
            )
        except Exception:
            time.sleep(0.8)
        settle_after_filter_change(page)

    # Re-apply Players after the date click so the tee sheet refreshes using
    # the requested player filter before we inspect any tee times.
    if not apply_players_filter(page, target_players):
        return False

    return True


def login_paramus(page, username: str, password: str, logger):
    page.wait_for_selector("mat-form-field", timeout=20000)
    page.wait_for_selector('input[type="email"]', timeout=10000)
    logger(f"[{datetime.datetime.now()}] Injecting email and clicking NEXT via JS...")
    page.evaluate(
        """(username) => {
            let el = document.querySelector('input[type="email"]');
            if (!el) return;
            el.removeAttribute('readonly');
            el.dispatchEvent(new FocusEvent('focus', { bubbles: true }));
            el.value = username;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new FocusEvent('blur', { bubbles: true }));
            setTimeout(() => {
                let btn = Array.from(document.querySelectorAll('button'))
                    .find(b => b.textContent?.includes('NEXT'));
                if (btn) btn.click();
            }, 500);
        }""",
        username,
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


def login_bergen(page, username: str, password: str, logger):
    username_ready = False
    for attempt in range(2):
        try:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            page.wait_for_selector('input[formcontrolname="username"]:visible', timeout=30000)
            username_ready = True
            break
        except Exception:
            if attempt == 1:
                raise
            logger(f"[{datetime.datetime.now()}] Bergen verify page did not render cleanly. Retrying once...")
            page.goto(get_site_config("bergen")["login_url"])

    if not username_ready:
        raise RuntimeError("Bergen username input did not become visible.")

    logger(f"[{datetime.datetime.now()}] Filling username and advancing to password step...")
    page.locator('input[formcontrolname="username"]:visible').first.fill(username)
    page.wait_for_timeout(300)
    page.locator('button:has-text("NEXT")').last.click(force=True)
    try:
        page.wait_for_url("**/auth/login", timeout=20000)
    except Exception:
        pass
    page.wait_for_selector('input[type="password"]:visible', timeout=20000)
    logger(f"[{datetime.datetime.now()}] Password UI detected. Submitting Bergen sign-in...")
    page.locator('input[type="password"]:visible').first.fill(password)
    page.wait_for_timeout(300)
    page.locator('button:has-text("SIGN IN")').last.click(force=True)


def login_to_site(page, site: str, username: str, password: str, logger):
    if normalize_site(site) == "bergen":
        login_bergen(page, username, password, logger)
    else:
        login_paramus(page, username, password, logger)


def run_booking(
    dry_run: bool = False,
    force_run: bool = False,
    log_callback=None,
    site: str = "paramus",
    target_course: str | None = None,
    target_holes: str = "18 Holes",
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

    site_key = "paramus"
    site_config = get_site_config(site_key)
    target_course = resolve_default_course(site_key, target_course)

    if not (email and password):
        email, password = require_credentials(site_key)

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
        browser = p.chromium.launch(headless=use_headless_browser(site_key))
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        stealth_sync(page)
        page.on("dialog", lambda dialog: dialog.accept())

        logger(
            f"[{datetime.datetime.now()}] Navigating to {site_config['display_name']} login page...",
            is_important=True,
        )
        page.goto(site_config["login_url"])
        try:
            login_to_site(page, site_key, email, password, logger)
            logger(f"[{datetime.datetime.now()}] Credentials submitted.")
        except Exception as e:
            logger(f"[{datetime.datetime.now()}] Login failed or already logged in ... {e}", is_important=True)
            browser.close()
            raise

        page.wait_for_url("**/search-teetime", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        for popup_attempt in range(3):
            if dismiss_search_overlays(page):
                logger(f"[{datetime.datetime.now()}] Dismissed post-login popup ({popup_attempt + 1}/3).")
                page.wait_for_timeout(500)
            else:
                break
        post_login_snapshot = get_results_debug_snapshot(page)
        if post_login_snapshot.get("visibleBackdrops") or post_login_snapshot.get("dialogTexts"):
            logger(
                f"[{datetime.datetime.now()}] Post-login overlay still visible. "
                f"{format_debug_snapshot(post_login_snapshot)}",
                is_important=True,
            )
            save_debug_artifacts(page, f"{site_key}_post_login_overlay", logger)
        logger(
            f"[{datetime.datetime.now()}] Login successful on {site_config['display_name']}. On search page.",
            is_important=True,
        )

        # Handle unexpected redirects to checkout/reservation pages (leftover sessions)
        current_url = page.url.lower()
        if "/checkout" in current_url or "/reservation" in current_url:
            logger(f"[{datetime.datetime.now()}] Detected unexpected checkout page ({current_url}). Redirecting to search...", is_important=True)
            page.goto(site_config["search_url"])
            page.wait_for_url("**/search-teetime", timeout=20000)
            dismiss_search_overlays(page)

        target_date = datetime.date.today() + datetime.timedelta(days=target_offset_days)
        offset_text = f"T+{target_offset_days}"
        logger(
            f"[{datetime.datetime.now()}] Pre-selecting date {target_date} ({offset_text}) "
            f"then {target_players} Players, Course={target_course or 'Any'}, Holes={target_holes}...",
            is_important=True,
        )
        for click_attempt in range(5):
            if stop_if_requested("initial filter application"):
                return
            if select_search_filters(
                page,
                target_players,
                target_offset_days,
                site=site_key,
                target_course=target_course,
                target_holes=target_holes,
            ):
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
            snapshot = get_results_debug_snapshot(page)
            logger(
                f"[{datetime.datetime.now()}] Filter application failed, retrying ({click_attempt + 1}/5). "
                f"{format_debug_snapshot(snapshot)}",
                is_important=True,
            )
            save_debug_artifacts(page, f"{site_key}_date_click_failed_{click_attempt + 1}", logger)
            if not interruptible_sleep(1, stop_event):
                return
        else:
            raise RuntimeError(
                f"Failed to apply date/player filters after 5 attempts ({offset_text}, {target_players} Players)."
            )

        initial_snapshot = get_results_debug_snapshot(page)
        logger(
            f"[{datetime.datetime.now()}] Initial search state after filters: "
            f"{format_debug_snapshot(initial_snapshot)}",
            is_important=True,
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
            if select_search_filters(
                page,
                target_players,
                target_offset_days,
                site=site_key,
                target_course=target_course,
                target_holes=target_holes,
            ):
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
                    settle_after_filter_change(page)
                    snapshot = get_results_debug_snapshot(page)
                    logger(
                        f"[{datetime.datetime.now()}] (Attempt {attempt}) Found 0 times. "
                        f"Re-applying date -> Players filters... "
                        f"{format_debug_snapshot(snapshot)}",
                        is_important=(attempt == 1 or attempt % 10 == 0),
                    )
                    if attempt == 1 or attempt % 10 == 0:
                        save_debug_artifacts(page, f"{site_key}_no_tee_times_attempt_{attempt}", logger)
                    select_search_filters(
                        page,
                        target_players,
                        target_offset_days,
                        site=site_key,
                        target_course=target_course,
                        target_holes=target_holes,
                    )
                    if not interruptible_sleep(1, stop_event):
                        break
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
                            logger(f"[{datetime.datetime.now()}] Found matching time: {time_text} {am_pm}", is_important=True)
                            target_btn = card
                            target_time_key = time_key
                            break
                    except Exception as e:
                        continue
                
                if target_btn:
                    # Press Escape to clear any rogue "Too Early" Angular modals from previous clicks
                    page.keyboard.press("Escape")
                    target_btn.click(force=True)
                    logger(f"[{datetime.datetime.now()}] Time selected. Finalizing...", is_important=True)
                else:
                    logger(
                        f"[{datetime.datetime.now()}] Tee times exist but none match "
                        f"{target_start_time}-{target_end_time}. Retrying..."
                    )
                    if not interruptible_sleep(2, stop_event):
                        break
                    continue

                checkout_ready = wait_for_checkout_ready(page, expected_guests)
                if not interruptible_sleep(0.3, stop_event):
                    break

                actual_guests = count_guest_checkboxes(page)
                logger(
                    f"[{datetime.datetime.now()}] Found {actual_guests} Guest checkbox(es) on page "
                    f"(need {expected_guests}, checkout_ready={checkout_ready})."
                )

                if actual_guests < expected_guests:
                    if not checkout_ready and wait_for_checkout_ready(page, expected_guests, timeout_ms=10000):
                        actual_guests = count_guest_checkboxes(page)
                        logger(
                            f"[{datetime.datetime.now()}] Checkout finished after extra wait. "
                            f"Guest checkbox(es): {actual_guests}/{expected_guests}."
                        )

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
                        page.goto(site_config["search_url"])
                        page.wait_for_url("**/search-teetime", timeout=30000)
                        select_search_filters(
                            page,
                            target_players,
                            target_offset_days,
                            site=site_key,
                            target_course=target_course,
                            target_holes=target_holes,
                        )
                    except Exception as nav_err:
                        logger(f"[{datetime.datetime.now()}] Navigation back failed: {nav_err}")
                    continue

                click_unchecked_guest_checkboxes(page)
                if not interruptible_sleep(0.4, stop_event):
                    break

                if stop_if_requested("guest selection"):
                    break

                if not interruptible_sleep(0.5, stop_event):
                    break

                for _retry in range(2):
                    if count_checked_guest_checkboxes(page) >= actual_guests:
                        break
                    click_unchecked_guest_checkboxes(page)
                    if not interruptible_sleep(0.5, stop_event):
                        break

                final_checked = count_checked_guest_checkboxes(page)
                logger(
                    f"[{datetime.datetime.now()}] Guests checked: {final_checked}/{expected_guests} confirmed.",
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
                        page.goto(site_config["search_url"])
                        page.wait_for_url("**/search-teetime", timeout=30000)
                        select_search_filters(
                            page,
                            target_players,
                            target_offset_days,
                            site=site_key,
                            target_course=target_course,
                            target_holes=target_holes,
                        )
                    except Exception as nav_err:
                        logger(f"[{datetime.datetime.now()}] Navigation back failed: {nav_err}")
                    continue

                if site_key == "bergen" and click_continue_to_review_if_present(page):
                    logger(f"[{datetime.datetime.now()}] Continue clicked. Waiting for review/confirm step...", is_important=True)
                    try:
                        page.wait_for_selector(FINALIZE_SELECTOR, timeout=8000)
                    except Exception:
                        snapshot = get_results_debug_snapshot(page)
                        logger(
                            f"[{datetime.datetime.now()}] Review/confirm step did not show finalize button. "
                            f"{format_debug_snapshot(snapshot)}",
                            is_important=True,
                        )
                        save_debug_artifacts(page, f"{site_key}_review_confirm_missing_finalize", logger)
                        if dry_run:
                            hold_seconds = get_dry_run_hold_seconds()
                            logger(
                                f"[{datetime.datetime.now()}] DRY RUN: Holding current reservation screen "
                                f"for {hold_seconds}s instead of returning to search.",
                                is_important=True,
                            )
                            interruptible_sleep(hold_seconds, stop_event)
                            break
                        page.keyboard.press("Escape")
                        continue
                else:
                    try:
                        page.wait_for_selector(FINALIZE_SELECTOR, timeout=8000)
                    except Exception:
                        snapshot = get_results_debug_snapshot(page)
                        logger(
                            f"[{datetime.datetime.now()}] Finalize button didn't appear and no Continue button was found. "
                            f"{format_debug_snapshot(snapshot)}",
                            is_important=True,
                        )
                        save_debug_artifacts(page, f"{site_key}_missing_continue_or_finalize", logger)
                        if dry_run:
                            hold_seconds = get_dry_run_hold_seconds()
                            logger(
                                f"[{datetime.datetime.now()}] DRY RUN: Holding current reservation screen "
                                f"for {hold_seconds}s instead of returning to search.",
                                is_important=True,
                            )
                            interruptible_sleep(hold_seconds, stop_event)
                            break
                        page.keyboard.press("Escape")
                        continue

                if dry_run:
                    hold_seconds = get_dry_run_hold_seconds()
                    logger(
                        f"[{datetime.datetime.now()}] DRY RUN: 'Finalize Reservation' is visible. "
                        f"Skipping final click and holding for {hold_seconds}s.",
                        is_important=True,
                    )
                    interruptible_sleep(hold_seconds, stop_event)
                    break
                else:
                    try:
                        # Finalize using the Playwright locator for reliability.
                        page.locator('button:has-text("Finalize Reservation")').click(timeout=5000)
                        if confirm_final_reservation_prompt(page):
                            logger(f"[{datetime.datetime.now()}] Final confirmation accepted.", is_important=True)
                        else:
                            snapshot = get_results_debug_snapshot(page)
                            logger(
                                f"[{datetime.datetime.now()}] Finalize clicked, but no final confirmation button was found. "
                                f"{format_debug_snapshot(snapshot)}",
                                is_important=True,
                            )
                            save_debug_artifacts(page, f"{site_key}_final_confirm_missing", logger)
                        logger(f"[{datetime.datetime.now()}] Booking finalized!", is_important=True)
                        interruptible_sleep(5, stop_event)
                        break # BREAK out of retry loop on success!
                    except Exception as e:
                        snapshot = get_results_debug_snapshot(page)
                        logger(
                            f"[{datetime.datetime.now()}] Could not click finalize button: {e}. "
                            f"{format_debug_snapshot(snapshot)}",
                            is_important=True,
                        )
                        save_debug_artifacts(page, f"{site_key}_finalize_click_failed", logger)
                        dismiss_search_overlays(page)
                        continue

            except Exception as e:
                snapshot = get_results_debug_snapshot(page)
                logger(
                    f"[{datetime.datetime.now()}] Unexpected error: {e}. "
                    f"{format_debug_snapshot(snapshot)}",
                    is_important=True,
                )
                save_debug_artifacts(page, f"{site_key}_unexpected_error", logger)
                logger(f"[{datetime.datetime.now()}] Refreshing to try again...")
                try:
                    page.goto(site_config["search_url"])
                    page.wait_for_url("**/search-teetime", timeout=30000)
                    select_search_filters(
                        page,
                        target_players,
                        target_offset_days,
                        site=site_key,
                        target_course=target_course,
                        target_holes=target_holes,
                    )
                except Exception as refresh_err:
                    logger(f"[{datetime.datetime.now()}] Refresh after error failed: {refresh_err}", is_important=True)
                if not interruptible_sleep(2, stop_event):
                    break

        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CPS Golf Booking Bot")
    parser.add_argument("--site", type=str, default="paramus", choices=sorted(SUPPORTED_SITES), help="Target golf site")
    parser.add_argument("--dry-run", action="store_true", help="Skip final booking click")
    parser.add_argument("--force-run", action="store_true", help="Run immediately without waiting for 7 AM")
    parser.add_argument("--course", type=str, default=None, help="Course label to select")
    parser.add_argument("--holes", type=str, default="18 Holes", help="Holes option to select")
    parser.add_argument("--offset", type=int, default=2, help="Days ahead to book (0=today)")
    parser.add_argument("--start-time", type=str, default="11:30", help="Earliest tee time (HH:MM 24h)")
    parser.add_argument("--end-time", type=str, default="15:00", help="Latest tee time (HH:MM 24h)")
    parser.add_argument("--players", type=int, default=4, help="Number of players (1-4)")
    args = parser.parse_args()

    site_config = get_site_config(args.site)

    print("=======================================", flush=True)
    print(f" {site_config['display_name']} Golf Bot Started              ", flush=True)
    print(f" Site      : {args.site}", flush=True)
    print(f" Dry Run   : {args.dry_run}", flush=True)
    print(f" Force Run : {args.force_run}", flush=True)
    print(f" Course    : {args.course or 'Any'}", flush=True)
    print(f" Holes     : {args.holes}", flush=True)
    print(f" Offset    : T+{args.offset} (Days)", flush=True)
    print(f" Time      : {args.start_time} ~ {args.end_time}", flush=True)
    print(f" Players   : {args.players}", flush=True)
    print("=======================================", flush=True)

    run_booking(
        dry_run=args.dry_run,
        force_run=args.force_run,
        site=args.site,
        target_course=args.course,
        target_holes=args.holes,
        target_offset_days=args.offset,
        target_start_time=args.start_time,
        target_end_time=args.end_time,
        target_players=args.players,
    )
