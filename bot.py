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
COPY_PERCENTAGE = float(os.getenv("COPY_PERCENTAGE", "50"))
MAX_BET_USDC    = float(os.getenv("MAX_BET_USDC", "10"))
POLY_API_KEY    = os.getenv("POLY_API_KEY")
POLY_SECRET     = os.getenv("POLY_SECRET")
POLY_PASSPHRASE = os.getenv("POLY_PASSPHRASE")
STOP_LOSS_PCT   = 20.0
TAKE_PROFIT_PCT = 70.0

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
    "copy_pct": COPY_PERCENTAGE,
    "max_bet": MAX_BET_USDC,
    "seen_trades": set(),
    "chat_id": None,
    "total_copied": 0,
    "open_positions": {},
}

def calculate_copy_amount(original):
    return min(original * (bot_state["copy_pct"] / 100), bot_state["max_bet"])

def execute_copy_trade(trade):
    try:
        trade_id = trade.get("id", "") or trade.get("transactionHash", "")
        if not trade_id or trade_id in bot_state["seen_trades"]:
            return False, ""
        bot_state["seen_trades"].add(trade_id)
        outcome = trade.get("side", "BUY").upper()
        original_amount = float(trade.get("size", 0))
        price = float(trade.get("price", 0))
        token_id = trade.get("asset_id") or trade.get("tokenId", "")
        if original_amount < 1:
            return False, ""
        copy_amount = calculate_copy_amount(original_amount)
        if copy_amount < 1:
            return False, ""
        if clob_client and token_id:
            try:
                from py_clob_client.clob_types import CreateOrderOptions, OrderType
                clob_client.create_and_post_order(CreateOrderOptions(
                    token_id=token_id, price=price, size=copy_amount,
                    side=outcome, order_type=OrderType.GTC
                ))
                bot_state["total_copied"] += 1
                if outcome == "BUY":
                    bot_state["open_positions"][token_id] = {
                        "size": copy_amount, "entry_price": price
                    }
                return True, (
                    f"✅ <b>İŞLEM KOPYALANDI</b>\n"
                    f"{'🟢 AL' if outcome == 'BUY' else '🔴 SAT'} @ ${price:.4f}\n"
                    f"💵 ${copy_amount:.2f} USDC\n"
                    f"🛡 Stop: %{STOP_LOSS_PCT} | 🎯 Kar: %{TAKE_PROFIT_PCT}"
                )
            except Exception as e:
                return False, f"⚠️ İşlem hatası: {e}"
        return False, ""
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
                success, message = execute_copy_trade(trade)
                if success and message and bot_state["chat_id"]:
                    await app.bot.send_message(
                        chat_id=bot_state["chat_id"],
                        text=message,
                        parse_mode="HTML"
                    )
        except Exception as e:
            logger.error(f"Polling hatası: {e}")

async def stop_loss_loop(app):
    while True:
        await asyncio.sleep(60)
        if not bot_state["running"] or not bot_state["open_positions"]:
            continue
        for token_id, pos in list(bot_state["open_positions"].items()):
            try:
                r = requests.get(f"https://clob.polymarket.com/prices?token_id={token_id}", timeout=8)
                current_price = float(r.json().get("price", 0))
                if current_price <= 0:
                    continue
                entry = pos["entry_price"]
                pnl_pct = ((current_price - entry) / entry) * 100
                reason = None
                if pnl_pct <= -STOP_LOSS_PCT:
                    reason = "STOP_LOSS"
                elif pnl_pct >= TAKE_PROFIT_PCT:
                    reason = "TAKE_PROFIT"
                if reason and clob_client:
                    from py_clob_client.clob_types import CreateOrderOptions, OrderType
                    clob_client.create_and_post_order(CreateOrderOptions(
                        token_id=token_id, price=current_price,
                        size=pos["size"], side="SELL", order_type=OrderType.GTC
                    ))
                    del bot_state["open_positions"][token_id]
                    emoji = "🛑" if reason == "STOP_LOSS" else "🎯"
                    msg = (
                        f"{emoji} <b>{'STOP LOSS' if reason == 'STOP_LOSS' else 'KAR HEDEFİ'}</b>\n"
                        f"Kapatıldı @ ${current_price:.4f}\n"
                        f"PnL: {'🔴' if pnl_pct < 0 else '🟢'} %{pnl_pct:.1f}"
                    )
                    if bot_state["chat_id"]:
                        await app.bot.send_message(chat_id=bot_state["chat_id"], text=msg, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Stop loss hatası: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_state["chat_id"] = update.effective_chat.id
    keyboard = [
        [InlineKeyboardButton("▶️ Başlat", callback_data="start_bot"),
         InlineKeyboardButton("⏹ Durdur", callback_data="stop_bot")],
        [InlineKeyboardButton("📊 Durum", callback_data="status"),
         InlineKeyboardButton("💼 Pozisyon", callback_data="positions")],
    ]
    clob_status = "✅ Bağlı" if clob_client else "❌ Bağlanamadı"
    await update.message.reply_text(
        f"⚡️ *Polymarket Copy Trade Bot*\n\n"
        f"İzlenen: `{bot_state['tracked'][:10]}...`\n"
        f"Kopya: %{bot_state['copy_pct']} | Max: ${bot_state['max_bet']} USDC\n"
        f"🛡 Stop: %{STOP_LOSS_PCT} | 🎯 Kar: %{TAKE_PROFIT_PCT}\n"
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
        await query.edit_message_text("▶️ Bot başlatıldı! Her 5 saniyede kontrol ediliyor...")
    elif query.data == "stop_bot":
        bot_state["running"] = False
        await query.edit_message_text("⏹ Bot durduruldu.")
    elif query.data == "status":
        clob_status = "✅ Aktif" if clob_client else "❌ Bağlanamadı"
        await query.edit_message_text(
            f"📊 *Durum*\n\n"
            f"Bot: {'🟢 Çalışıyor' if bot_state['running'] else '🔴 Durdu'}\n"
            f"API: {clob_status}\n"
            f"Kopyalanan: {bot_state['total_copied']}\n"
            f"Açık pozisyon: {len(bot_state['open_positions'])}\n"
            f"🛡 Stop: %{STOP_LOSS_PCT} | 🎯 Kar: %{TAKE_PROFIT_PCT}",
            parse_mode="Markdown"
        )
    elif query.data == "positions":
        if not bot_state["open_positions"]:
            await query.edit_message_text("📭 Açık pozisyon yok.")
            return
        text = "💼 *Açık Pozisyonlar*\n\n"
        for tid, pos in list(bot_state["open_positions"].items())[:5]:
            text += f"• {tid[:10]}... @ ${pos['entry_price']:.4f} | ${pos['size']:.2f}\n"
        await query.edit_message_text(text, parse_mode="Markdown")

async def post_init(app):
    asyncio.create_task(polling_loop(app))
    asyncio.create_task(stop_loss_loop(app))

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.post_init = post_init
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
