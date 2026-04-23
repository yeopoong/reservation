from __future__ import annotations

import os
import json
import threading
from pathlib import Path

import telebot
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

from bergen_booker import run_booking as run_bergen_booking
from paramus_booker import run_booking as run_paramus_booking


load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_EMAIL = os.getenv("PARAMUS_EMAIL")
DEFAULT_PASSWORD = os.getenv("PARAMUS_PASSWORD")
CREDENTIALS_KEY = os.getenv("TELEGRAM_CREDENTIALS_KEY")
CREDENTIALS_PATH = Path(__file__).with_name("telegram_credentials.enc")

if not BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN not found in .env file or environment.")
    print("Please create a .env file or set TELEGRAM_BOT_TOKEN in your environment.")
    raise SystemExit(1)

if not CREDENTIALS_KEY:
    print("ERROR: TELEGRAM_CREDENTIALS_KEY not found in .env file or environment.")
    print("Set TELEGRAM_CREDENTIALS_KEY to a Fernet key so Telegram credentials are stored encrypted.")
    raise SystemExit(1)

try:
    Fernet(CREDENTIALS_KEY.encode("utf-8"))
except Exception as exc:
    print("ERROR: TELEGRAM_CREDENTIALS_KEY is invalid.")
    print("Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
    raise SystemExit(1) from exc


bot = telebot.TeleBot(BOT_TOKEN)
booking_thread = None
booking_stop_event = None

# Per-chat credential store: {chat_id_str: {'paramus': {'email': str, 'password': str}, 'bergen': {...}}}
user_credentials: dict[str, dict] = {}
# Conversation state while collecting credentials:
# {chat_id: {'site': str, 'field': 'email' | 'password'}}
pending_creds: dict[int, dict] = {}
user_sites: dict[int, str] = {}


def _chat_key(chat_id: int) -> str:
    return str(chat_id)


def reply_handler_error(message, command_name: str, exc: Exception):
    print(f"Telegram command {command_name} failed: {exc}", flush=True)
    try:
        bot.reply_to(message, f"{command_name} failed on the server: {exc}")
    except Exception:
        pass


def _get_credentials_cipher() -> Fernet:
    try:
        return Fernet(CREDENTIALS_KEY.encode("utf-8"))
    except Exception as exc:
        raise SystemExit("Invalid TELEGRAM_CREDENTIALS_KEY. Generate a valid Fernet key.") from exc


def load_credentials_store() -> dict[str, dict]:
    if not CREDENTIALS_PATH.exists():
        return {}

    cipher = _get_credentials_cipher()
    try:
        encrypted = CREDENTIALS_PATH.read_bytes()
        if not encrypted:
            return {}
        decrypted = cipher.decrypt(encrypted)
        data = json.loads(decrypted.decode("utf-8"))
    except InvalidToken as exc:
        raise SystemExit(
            "Failed to decrypt telegram_credentials.enc. Check TELEGRAM_CREDENTIALS_KEY."
        ) from exc
    except Exception as exc:
        raise SystemExit(f"Failed to load encrypted credential store: {exc}") from exc

    if not isinstance(data, dict):
        raise SystemExit("Encrypted credential store is malformed.")

    if "credentials" in data or "sites" in data:
        return data

    # Backward compatibility for the first encrypted-store format where the
    # file contained only the credential map.
    return {
        "credentials": {
            str(chat_id): sites for chat_id, sites in data.items() if isinstance(sites, dict)
        },
        "sites": {},
    }


def save_credentials_store():
    cipher = _get_credentials_cipher()
    payload = json.dumps(
        {
            "credentials": user_credentials,
            "sites": {str(chat_id): site for chat_id, site in user_sites.items()},
        },
        ensure_ascii=True,
        sort_keys=True,
    ).encode("utf-8")
    encrypted = cipher.encrypt(payload)
    tmp_path = CREDENTIALS_PATH.with_suffix(".enc.tmp")
    tmp_path.write_bytes(encrypted)
    tmp_path.replace(CREDENTIALS_PATH)


def try_save_credentials_store(chat_id: int | None = None) -> bool:
    try:
        save_credentials_store()
        return True
    except Exception as exc:
        print(f"Failed to save encrypted Telegram credentials: {exc}", flush=True)
        if chat_id is not None:
            try:
                bot.send_message(
                    chat_id,
                    "Credential save failed on the server. Check file permissions and TELEGRAM_CREDENTIALS_KEY.",
                )
            except Exception:
                pass
        return False


stored_state = load_credentials_store()
user_credentials = stored_state.get("credentials", {})
user_sites = {
    int(chat_id): site
    for chat_id, site in stored_state.get("sites", {}).items()
    if str(chat_id).isdigit() and site in {"paramus", "bergen"}
}


def run_booking_async(
    chat_id,
    dry_run,
    force_run,
    site="paramus",
    course=None,
    offset=2,
    start_time="11:30",
    end_time="15:00",
    players=4,
    email=None,
    password=None,
    stop_event=None,
):
    global booking_thread, booking_stop_event

    def send_log(msg):
        try:
            bot.send_message(chat_id, msg)
        except Exception as e:
            print(f"Failed to send telegram msg: {e}")

    try:
        booking_runner = run_bergen_booking if site == "bergen" else run_paramus_booking
        booking_runner(
            dry_run=dry_run,
            force_run=force_run,
            log_callback=send_log,
            site=site,
            target_course=course,
            target_holes="18 Holes",
            target_offset_days=offset,
            target_start_time=start_time,
            target_end_time=end_time,
            target_players=players,
            email=email,
            password=password,
            stop_event=stop_event,
        )
    except Exception as e:
        bot.send_message(chat_id, f"Booking bot failed: {e}")
    finally:
        booking_thread = None
        booking_stop_event = None


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    welcome_text = (
        "*CPS Golf Booking Bot*\n\n"
        "Commands\n"
        "/site [paramus|bergen]\n"
        "Choose target site\n\n"
        "/creds [paramus|bergen]\n"
        "Set login/password for one site only\n\n"
        "/book [site] [course] [offset] [start_time] [end_time] [players]\n"
        "Example: `/book bergen 2 11:30 15:00 4`\n"
        "Start a real booking attempt at the normal 7 AM schedule\n\n"
        "/run [site] [course] [offset] [start_time] [end_time] [players]\n"
        "Example: `/run paramus 2 14:00 17:00 2`\n"
        "Start a real booking attempt immediately\n\n"
        "/test [site] [course] [offset] [start_time] [end_time] [players]\n"
        "Example: `/test paramus 2 14:00 17:00 2`\n"
        "Dry-run only. It will not click final confirmation or reserve the tee time\n\n"
        "Notes\n"
        "- Default `site` is `paramus`\n"
        "- `site` is optional if you already used `/site`\n"
        "- `course` is optional and matched by visible text\n"
        "- Paramus uses its site default course automatically\n"
        "- Default Bergen `course` is `Overpeck 18`\n"
        "- Holes Selection is always fixed to `18 Holes`\n\n"
        "/status\n"
        "Show current run status\n\n"
        "/stop\n"
        "Request stop during repeated search"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")


@bot.message_handler(commands=["creds"])
def cmd_creds(message):
    try:
        args = message.text.split()
        site = user_sites.get(message.chat.id, "paramus")
        if len(args) > 1:
            if args[1].lower() not in {"paramus", "bergen"}:
                bot.reply_to(message, "Usage: /creds paramus or /creds bergen")
                return
            site = args[1].lower()
        user_sites[message.chat.id] = site
        pending_creds[message.chat.id] = {"site": site, "field": "email"}
        bot.reply_to(message, f"Enter your {site} login ID.")
    except Exception as exc:
        reply_handler_error(message, "/creds", exc)


@bot.message_handler(commands=["site"])
def cmd_site(message):
    try:
        args = message.text.split()
        if len(args) < 2 or args[1].lower() not in {"paramus", "bergen"}:
            bot.reply_to(message, "Usage: /site paramus or /site bergen")
            return
        site = args[1].lower()
        user_sites[message.chat.id] = site
        try_save_credentials_store(message.chat.id)
        bot.reply_to(message, f"Target site set to {site}.")
    except Exception as exc:
        reply_handler_error(message, "/site", exc)


@bot.message_handler(func=lambda m: bool(m.text) and m.chat.id in pending_creds and not m.text.startswith("/"))
def handle_pending_creds(message):
    try:
        chat_id = message.chat.id
        chat_key = _chat_key(chat_id)
        state = pending_creds.get(chat_id) or {}
        site = state.get("site", user_sites.get(chat_id, "paramus"))
        field = state.get("field")
        text = message.text.strip()

        if field == "email":
            user_credentials.setdefault(chat_key, {})
            user_credentials[chat_key][site] = {"email": text, "password": None}
            user_sites[chat_id] = site
            pending_creds[chat_id] = {"site": site, "field": "password"}
            bot.reply_to(message, f"Enter your {site} password.")

        elif field == "password":
            user_credentials.setdefault(chat_key, {})
            user_credentials[chat_key].setdefault(site, {"email": None, "password": None})
            user_credentials[chat_key][site]["password"] = text
            user_sites[chat_id] = site
            if not try_save_credentials_store(chat_id):
                return
            del pending_creds[chat_id]
            try:
                bot.delete_message(chat_id, message.message_id)
            except Exception:
                pass
            email = user_credentials[chat_key][site]["email"]
            bot.send_message(
                chat_id,
                f"Saved for {site}.\nLogin ID: {email}\nPassword: {'*' * len(text)}",
            )
    except Exception as exc:
        reply_handler_error(message, "credential input", exc)


def get_args_from_message(message):
    args = [part.strip().rstrip(",") for part in message.text.split()]
    site = user_sites.get(message.chat.id, "paramus")
    arg_index = 1
    if len(args) > 1 and args[1].lower() in {"paramus", "bergen"}:
        site = args[1].lower()
        arg_index = 2

    course = None
    offset = 2
    start_time = "11:30"
    end_time = "15:00"
    players = 4

    if len(args) > arg_index:
        maybe_course = args[arg_index]
        if ":" not in maybe_course:
            try:
                offset = int(maybe_course)
            except Exception:
                course = maybe_course
                arg_index += 1

    if len(args) > arg_index:
        try:
            offset = int(args[arg_index])
            if offset < 0 or offset > 5:
                raise ValueError
        except Exception:
            return None, None, None, None, None, None

    if len(args) > arg_index + 2:
        start_time = args[arg_index + 1]
        end_time = args[arg_index + 2]

    if len(args) > arg_index + 3:
        try:
            players = int(args[arg_index + 3])
            if players < 1 or players > 4:
                raise ValueError
        except Exception:
            return None, None, None, None, None, None

    return site, course, offset, start_time, end_time, players


def _resolve_credentials(message, site: str) -> tuple[str | None, str | None]:
    """Return (email, password) from per-chat store, falling back to env vars."""
    creds = user_credentials.get(_chat_key(message.chat.id), {}).get(site, {})
    if site == "bergen":
        env_user = os.getenv("BERGEN_USERNAME")
        env_password = os.getenv("BERGEN_PASSWORD")
    else:
        env_user = DEFAULT_EMAIL
        env_password = DEFAULT_PASSWORD
    email = creds.get("email") or env_user
    password = creds.get("password") or env_password
    if not email or not password:
        bot.reply_to(
            message,
            "Missing login info. Run /creds first, or set site credentials in environment.",
        )
        return None, None
    return email, password


def _start_booking_thread(message, dry_run: bool, force_run: bool):
    global booking_thread, booking_stop_event

    site, course, offset, start_time, end_time, players = get_args_from_message(message)
    if site is None:
        example = "/test bergen 2 11:30 15:00 4" if dry_run else "/book bergen 2 11:30 15:00 4"
        bot.reply_to(message, f"Invalid option format. Example: {example}")
        return

    if site == "bergen" and not course:
        course = "Overpeck 18"

    email, password = _resolve_credentials(message, site)
    if not email:
        return

    if booking_thread and booking_thread.is_alive():
        bot.reply_to(message, "Another booking job is already running. Use /stop first if needed.")
        return

    booking_stop_event = threading.Event()
    if dry_run:
        mode_text = "Starting dry-run mode."
        finalize_text = "Dry-run only: final confirmation will be skipped, so no reservation will be made."
    elif force_run:
        mode_text = "Starting immediate booking run."
        finalize_text = "This is a real booking attempt and will run immediately."
    else:
        mode_text = "Starting booking run."
        finalize_text = "This is a real booking attempt and will wait for the normal 7 AM schedule."
    bot.reply_to(
        message,
        f"{mode_text}\n"
        f"- Site: {site}\n"
        f"- Course: {course or 'Any'}\n"
        f"- Holes: 18 Holes\n"
        f"- Date offset: T+{offset}\n"
        f"- Time range: {start_time} ~ {end_time}\n"
        f"- Players: {players}\n"
        f"- Stop command: /stop\n"
        f"{finalize_text}",
    )

    booking_thread = threading.Thread(
        target=run_booking_async,
        args=(
            message.chat.id,
            dry_run,
            force_run,
            site,
            course,
            offset,
            start_time,
            end_time,
            players,
            email,
            password,
            booking_stop_event,
        ),
        daemon=True,
    )
    booking_thread.start()


@bot.message_handler(commands=["book"])
def cmd_book(message):
    try:
        _start_booking_thread(message, dry_run=False, force_run=False)
    except Exception as exc:
        reply_handler_error(message, "/book", exc)


@bot.message_handler(commands=["run"])
def cmd_run(message):
    try:
        _start_booking_thread(message, dry_run=False, force_run=True)
    except Exception as exc:
        reply_handler_error(message, "/run", exc)


@bot.message_handler(commands=["test"])
def cmd_testbook(message):
    try:
        _start_booking_thread(message, dry_run=True, force_run=True)
    except Exception as exc:
        reply_handler_error(message, "/test", exc)


@bot.message_handler(commands=["status"])
def cmd_status(message):
    try:
        global booking_thread, booking_stop_event

        if booking_thread and booking_thread.is_alive():
            stop_state = "stop requested" if booking_stop_event and booking_stop_event.is_set() else "running"
            bot.reply_to(message, f"Booking bot status: {stop_state}.")
        else:
            bot.reply_to(message, "No active booking job. Use /book to start one.")
    except Exception as exc:
        reply_handler_error(message, "/status", exc)


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    try:
        global booking_thread, booking_stop_event

        if booking_thread and booking_thread.is_alive() and booking_stop_event:
            booking_stop_event.set()
            bot.reply_to(message, "Stop requested. The bot will exit safely at the next wait or retry point.")
        else:
            bot.reply_to(message, "No active booking job to stop.")
    except Exception as exc:
        reply_handler_error(message, "/stop", exc)


def main():
    print("======================================")
    print("Telegram bot is listening continuously...")
    print("Press Ctrl+C to stop.")
    print("======================================")
    bot.infinity_polling(skip_pending=True)


if __name__ == "__main__":
    main()
