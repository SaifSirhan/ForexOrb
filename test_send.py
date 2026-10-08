import os, requests
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("TELEGRAM_BOT_TOKEN")
chat  = os.getenv("TELEGRAM_CHAT_ID")

print("token:", (token[:10] + "..." + token[-4:]) if token else None)
print("chat :", chat)

r = requests.post(
    f"https://api.telegram.org/bot{token}/sendMessage",
    json={"chat_id": chat, "text": "test from forex-orb"},
    timeout=10,
)
print(r.status_code, r.text[:300])