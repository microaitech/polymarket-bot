import os
import asyncio
import logging
import time
import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

load_dotenv()

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
PRIVATE_KEY     = os.getenv("PRIVATE_KEY")
TRACKED_ADDRESS = os.getenv("TRACKED_ADDRESS", "").lower()
POLY_API_KEY    = os.getenv("POLY_API_KEY")
POLY_SECRET     = os.getenv("POLY_SECRET")
POLY_PASSPHRASE = os.getenv("POLY_PASSPHRASE")

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"

try:
    creds = ApiCreds(api_key=POLY_API_KEY, api_secret=POLY_SECRET, api_passphrase=POLY_PASSPHRASE)
    clob_client = ClobClient(host="https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=137, creds=creds)
    logger.info("CLOB client hazır")
except Exception as e:
    clob_client = None
    logger.error(f"CLOB client hatası: {e}")

bot_state = {
    "running": False,
    "tracked": TRACKED_ADDRESS,
    "seen_trades": set(),
    "chat_id": None,
    "total_copied": 0,
}

def execute_copy_trade(trade):
    try:
        trade_id = trade.get("id", "") or trade.get("transactionHash", "")
        if not trade_id or trade_id in bot_state["seen_trades"]:
            return False, ""
        bot_state["seen_trades"].add(trade_id)
        outcome = trade.get("side", "BUY").upper()
        price = float(trade.get("price", 0))
        token_id = trade.get("asset_id") or trade.get("tokenId", "")
        if not token_id:
            return False, f"⚠️ Token ID yok: {str(trade)[:200]}"
        if clob_client:
            try:
                from py_clob_client.clob_types import CreateOrderOptions, OrderType
                clob_client.create_and_post_order(CreateOrderOptions(
                    token_id=token_id, price=price, size=7.0,
                    side=outcome, order_type=OrderType.GTC
                ))
                bot_state["total_copied"] += 1
                return True, (
                    f"✅ <b>İŞLEM KOPYALANDI</b>\n"
                    f"{'🟢 AL' if outcome == 'BUY' else '🔴 SAT'} @ ${price:.4f}\n"
                    f"💵 $7.00 USDC"
                )
            except Exception as e:
                return False, f"⚠️ İşlem hatası: {e}"
        return False, "❌ API bağlı değil"
    except Exception as e:
        return False, f"Hata: {e}"

async def polling_loop(app):
    last_seen = set()
    start_time = time.time()
    while True:
        await asyncio.sleep(2)
        if not bot_state["running"]:
            continue
        try:
            url = f"{POLYMARKET_GAMMA_API}/trades?maker={bot_state['tracked']}&limit=20&status=MATCHED"
            r = requests.get(url, timeout=8)
            trades = r.json().get("trades", [])
            for trade in trades:
                tid = trade.get("id", "") or trade.get("transactionHash", "")
                if not tid or tid in last_seen:
                    continue
                last_seen.add(tid)
                created = trade.get("createdAt", 0)
                if isinstance(created, (int, float)) and created < start_time:
                    continue
                if bot_state["chat_id"]:
                    await app.bot.send_message(
                        chat_id=bot_state["chat_id"],
                        text=f"🔍 Yeni trade:\n{str(trade)[:400]}"
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
    clob_status = "✅ Bağlı" if clob_client else "❌ Bağlanamadı"
    await update.message.reply_text(
        f"⚡️ *Polymarket Copy Trade Bot*\n\n"
        f"İzlenen: `{bot_state['tracked'][:10]}...`\n"
        f"Sabit işlem: $7.00 USDC\n"
        f"API: {clob_status}",
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
        clob_status = "✅ Aktif" if clob_client else "❌ Bağlanamadı"
        await query.edit_message_text(
            f"📊 *Durum*\n\n"
            f"Bot: {'🟢 Çalışıyor' if bot_state['running'] else '🔴 Durdu'}\n"
            f"API: {clob_status}\n"
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
