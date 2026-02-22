import os
import asyncio
import logging
import time
import hmac
import hashlib
import json
import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
TRACKED_ADDRESS = os.getenv("TRACKED_ADDRESS", "").lower()
POLY_API_KEY    = os.getenv("POLY_API_KEY")
POLY_SECRET     = os.getenv("POLY_SECRET")
POLY_PASSPHRASE = os.getenv("POLY_PASSPHRASE")
PRIVATE_KEY     = os.getenv("PRIVATE_KEY")

bot_state = {
    "running": False,
    "tracked": TRACKED_ADDRESS,
    "seen_trades": set(),
    "chat_id": None,
    "total_copied": 0,
}

def get_auth_headers(method, path, body=""):
    timestamp = str(int(time.time()))
    message = timestamp + method + path + body
    signature = hmac.new(
        POLY_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return {
        "POLY-API-KEY": POLY_API_KEY,
        "POLY-SIGNATURE": signature,
        "POLY-TIMESTAMP": timestamp,
        "POLY-PASSPHRASE": POLY_PASSPHRASE,
        "Content-Type": "application/json",
    }

def place_market_order(token_id, side, amount):
    try:
        path = "/order"
        body = json.dumps({
            "tokenID": token_id,
            "side": side,
            "type": "MARKET",
            "amount": str(amount),
        })
        headers = get_auth_headers("POST", path, body)
        r = requests.post(
            f"https://clob.polymarket.com{path}",
            headers=headers,
            data=body,
            timeout=10
        )
        return r.status_code == 200, r.json()
    except Exception as e:
        return False, str(e)

def execute_copy_trade(trade):
    try:
        trade_id = trade.get("transactionHash", "")
        if not trade_id or trade_id in bot_state["seen_trades"]:
            return False, ""
        bot_state["seen_trades"].add(trade_id)
        outcome = trade.get("side", "BUY").upper()
        token_id = trade.get("asset", "")
        title = trade.get("title", "")[:40]
        price = trade.get("price", 0)
        if not token_id:
            return False, "⚠️ Token ID yok"
        success, resp = place_market_order(token_id, outcome, 7.0)
        if success:
            bot_state["total_copied"] += 1
            return True, (
                f"✅ <b>İŞLEM KOPYALANDI</b>\n"
                f"{'🟢 AL' if outcome == 'BUY' else '🔴 SAT'} @ {price}\n"
                f"💵 $7.00 USDC\n"
                f"📊 {title}"
            )
        else:
            return False, f"⚠️ Hata: {str(resp)[:100]}"
    except Exception as e:
        return False, f"Hata: {e}"

async def polling_loop(app):
    last_seen = set()
    start_time = time.time()
    while True:
        await asyncio.sleep(3)
        if not bot_state["running"]:
            continue
        try:
            url = f"https://data-api.polymarket.com/trades?user={bot_state['tracked']}&limit=20"
            r = requests.get(url, timeout=8)
            trades = r.json()
            if not isinstance(trades, list):
                continue
            for trade in trades:
                tid = trade.get("transactionHash", "")
                if not tid or tid in last_seen:
                    continue
                last_seen.add(tid)
                created = trade.get("timestamp", 0)
                if isinstance(created, (int, float)) and created < start_time:
                    continue
                if bot_state["chat_id"]:
                    await app.bot.send_message(
                        chat_id=bot_state["chat_id"],
                        text=f"🔍 {trade.get('title','')} | {trade.get('side','')} @ {trade.get('price','')}"
                    )
                success, message = execute_copy_trade(trade)
                if message and bot_state["chat_id"]:
                    await app.bot.send_message(
                        chat_id=bot_state["chat_id"],
                        text=message,
                        parse_mode="HTML"
                    )
        except Exception as e:
            logger.error(f"Polling hatası: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state["chat_id"] = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton("▶️ Başlat", callback_data="start_bot"),
         InlineKeyboardButton("⏹ Durdur", callback_data="stop_bot")],
        [InlineKeyboardButton("📊 Durum", callback_data="status")],
    ]
    await update.message.reply_text(
        f"⚡️ *Polymarket Copy Trade Bot*\n\n"
        f"İzlenen: `{bot_state['tracked'][:10]}...`\n"
        f"Sabit işlem: $7.00 USDC",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_state["chat_id"] = update.effective_chat.id
    if query.data == "start_bot":
        bot_state["running"] = True
        await query.edit_message_text("▶️ Bot başlatıldı! Her 3 saniyede kontrol ediliyor...")
    elif query.data == "stop_bot":
        bot_state["running"] = False
        await query.edit_message_text("⏹ Bot durduruldu.")
    elif query.data == "status":
        await query.edit_message_text(
            f"📊 *Durum*\n\n"
            f"Bot: {'🟢 Çalışıyor' if bot_state['running'] else '🔴 Durdu'}\n"
            f"Kopyalanan: {bot_state['total_copied']}",
            parse_mode="Markdown"
        )

async def post_init(app):
    asyncio.create_task(polling_loop(app))

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.post_init = post_init
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
