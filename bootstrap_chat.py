#!/usr/bin/env python3
"""Find Vishal's Telegram chat id from the bot's inbox and cache it in data/chat_id.txt."""
import json, os, requests

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PATH = "data/chat_id.txt"
os.makedirs("data", exist_ok=True)

chat_id = ""
if os.path.exists(PATH):
    chat_id = open(PATH).read().strip()

if not chat_id and TOKEN:
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=15).json()
        for u in reversed(r.get("result", [])):
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat", {})
            if chat.get("type") == "private":
                chat_id = str(chat["id"])
                break
        if chat_id:
            open(PATH, "w").write(chat_id)
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          json={"chat_id": chat_id,
                                "text": "✅ EV Stack scanner connected. You'll get a message here "
                                        "whenever a line clears your action threshold vs Pinnacle."},
                          timeout=15)
    except Exception as e:
        print("bootstrap error:", e)

print("chat_id:", chat_id or "(not found yet — send the bot a message)")
with open(os.environ.get("GITHUB_OUTPUT", "/dev/null"), "a") as f:
    f.write(f"chat_id={chat_id}\n")
