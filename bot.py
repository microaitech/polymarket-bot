import os
import asyncio
import logging
import json
import time
import websockets
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
TRACKED_ADDRESS = os.getenv("TRACKED_ADDRESS", "").lower()
COPY_PERCENTAGE = float(os.getenv("COPY_PERCENTAGE", "50"))
MAX_BET_USDC    = float(os.getenv("MAX_BET_USDC", "10"))

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
POLYMARKET_WS_URL    = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

bot_state = {
    "running": False,
    "tracked": TRACKED_ADDRESS,
    "copy_pct": COPY_PERCENTAGE,
    "max_bet": MAX_BET_USDC,
    "seen_trades": set(),
    "chat_id": None,
    "total_copied": 0,
    "ws_connected": False,
    "ws_reconnects": 0,
}

def get_trader_positions(address):
    try:
        r = requests.get(f"{POLYMARKET_GAMMA_API}/positions?user={address}&limit=20", timeout=10)
        return r.json().get("positions", [])
    except:
        return []

def calculate_copy_amount(original):
    return min(original * (bot_state["copy_pct"] / 100), bot_state["max_bet"])

def execute_copy_trade(trade):
    try:
        trade_id = trade.get("id") or trade.get("hash", "")
        if trade_id in bot_state["seen_trades"]:
            return False, "Zaten kopyalandı"
        bot_state["seen_trades"].add(trade_id)
        outcome = trade.get("side", "BUY").upper()
        original_amount = float(trade.get("size", 0))
        price = float(trade.get("price", 0))
        if original_amount < 1:
            return False, "Miktar çok küçük"
        copy_amount = calculate_copy_amount(original_amount)
        if copy_amount < 1:
            return False, f"Miktar çok küçük: ${copy_amount:.2f}"
        bot_state["total_copied"] += 1
        return True, (f"⚡️ <b>YENİ KOPYA</b>\n"
                     f"{'🟢 AL' if outcome == 'BUY' else '🔴 SAT'} @ ${price:.4f}\n"
                     f"💵 ${copy_amount:.2f} USDC")
    except Exception as e:
        return False, f"Hata: {e}"

async def websocket_listener(app):
    BACKOFF = [2, 5, 10, 30, 60]
    while True:
        if not bot_state["running"]:
            await asyncio.sleep(3)
            continue
        wait = BACKOFF[min(bot_state["ws_reconnects"], len(BACKOFF)-1)]
        try:
            async with websockets.connect(POLYMARKET_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"type": "subscribe", "channel": "user", "auth": {"address": bot_state["tracked"]}}))
                bot_state["ws_connected"] = True
                bot_state["ws_reconnects"] = 0
                if bot_state["chat_id"]:
                    await app.bot.send_message(chat_id=bot_state["chat_id"], text="🔌 Bağlandı! Anlık izleme aktif.")
                async for raw in ws:
                    if not bot_state["running"]:
                        break
                    try:
                        data = json.loads(raw)
                        event_type = data.get("type") or data.get("event_type", "")
                        if event_type in ("trade", "order_filled", "TRADE", "ORDER_FILLED"):
                            success, message = execute_copy_trade(data)
                            if bot_state["chat_id"]:
                                await app.bot.send_message(chat_id=bot_state["chat_id"], text=message, parse_mode="HTML")
                    except:
                        continue
        except Exception as e:
            logger.error(f"WS hatası: {e}")
        finally:
            bot_state["ws_connected"] = False
            bot_state["ws_reconnects"] += 1
        await asyncio.sleep(wait)

async def fallback_polling(app):
    last_seen = set()
    while True:
        await asyncio.sleep(10)
        if not bot_state["running"] or bot_state["ws_connected"]:
            continue
        try:
            r = requests.get(f"{POLYMARKET_GAMMA_API}/trades?maker={bot_state['tracked']}&limit=10&status=MATCHED", timeout=10)
            for trade in r.json().get("trades", []):
                tid = trade.get("id", "")
                if not tid or tid in last_seen:
                    continue
                last_seen.add(tid)
                created = trade.get("createdAt", 0)
                if isinstance(created, (int, float)) and time.time() - created > 120:
                    continue
                success, message = execute_copy_trade(trade)
                if bot_state["chat_id"]:
                    await app.bot.send_message(chat_id=bot_state["chat_id"], text=message, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Polling hatası: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state["chat_id"] = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton("▶️ Başlat", callback_data="start_bot"),
         InlineKeyboardButton("⏹ Durdur", callback_data="stop_bot")],
        [InlineKeyboardButton("📊 Durum", callback_data="status"),
         InlineKeyboardButton("💼 Pozisyon", callback_data="positions")],
    ]
    await update.message.reply_text(
        f"⚡️ *Polymarket Copy Trade Bot*\n\n"
        f"İzlenen: `{bot_state['tracked'][:10]}...`\n"
        f"Kopya: %{bot_state['copy_pct']} | Max: ${bot_state['max_bet']} USDC",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_state["chat_id"] = update.effective_chat.id
    if query.data == "start_bot":
        bot_state["running"] = True
        await query.edit_message_text("▶️ Bot başlatıldı! Trader izleniyor...")
    elif query.data == "stop_bot":
        bot_state["running"] = False
        await query.edit_message_text("⏹ Bot durduruldu.")
    elif query.data == "status":
        ws = "✅ Bağlı" if bot_state["ws_connected"] else "❌ Bağlı değil"
        await query.edit_message_text(
            f"📊 *Durum*\n\nBot: {'🟢 Çalışıyor' if bot_state['running'] else '🔴 Durdu'}\n"
            f"WebSocket: {ws}\nKopyalanan: {bot_state['total_copied']}",
            parse_mode="Markdown"
        )
    elif query.data == "positions":
        positions = get_trader_positions(bot_state["tracked"])
        if not positions:
            await query.edit_message_text("📭 Açık pozisyon yok.")
            return
        text = "💼 *Pozisyonlar*\n\n"
        for p in positions[:5]:
            q = p.get("market", {}).get("question", "?")[:35]
            text += f"• {q}\n"
        await query.edit_message_text(text, parse_mode="Markdown")

async def post_init(app):
    asyncio.create_task(websocket_listener(app))
    asyncio.create_task(fallback_polling(app))

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.post_init = post_init
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

