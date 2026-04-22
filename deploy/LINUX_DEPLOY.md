# Linux Deployment

## 1. Install system packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

If you want to run non-headless on a server without a desktop session:

```bash
sudo apt install -y xvfb
```

## 2. Copy project

```bash
sudo mkdir -p /opt/reservation
sudo chown "$USER":"$USER" /opt/reservation
cd /opt/reservation
```

Copy the project files into `/opt/reservation`.

## 3. Create virtualenv and install Python dependencies

```bash
cd /opt/reservation
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install playwright pyTelegramBotAPI python-dotenv
playwright install
playwright install-deps
```

## 4. Configure environment

Create `.env` from `.env.example`.

```bash
cp .env.example .env
```

Edit `.env` and set:

- `TELEGRAM_BOT_TOKEN`
- `PARAMUS_EMAIL`
- `PARAMUS_PASSWORD`
- `PARAMUS_HEADLESS`

Notes:

- Use `PARAMUS_HEADLESS=true` for a normal headless server test.
- If the booking site behaves poorly in headless mode, set `PARAMUS_HEADLESS=false` and run with Xvfb.

## 5. Manual run

Headless:

```bash
cd /opt/reservation
source .venv/bin/activate
python telegram_bot.py
```

Headful via Xvfb:

```bash
cd /opt/reservation
source .venv/bin/activate
xvfb-run -a python telegram_bot.py
```

## 6. systemd service

Copy the service file:

```bash
sudo cp deploy/paramus-bot.service /etc/systemd/system/paramus-bot.service
```

If your Linux username is not `ubuntu`, edit the `User=` line first.

Then enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable paramus-bot
sudo systemctl start paramus-bot
sudo systemctl status paramus-bot
```

Logs:

```bash
journalctl -u paramus-bot -f
```

## 7. Xvfb with systemd

If you need Xvfb, update `ExecStart=` in the service file to:

```ini
ExecStart=/usr/bin/xvfb-run -a /opt/reservation/.venv/bin/python /opt/reservation/telegram_bot.py
```
