import os
import threading

import telebot
from dotenv import load_dotenv

from paramus_booker import run_booking


load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
EMAIL = os.getenv("PARAMUS_EMAIL")
PASSWORD = os.getenv("PARAMUS_PASSWORD")

if not BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN not found in .env file or environment.")
    print("Please create a .env file or set TELEGRAM_BOT_TOKEN in your environment.")
    raise SystemExit(1)


bot = telebot.TeleBot(BOT_TOKEN)
booking_thread = None
booking_stop_event = None

# Per-chat credential store: {chat_id: {'email': str, 'password': str}}
user_credentials: dict[int, dict] = {}
# Conversation state while collecting credentials: {chat_id: 'email' | 'password'}
pending_creds: dict[int, str] = {}


def run_booking_async(
    chat_id,
    dry_run,
    force_run,
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
        run_booking(
            dry_run=dry_run,
            force_run=force_run,
            log_callback=send_log,
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
        "*Paramus Golf Booking Bot*\n\n"
        "Commands\n"
        "/creds\n"
        "Set golf site email/password\n\n"
        "/book [offset] [start_time] [end_time] [players]\n"
        "Example: `/book 2 11:30 15:00 4`\n"
        "Start a real booking attempt\n\n"
        "/test [offset] [start_time] [end_time] [players]\n"
        "Example: `/test 1 14:00 17:00 4`\n"
        "Run in dry-run mode without final confirmation\n\n"
        "/status\n"
        "Show current run status\n\n"
        "/stop\n"
        "Request stop during repeated search"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")


@bot.message_handler(commands=["creds"])
def cmd_creds(message):
    pending_creds[message.chat.id] = "email"
    bot.reply_to(message, "Enter your golf site email.")


@bot.message_handler(func=lambda m: m.chat.id in pending_creds and not m.text.startswith("/"))
def handle_pending_creds(message):
    chat_id = message.chat.id
    state = pending_creds.get(chat_id)
    text = message.text.strip()

    if state == "email":
        user_credentials[chat_id] = {"email": text, "password": None}
        pending_creds[chat_id] = "password"
        bot.reply_to(message, "Enter your password.")

    elif state == "password":
        user_credentials[chat_id]["password"] = text
        del pending_creds[chat_id]
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
        email = user_credentials[chat_id]["email"]
        bot.send_message(
            chat_id,
            f"Saved.\nEmail: {email}\nPassword: {'*' * len(text)}",
        )


def get_args_from_message(message):
    args = message.text.split()
    offset = 2
    start_time = "11:30"
    end_time = "15:00"
    players = 4

    if len(args) > 1:
        try:
            offset = int(args[1])
            if offset < 0 or offset > 5:
                raise ValueError
        except Exception:
            return None, None, None, None

    if len(args) > 3:
        start_time = args[2]
        end_time = args[3]

    if len(args) > 4:
        try:
            players = int(args[4])
            if players < 1 or players > 4:
                raise ValueError
        except Exception:
            return None, None, None, None

    return offset, start_time, end_time, players


def _resolve_credentials(message) -> tuple[str | None, str | None]:
    """Return (email, password) from per-chat store, falling back to env vars."""
    creds = user_credentials.get(message.chat.id, {})
    email = creds.get("email") or EMAIL
    password = creds.get("password") or PASSWORD
    if not email or not password:
        bot.reply_to(
            message,
            "Missing login info. Run /creds first, or set PARAMUS_EMAIL and PARAMUS_PASSWORD.",
        )
        return None, None
    return email, password


def _start_booking_thread(message, dry_run: bool, force_run: bool):
    global booking_thread, booking_stop_event

    offset, start_time, end_time, players = get_args_from_message(message)
    if offset is None:
        example = "/test 2 11:30 15:00 4" if dry_run else "/book 2 11:30 15:00 4"
        bot.reply_to(message, f"Invalid option format. Example: {example}")
        return

    email, password = _resolve_credentials(message)
    if not email:
        return

    if booking_thread and booking_thread.is_alive():
        bot.reply_to(message, "Another booking job is already running. Use /stop first if needed.")
        return

    booking_stop_event = threading.Event()
    mode_text = "Starting dry-run mode." if dry_run else "Starting booking run."
    finalize_text = (
        "Final confirmation will be skipped."
        if dry_run
        else "Important progress updates will be sent here."
    )
    bot.reply_to(
        message,
        f"{mode_text}\n"
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
    _start_booking_thread(message, dry_run=False, force_run=False)


@bot.message_handler(commands=["test"])
def cmd_testbook(message):
    _start_booking_thread(message, dry_run=True, force_run=True)


@bot.message_handler(commands=["status"])
def cmd_status(message):
    global booking_thread, booking_stop_event

    if booking_thread and booking_thread.is_alive():
        stop_state = "stop requested" if booking_stop_event and booking_stop_event.is_set() else "running"
        bot.reply_to(message, f"Booking bot status: {stop_state}.")
    else:
        bot.reply_to(message, "No active booking job. Use /book to start one.")


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    global booking_thread, booking_stop_event

    if booking_thread and booking_thread.is_alive() and booking_stop_event:
        booking_stop_event.set()
        bot.reply_to(message, "Stop requested. The bot will exit safely at the next wait or retry point.")
    else:
        bot.reply_to(message, "No active booking job to stop.")


print("======================================")
print("Telegram bot is listening continuously...")
print("Press Ctrl+C to stop.")
print("======================================")
bot.infinity_polling()
