import os
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters, CallbackQueryHandler
from dotenv import load_dotenv
import wallet_utils
import swap_handler
from config import BOT_TOKEN, BRIDGE_URL, CHAIN_ID # Make sure CHAIN_ID is imported or defined
from web3 import Web3
import requests
import json
import logging
import telegram # Import telegram for specific error handling
import threading
from config import ROUTERS, EXPLORER_URL
import asyncio

# Ensure RPC_URL is properly configured and accessible
from config import RPC_URL
w3 = Web3(Web3.HTTPProvider(RPC_URL))

load_dotenv()
# wallet_utils.init_db()  # Removed: not needed with DynamoDB

# States for buy/sell flows
BUY_TOKEN, BUY_AMOUNT, BUY_CONFIRM = range(3)
SELL_TOKEN, SELL_AMOUNT, SELL_CONFIRM = range(3, 6)
# Withdraw flow states (re-indexed for clarity)
WITHDRAW_TYPE, WITHDRAW_RECIPIENT_ADDRESS, WITHDRAW_TOKEN_SELECT, WITHDRAW_AMOUNT, WITHDRAW_CONFIRM = range(6, 11)


# --- Menu Keyboards ---
main_menu_inline_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("🏠 Menu", callback_data="menu_home"), InlineKeyboardButton("💰 Wallet", callback_data="menu_wallet")],
    [InlineKeyboardButton("🛒 Buy", callback_data="menu_buy"), InlineKeyboardButton("💸 Sell", callback_data="menu_sell")],
    [InlineKeyboardButton("⬆️ Withdraw", callback_data="menu_withdraw"), InlineKeyboardButton("🛠️ Manage Wallet", callback_data="menu_manage_wallet")]
])

# Removed persistent_menu_keyboard and associated logic

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)

# Global application instance for Lambda
app = None

def get_application():
    """Get or create the Telegram application instance"""
    global app
    if app is None:
        if not BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable is required")
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        
        # Add all handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("wallet", wallet))
        app.add_handler(CommandHandler("export_keys", export_keys))
        app.add_handler(CommandHandler("reset_wallet", reset_wallet))

        # General menu callback handlers that should not interfere with conversations
        app.add_handler(CallbackQueryHandler(wallet, pattern="^menu_wallet$"))
        app.add_handler(CallbackQueryHandler(manage_wallet, pattern="^menu_manage_wallet$"))
        app.add_handler(CallbackQueryHandler(export_keys, pattern="^manage_export_keys$"))
        app.add_handler(CallbackQueryHandler(reset_wallet, pattern="^manage_reset_wallet$"))
        app.add_handler(CallbackQueryHandler(reset_to_menu_handler, pattern="^menu_home$"))

        # --- Buy Conversation Handler ---
        buy_conv = ConversationHandler(
            entry_points=[
                CommandHandler("buy", buy),
                CallbackQueryHandler(buy, pattern="^menu_buy$")
            ],
            states={
                BUY_TOKEN: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, buy_token),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^buy_cancel$|^menu_home$"),
                ],
                BUY_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amount),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^buy_back$|^buy_cancel$|^menu_home$"),
                ],
                BUY_CONFIRM: [
                    CallbackQueryHandler(buy_confirm),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^buy_cancel$|^menu_home$"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", reset_to_menu_handler),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^cancel_flow$"),
            ],
            allow_reentry=True
        )
        app.add_handler(buy_conv)

        # --- Sell Conversation Handler ---
        sell_conv = ConversationHandler(
            entry_points=[
                CommandHandler("sell", sell),
                CallbackQueryHandler(sell, pattern="^menu_sell$")
            ],
            states={
                SELL_TOKEN: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, sell_token),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^sell_cancel$|^menu_home$"),
                ],
                SELL_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, sell_amount),
                    CallbackQueryHandler(sell_amount_percent, pattern="^sell_pct_.*$"),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^sell_cancel$|^menu_home$"),
                ],
                SELL_CONFIRM: [
                    CallbackQueryHandler(sell_confirm),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^sell_cancel$|^menu_home$"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", reset_to_menu_handler),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^cancel_flow$"),
            ],
            allow_reentry=True
        )
        app.add_handler(sell_conv)

        # --- Withdraw Conversation Handler (Revised) ---
        withdraw_conv = ConversationHandler(
            entry_points=[
                CommandHandler("withdraw", withdraw),
                CallbackQueryHandler(withdraw, pattern="^menu_withdraw$")
            ],
            states={
                WITHDRAW_TYPE: [
                    CallbackQueryHandler(withdraw_type, pattern="^withdraw_eth$|^withdraw_token$"),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^menu_home$|^withdraw_cancel$"), # Added cancel
                ],
                WITHDRAW_RECIPIENT_ADDRESS: [ # New state name
                    MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_recipient_address), # New function name
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^withdraw_cancel$|^menu_home$"),
                ],
                WITHDRAW_TOKEN_SELECT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_token_select),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^withdraw_cancel$|^menu_home$"),
                ],
                WITHDRAW_AMOUNT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^withdraw_cancel$|^menu_home$"),
                ],
                WITHDRAW_CONFIRM: [
                    CallbackQueryHandler(withdraw_confirm, pattern="^withdraw_confirm$|^withdraw_cancel$"),
                    CallbackQueryHandler(reset_to_menu_handler, pattern="^menu_home$"),
                ],
            },
            fallbacks=[
                CommandHandler("cancel", reset_to_menu_handler),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^cancel_flow$"),
            ],
            allow_reentry=True
        )
        app.add_handler(withdraw_conv)
        
        # Add global debug text handler LAST, so it only catches unhandled text messages
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, debug_text_handler))
    
    return app

def lambda_handler(event, context):
    """AWS Lambda handler function"""
    try:
        # Parse the incoming webhook
        body = json.loads(event['body'])
        
        # Create the application
        application = get_application()
        
        # Process the update
        application.process_update(Update.de_json(body, application.bot))
        
        return {
            'statusCode': 200,
            'body': json.dumps('OK')
        }
    except Exception as e:
        logging.error(f"Error in lambda_handler: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps(f'Error: {str(e)}')
        }


# Utility to log user actions
def log_action(update, context, handler_name, extra_info=None):
    user_id = getattr(update.effective_user, 'id', None)
    chat_id = getattr(update.effective_chat, 'id', None)
    msg = f"[Handler: {handler_name}] [User: {user_id}] [Chat: {chat_id}] "
    if update.message:
        msg += f"[Text: {update.message.text}] "
    if update.callback_query:
        msg += f"[Callback: {update.callback_query.data}] "
    if extra_info:
        msg += f"[Info: {extra_info}] "
    logging.info(msg)

def get_token_balances_from_explorer(address):
    url = f"https://explorer.inkonchain.com/api/v2/addresses/{address}/token-balances"
    try:
        resp = requests.get(url, headers={"accept": "application/json"}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        tokens = []
        for entry in data:
            try:
                token_info = entry.get("token", {})
                balance = int(entry.get("value", "0"))
                decimals = int(token_info.get("decimals", "18")) # Default to 18 if not found
                symbol = token_info.get("symbol", "?")
                token_address = token_info.get("address", "")
                if balance > 0:
                    tokens.append({
                        "address": token_address,
                        "symbol": symbol,
                        "balance": balance / (10 ** decimals),
                        "decimals": decimals # Store decimals for accurate conversion later
                    })
            except Exception as e:
                logging.warning(f"Error parsing token entry for {address}: {e} - Entry: {entry}")
                pass
        return tokens
    except Exception as e:
        logging.error(f"Error fetching token balances from explorer.inkonchain.com for {address}: {e}")
        return []

def is_valid_eth_address(address):
    return isinstance(address, str) and address.startswith('0x') and len(address) == 42 and Web3.is_checksum_address(address)


# --- Main Menu Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'start')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()  # Clear any stored data
    if not update.effective_user:
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    telegram_id = str(update.effective_user.id)
    address, _ = wallet_utils.get_wallet(telegram_id)
    if not address:
        address, encrypted_pk = wallet_utils.create_wallet()
        wallet_utils.store_wallet(telegram_id, address, encrypted_pk)
    msg = (
        "🦑 <b>Welcome to <i>Inky Buy Bot</i>!</b>\n\n"
        f"👛 <b>Your wallet:</b> <code>{address}</code>\n"
        f"🌉 <b>Bridge ETH to Ink:</b> <a href='{BRIDGE_URL}'>{BRIDGE_URL}</a>\n\n"
        "💡 <i>Use the menu below or type a command.</i>"
    )
    if update.message:
        await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True,
                                         reply_markup=main_menu_inline_keyboard)
    elif update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode='HTML', disable_web_page_preview=True,
                                                       reply_markup=main_menu_inline_keyboard)
    elif update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=msg,
            parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    return ConversationHandler.END

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'menu')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()
    if not update.effective_user:
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    wallet = wallet_utils.get_wallet(str(update.effective_user.id))
    address = wallet[0] if wallet and wallet[0] else None
    msg = (
        "🦑 <b>Welcome to <i>Inky Buy Bot</i>!</b>\n\n"
        f"👛 <b>Your wallet:</b> <code>{address if address else 'N/A'}</code>\n"
        f"🌉 <b>Bridge ETH to Ink:</b> <a href='{BRIDGE_URL}'>{BRIDGE_URL}</a>\n\n"
        "💡 <i>Use the menu below or type a command.</i>"
    )
    if update.message:
        await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True, reply_markup=main_menu_inline_keyboard)
    elif update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode='HTML', disable_web_page_preview=True, reply_markup=main_menu_inline_keyboard)
    elif update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=msg,
            parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    return ConversationHandler.END

async def manage_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'manage_wallet')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 Export Keys", callback_data="manage_export_keys")],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_home")]
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(
            "🛠️ <b>Manage Wallet</b>\nChoose an option:",
            parse_mode='HTML', reply_markup=keyboard)
    elif update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="🛠️ <b>Manage Wallet</b>\nChoose an option:",
            parse_mode='HTML', reply_markup=keyboard)
    return ConversationHandler.END


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'back_to_menu')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()
    await start(update, context)
    return ConversationHandler.END

async def wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'wallet')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()
    if not update.effective_user:
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    telegram_id = str(update.effective_user.id)
    address, _ = wallet_utils.get_wallet(telegram_id)
    if not address:
        response_text = "❗️ <b>No wallet found.</b> Use /start to create one."
        if update.message:
            await update.message.reply_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=response_text,
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END

    try:
        balance_wei = w3.eth.get_balance(address)
        balance_eth = balance_wei / 1e18
        balance_str = f"{balance_eth:.6f} ETH"
    except Exception as e:
        balance_str = f"Error fetching balance: {e}"

    msg = (
        f"👛 <b>Your wallet:</b> <code>{address}</code>\n"
        f"💰 <b>Balance:</b> <code>{balance_str}</code>\n"
        f"🌉 <b>Bridge ETH:</b> <a href='{BRIDGE_URL}'>{BRIDGE_URL}</a>"
    )
    if update.message:
        await update.message.reply_text(msg, parse_mode='HTML', disable_web_page_preview=True, reply_markup=main_menu_inline_keyboard)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, parse_mode='HTML', disable_web_page_preview=True, reply_markup=main_menu_inline_keyboard)
    elif update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=msg,
            parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    return ConversationHandler.END

async def export_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'export_keys')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()
    if not update.effective_user:
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    telegram_id = str(update.effective_user.id)
    address, encrypted_pk = wallet_utils.get_wallet(telegram_id)
    if not address:
        response_text = "❗️ <b>No wallet found.</b> Use /start to create one."
        if update.message:
            await update.message.reply_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=response_text,
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    private_key = wallet_utils.decrypt_private_key(encrypted_pk)
    response_text = f"🔐 <b>Your private key:</b>\n<code>{private_key}</code>"
    if update.message:
        await update.message.reply_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    elif update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=response_text,
            parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    return ConversationHandler.END


async def reset_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'reset_wallet')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()
    if not update.effective_user:
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    telegram_id = str(update.effective_user.id)
    wallet_utils.delete_wallet(telegram_id)
    address, encrypted_pk = wallet_utils.create_wallet()
    wallet_utils.store_wallet(telegram_id, address, encrypted_pk)
    response_text = f"♻️ <b>Wallet reset!</b>\nNew address: <code>{address}</code>"
    if update.message:
        await update.message.reply_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    elif update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=response_text,
            parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    return ConversationHandler.END

# --- Buy Flow (Token First, Inline Only) ---
async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'buy')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()
    prompt = "🛒 <b>Buy Tokens</b>\n\n⚠️ <b>Only Inky Factory contracts can be traded.</b>\n\n🔗 <b>Enter the token address you want to buy:</b>"
    if update.callback_query:
        await update.callback_query.answer()
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=prompt,
                parse_mode='HTML',
                reply_markup=ForceReply(selective=True)
            )
        return BUY_TOKEN
    elif update.message:
        await update.message.reply_text(
            prompt,
            parse_mode='HTML',
            reply_markup=ForceReply(selective=True)
        )
        return BUY_TOKEN
    elif update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=prompt,
            parse_mode='HTML',
            reply_markup=ForceReply(selective=True)
        )
        return BUY_TOKEN
    return ConversationHandler.END

# --- V3 Pool Existence Check ---
def is_token_in_v3_pool(token_address):
    v3_router = next(r for r in ROUTERS if r['type'] == 'v3')
    factory_address = v3_router['factory']
    fee = v3_router['fee']
    weth = v3_router['weth']
    V3_FACTORY_ABI = [{
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"},
            {"internalType": "uint24", "name": "fee", "type": "uint24"}
        ],
        "name": "getPool",
        "outputs": [{"internalType": "address", "name": "pool", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }]
    factory = w3.eth.contract(address=Web3.to_checksum_address(factory_address), abi=V3_FACTORY_ABI)
    try:
        pool = factory.functions.getPool(weth, Web3.to_checksum_address(token_address), fee).call()
        return pool != "0x0000000000000000000000000000000000000000"
    except Exception as e:
        print(f"[V3 Pool Check] Error: {e}")
        return False

async def buy_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'buy_token')
    if update.callback_query:
        await update.callback_query.answer()
        if update.effective_chat and hasattr(context, 'bot'):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Unexpected input. Please enter a token address or cancel the operation.",
                parse_mode='HTML',
                reply_markup=main_menu_inline_keyboard
            )
        return ConversationHandler.END
    if not update.message or not hasattr(update.message, 'text') or update.message.text is None:
        if update.effective_chat and hasattr(context, 'bot'):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Please enter a token address.",
                parse_mode='HTML',
                reply_markup=main_menu_inline_keyboard
            )
        return BUY_TOKEN
    text = update.message.text.strip() if update.message and update.message.text else ""
    if not is_valid_eth_address(text):
        if update.message:
            await update.message.reply_text(
                "❗️ <b>Invalid token address. Enter a valid token address (0x...):</b>",
                parse_mode='HTML', reply_markup=ForceReply(selective=True))
        return BUY_TOKEN
    # --- V3 POOL CHECK ---
    if not is_token_in_v3_pool(text):
        if update.message:
            await update.message.reply_text(
                "❗️ <b>This token cannot be traded. No Inky Factory pool exists for this token.</b>",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    if not update.effective_user:
        if update.message:
            await update.message.reply_text(
                "❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    context.user_data['buy_token_address'] = text
    telegram_id = str(update.effective_user.id)
    wallet = wallet_utils.get_wallet(telegram_id)
    if not wallet or not wallet[0]:
        if update.message:
            await update.message.reply_text(
                "❗️ No wallet found. Use /start to create one.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    address = wallet[0]
    try:
        balance_wei = w3.eth.get_balance(address)
        balance_eth = balance_wei / 1e18
        balance_str = f"{balance_eth:.6f} ETH"
    except Exception:
        balance_str = "(unavailable)"
    if update.message:
        await update.message.reply_text(
            text=f"💰 <b>Your ETH balance:</b> <code>{balance_str}</code>\nHow much ETH do you want to swap?",
            parse_mode='HTML',
            reply_markup=ForceReply(selective=True)
        )
    return BUY_AMOUNT

async def buy_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'buy_amount')
    
    if update.callback_query:
        await update.callback_query.answer()
        await context.bot.send_message(
            chat_id=update.effective_chat.id if update.effective_chat else None,
            text="❗️ Unexpected input. Please enter the ETH amount or cancel the operation.",
            parse_mode='HTML',
            reply_markup=main_menu_inline_keyboard
        )
        return ConversationHandler.END

    text = update.message.text.strip() if update.message and update.message.text else ""
    try:
        eth_amount = float(text)
        if eth_amount <= 0:
            raise ValueError
        context.user_data['buy_eth_amount'] = int(eth_amount * 1e18)
        token_address = context.user_data['buy_token_address']
        eth_amount_display = eth_amount
        
        msg = (
            f"🛒 <b>Swap Summary</b>\n"
            f"• <b>Amount:</b> <code>{eth_amount_display:.4f} ETH</code>\n"
            f"• <b>Token:</b> <code>{token_address}</code>\n\n"
            "Do you want to proceed?"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="buy_confirm"),
             InlineKeyboardButton("❌ Cancel", callback_data="buy_cancel")]
        ])
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)
        return BUY_CONFIRM
    except Exception:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel Buy", callback_data="buy_cancel")]
        ])
        await update.message.reply_text("❗️ <b>Please enter a valid positive ETH amount (e.g., 0.05).</b>",
                                         parse_mode='HTML',
                                         reply_markup=keyboard)
        return BUY_AMOUNT

async def buy_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'buy_confirm')
    query = update.callback_query
    await query.answer()
    if query.data == "buy_confirm":
        telegram_id = str(query.from_user.id) if query.from_user else None
        address, encrypted_pk = wallet_utils.get_wallet(telegram_id)
        if not address:
            await query.edit_message_text("❗️ <b>No wallet found.</b> Use /start to create one.", parse_mode='HTML', reply_markup=None)
            await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            return ConversationHandler.END
        private_key = wallet_utils.decrypt_private_key(encrypted_pk)
        eth_amount = context.user_data['buy_eth_amount']
        token_address = context.user_data['buy_token_address']
        await query.edit_message_text("⏳ <b>Sending swap...</b>", parse_mode='HTML', reply_markup=None)
        try:
            result = swap_handler.execute_buy(address, private_key, eth_amount, token_address)
            if 'error' in result:
                await query.edit_message_text(f"❌ <b>Error:</b> {result['error']}", parse_mode='HTML', reply_markup=None)
                await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            else:
                tx_hash = result['tx_hash']
                if not tx_hash.startswith('0x'):
                    tx_hash = '0x' + tx_hash
                await query.edit_message_text(
                    f"✅ <b>Success!</b>\n<a href='https://explorer.inkonchain.com/tx/{tx_hash}'>View on Explorer</a>",
                    parse_mode='HTML', disable_web_page_preview=True, reply_markup=None)
                await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        except Exception as e:
            print(f"Error executing buy swap: {e}")
            await query.edit_message_text(f"❌ <b>Error:</b> {e}", parse_mode='HTML', reply_markup=None)
            await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    else: # buy_cancel
        await query.edit_message_text("❌ <b>Cancelled.</b>", parse_mode='HTML', reply_markup=None)
        await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    return ConversationHandler.END

# --- Sell Flow (Robust) ---
async def sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'sell')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()
    if not update.effective_user:
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    telegram_id = str(update.effective_user.id)
    address, _ = wallet_utils.get_wallet(telegram_id)
    if not address:
        response_text = "❗️ <b>No wallet found.</b> Use /start to create one."
        if update.message:
            await update.message.reply_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=response_text,
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    try:
        tokens = get_token_balances_from_explorer(address)
        if not tokens:
            if update.callback_query:
                await update.callback_query.edit_message_text("❗️ <b>No tokens found in your wallet to sell.</b>", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            elif update.message:
                await update.message.reply_text("❗️ <b>No tokens found in your wallet to sell.</b>", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            elif update.effective_chat:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❗️ <b>No tokens found in your wallet to sell.</b>",
                    parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            return ConversationHandler.END
        msg = "<b>Your tokens:</b>\n"
        for t in tokens:
            # Format balance with commas and two decimals
            formatted_balance = f"{t['balance']:,.2f}"
            msg += f"• <code>{t['symbol']}</code>: <b>{formatted_balance}</b> (<code>{t['address']}</code>)\n"
        msg += "\n🔗 <b>Enter the token address you want to sell:</b>"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_home")]
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode='HTML', reply_markup=keyboard)
        elif update.message:
            await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)
        elif update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=msg,
                parse_mode='HTML', reply_markup=keyboard)
        # Apply ForceReply for text input after showing token list
        if update.effective_chat:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="Please enter the token address...", reply_markup=ForceReply(selective=True))
        return SELL_TOKEN
    except Exception as e:
        print(f"Error in sell: {e}")
        if update.callback_query:
            await update.callback_query.edit_message_text(f"❌ <b>Error:</b> {e}", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.message:
            await update.message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ <b>Error:</b> {e}",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END

async def sell_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'sell_token')
    
    if update.callback_query:
        await update.callback_query.answer()
        await reset_to_menu_handler(update, context)
        return ConversationHandler.END

    token_address = update.message.text.strip() if update.message and update.message.text else ""
    if not is_valid_eth_address(token_address):
        if update.message:
            await update.message.reply_text(
                "❗️ <b>Invalid token address. Enter a valid token address (0x...):</b>",
                parse_mode='HTML', reply_markup=ForceReply(selective=True))
        return SELL_TOKEN
    # --- V3 POOL CHECK ---
    if not is_token_in_v3_pool(token_address):
        if update.message:
            await update.message.reply_text(
                "❗️ <b>This token cannot be sold. No Inky Factory pool exists for this token.</b>",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return SELL_TOKEN

    context.user_data['sell_token_address'] = token_address
    if not update.effective_user:
        if update.message:
            await update.message.reply_text(
                "❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    telegram_id = str(update.effective_user.id)
    address, _ = wallet_utils.get_wallet(telegram_id)
    
    try:
        tokens = get_token_balances_from_explorer(address)
        token = next((t for t in tokens if t['address'].lower() == token_address.lower()), None)
        if not token:
            if update.message:
                await update.message.reply_text("❗️ <b>Token not found in your wallet or invalid address.</b> Please enter a valid token address:", parse_mode='HTML', reply_markup=ForceReply(selective=True))
            return SELL_TOKEN
        
        context.user_data['sell_token_balance'] = token['balance']
        context.user_data['sell_token_decimals'] = token['decimals'] # Store decimals
        
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("10%", callback_data="sell_pct_10"),
                InlineKeyboardButton("25%", callback_data="sell_pct_25"),
                InlineKeyboardButton("50%", callback_data="sell_pct_50"),
            ],
            [
                InlineKeyboardButton("75%", callback_data="sell_pct_75"),
                InlineKeyboardButton("100%", callback_data="sell_pct_100"),
            ],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_home")]
        ])
        
        await update.message.reply_text(
            f"<b>{token['symbol']}</b> balance: <b>{token['balance']:.6f}</b>\nSelect the percentage to sell, or enter a specific amount:",
            parse_mode='HTML', reply_markup=keyboard)
        
        return SELL_AMOUNT
    except Exception as e:
        print(f"Error in sell_token: {e}")
        await update.message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END

async def sell_amount_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'sell_amount_percent')
    query = update.callback_query
    await query.answer()
    data = query.data
    
    pct_map = {
        'sell_pct_10': 0.10,
        'sell_pct_25': 0.25,
        'sell_pct_50': 0.50,
        'sell_pct_75': 0.75,
        'sell_pct_100': 0.9999,  # 99.99% for 100% to avoid dust issues
    }
    
    pct = pct_map.get(data, None)
    if pct is None:
        await query.edit_message_text("❗️ <b>Invalid selection.</b>", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END # End conversation if invalid selection
    
    balance = context.user_data.get('sell_token_balance', 0)
    amount = balance * pct
    
    # Ensure the amount is not tiny due to float precision if balance is very small
    if amount < 10**(-context.user_data.get('sell_token_decimals', 18) - 2): # e.g., less than 0.000000000000000001
        amount = 0 # Consider it zero for practical purposes

    context.user_data['sell_token_amount'] = amount # Store as float for display, convert to int(wei) later for transaction
    token_address = context.user_data['sell_token_address']
    token_symbol = context.user_data.get('sell_token_symbol', 'Token')
    
    msg = (
        f"💸 <b>Sell Summary</b>\n"
        f"• <b>Token:</b> <code>{token_address}</code>\n"
        f"• <b>Amount:</b> <code>{amount:.6f} {token_symbol}</code>\n\n"
        "Do you want to proceed?"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="sell_confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data="sell_cancel")]
    ])
    await query.edit_message_text(msg, parse_mode='HTML', reply_markup=keyboard)
    return SELL_CONFIRM

async def sell_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'sell_amount')
    # This handler processes text input for the amount.
    if update.callback_query:
        await update.callback_query.answer()
        await reset_to_menu_handler(update, context)
        return ConversationHandler.END

    text = update.message.text.strip() if update.message and update.message.text else ""
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
        
        token_address = context.user_data['sell_token_address']
        if not update.effective_user:
            if update.message:
                await update.message.reply_text(
                    "❗️ Unable to determine your user ID.",
                    parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            return ConversationHandler.END
        telegram_id = str(update.effective_user.id)
        address, _ = wallet_utils.get_wallet(telegram_id)
        
        tokens = get_token_balances_from_explorer(address)
        token = next((t for t in tokens if t['address'].lower() == token_address.lower()), None)

        if not token or amount > token['balance']:
            if update.message:
                await update.message.reply_text("❗️ <b>Insufficient token balance or invalid amount.</b> Please enter a valid amount:", parse_mode='HTML', reply_markup=ForceReply(selective=True))
            return SELL_AMOUNT

        context.user_data['sell_token_amount'] = amount # Store as float for display, convert to int(wei) later for transaction
        context.user_data['sell_token_decimals'] = token['decimals'] # Ensure decimals are stored

        msg = (
            f"💸 <b>Sell Summary</b>\n"
            f"• <b>Token:</b> <code>{token_address}</code>\n"
            f"• <b>Amount:</b> <code>{amount}</code>\n\n"
            "Do you want to proceed?"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="sell_confirm"),
             InlineKeyboardButton("❌ Cancel", callback_data="sell_cancel")]
        ])
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)
        return SELL_CONFIRM
    except ValueError:
        await update.message.reply_text("❗️ <b>Please enter a valid numeric amount.</b>", parse_mode='HTML', reply_markup=ForceReply(selective=True))
        return SELL_AMOUNT
    except Exception as e:
        print(f"Error in sell_amount: {e}")
        await update.message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END

async def sell_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'sell_confirm')
    query = update.callback_query
    await query.answer()
    if query.data == "sell_confirm":
        if not update.effective_user:
            await query.edit_message_text("❗️ <b>No wallet found.</b> Use /start to create one.", parse_mode='HTML', reply_markup=None)
            await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            return ConversationHandler.END
        telegram_id = str(update.effective_user.id)
        address, encrypted_pk = wallet_utils.get_wallet(telegram_id)
        if not address:
            await query.edit_message_text("❗️ <b>No wallet found.</b> Use /start to create one.", parse_mode='HTML', reply_markup=None)
            await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            return ConversationHandler.END
        private_key = wallet_utils.decrypt_private_key(encrypted_pk)
        token_address = context.user_data['sell_token_address']
        amount_float = context.user_data['sell_token_amount']
        token_decimals = context.user_data.get('sell_token_decimals', 18) # Default to 18

        amount_wei = int(amount_float * (10**token_decimals))
        
        await query.edit_message_text("⏳ <b>Sending swap...</b>", parse_mode='HTML', reply_markup=None)
        try:
            result = swap_handler.execute_sell(address, private_key, token_address, amount_wei)
            if 'error' in result:
                await query.edit_message_text(f"❌ <b>Error:</b> {result['error']}", parse_mode='HTML', reply_markup=None)
                await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            else:
                tx_hash = result['tx_hash']
                if not tx_hash.startswith('0x'):
                    tx_hash = '0x' + tx_hash
                await query.edit_message_text(
                    f"✅ <b>Sell sent!</b>\n<a href='https://explorer.inkonchain.com/tx/{tx_hash}'>View on Explorer</a>",
                    parse_mode='HTML', disable_web_page_preview=True, reply_markup=None)
                await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        except Exception as e:
            print(f"Error executing sell swap: {e}")
            await query.edit_message_text(f"❌ <b>Error:</b> {e}", parse_mode='HTML', reply_markup=None)
            await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    else: # sell_cancel
        await query.edit_message_text("❌ <b>Cancelled.</b>", parse_mode='HTML', reply_markup=None)
        await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    return ConversationHandler.END

# --- Withdraw Flow (Revised) ---
async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'withdraw')
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear() # Clear any stored data from previous conversations

    effective_chat_id = update.effective_chat.id if update.effective_chat else None

    try:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("ETH", callback_data="withdraw_eth"),
             InlineKeyboardButton("Token", callback_data="withdraw_token")],
            [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_home")]
        ])
        
        # Always send a new message for choice selection, don't edit the menu
        if effective_chat_id:
            await context.bot.send_message(
                chat_id=effective_chat_id,
                text="⬆️ <b>Withdraw</b>\nChoose what to withdraw:",
                parse_mode='HTML',
                reply_markup=keyboard
            )
        return WITHDRAW_TYPE
    except Exception as e:
        logging.error(f"Error in withdraw: {e}")
        if effective_chat_id:
            await context.bot.send_message(
                chat_id=effective_chat_id,
                text=f"❌ <b>Error during withdrawal initiation:</b> {e}",
                parse_mode='HTML',
                reply_markup=main_menu_inline_keyboard
            )
        return ConversationHandler.END

async def withdraw_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'withdraw_type')
    query = update.callback_query
    await query.answer()

    context.user_data['withdraw_type'] = query.data.replace("withdraw_", "")
    
    # Edit the inline keyboard message to just show "Loading..." or similar if desired, then send ForceReply
    # Or, just remove the inline keyboard and immediately send the next prompt with ForceReply.
    if query.message:
        await query.edit_message_reply_markup(reply_markup=None) # Remove the inline keyboard from the 'Choose what to withdraw' message
    
    prompt = f"⬆️ <b>Withdraw {context.user_data['withdraw_type'].upper()}</b>\nPlease enter the **recipient address**:"
    if update.message:
        await update.message.reply_text(
            prompt,
            parse_mode='HTML',
            reply_markup=ForceReply(selective=True)
        )
    return WITHDRAW_RECIPIENT_ADDRESS # Changed from WITHDRAW_ADDRESS to reflect new state name

async def withdraw_recipient_address(update: Update, context: ContextTypes.DEFAULT_TYPE): # Renamed function
    log_action(update, context, 'withdraw_recipient_address')
    
    if update.callback_query: # This should not be hit if flow is correct
        await update.callback_query.answer()
        await reset_to_menu_handler(update, context)
        return ConversationHandler.END

    if not update.message or not hasattr(update.message, 'text') or update.message.text is None:
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Please enter the recipient address.",
                parse_mode='HTML',
                reply_markup=main_menu_inline_keyboard
            )
        return WITHDRAW_RECIPIENT_ADDRESS
    
    recipient_address = update.message.text.strip()
    if not is_valid_eth_address(recipient_address):
        await update.message.reply_text(
            "❗️ <b>Invalid recipient address. Please enter a valid Ethereum address (0x...):</b>",
            parse_mode='HTML', reply_markup=ForceReply(selective=True))
        return WITHDRAW_RECIPIENT_ADDRESS

    context.user_data['withdraw_recipient'] = recipient_address
    if not update.effective_user:
        if update.message:
            await update.message.reply_text(
                "❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    
    telegram_id = str(update.effective_user.id)
    user_address, _ = wallet_utils.get_wallet(telegram_id)
    if not user_address:
        await update.message.reply_text(
            "❗️ No wallet found. Use /start to create one.",
            parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END

    try:
        if context.user_data.get('withdraw_type') == 'eth':
            balance_wei = w3.eth.get_balance(user_address)
            balance_eth = balance_wei / 1e18
            context.user_data['withdraw_eth_balance'] = balance_eth
            await update.message.reply_text(
                f"💰 <b>Your ETH balance:</b> <code>{balance_eth:.6f} ETH</code>\nHow much ETH do you want to send?",
                parse_mode='HTML',
                reply_markup=ForceReply(selective=True)
            )
            return WITHDRAW_AMOUNT
        else: # withdraw_type is 'token'
            tokens = get_token_balances_from_explorer(user_address)
            context.user_data['available_tokens'] = tokens # Store for later lookup in withdraw_token_select
            
            if not tokens:
                await update.message.reply_text("❗️ <b>No tokens found in your wallet to withdraw.</b>",
                                                parse_mode='HTML',
                                                reply_markup=main_menu_inline_keyboard)
                return ConversationHandler.END
            
            msg = "<b>Your tokens available for withdrawal:</b>\n"
            for t in tokens:
                formatted_balance = f"{t['balance']:,.2f}"
                msg += f"• <code>{t['symbol']}</code>: <b>{formatted_balance}</b> (<code>{t['address']}</code>)\n"
            msg += "\n🔗 <b>Enter the token address you want to withdraw:</b>"
            
            await update.message.reply_text(
                msg,
                parse_mode='HTML',
                reply_markup=ForceReply(selective=True) # Prompt for token address
            )
            return WITHDRAW_TOKEN_SELECT
    except Exception as e:
        logging.error(f"Error in withdraw_recipient_address: {e}")
        await update.message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END

async def withdraw_token_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'withdraw_token_select')
    
    # Handle callback queries (back button)
    if update.callback_query:
        await update.callback_query.answer()
        await reset_to_menu_handler(update, context)
        return ConversationHandler.END
    
    # Handle text input (token address)
    if update.message and update.message.text:
        token_address_input = update.message.text.strip()
        
        # Validate the token address
        if not is_valid_eth_address(token_address_input):
            await update.message.reply_text("❗️ <b>Invalid token address format.</b> Please enter a valid Ethereum address.", parse_mode='HTML', reply_markup=ForceReply(selective=True))
            return WITHDRAW_TOKEN_SELECT
        
        # Retrieve available tokens from user_data (populated in withdraw_recipient_address)
        available_tokens = context.user_data.get('available_tokens', [])
        selected_token = None
        for token in available_tokens:
            if token['address'].lower() == token_address_input.lower():
                selected_token = token
                break
        
        if not selected_token:
            await update.message.reply_text("❗️ <b>Token not found in your wallet.</b> Please enter a valid token address from the list above.", parse_mode='HTML', reply_markup=ForceReply(selective=True))
            return WITHDRAW_TOKEN_SELECT
        
        # Store token info in context
        context.user_data['withdraw_token_address'] = selected_token['address']
        context.user_data['withdraw_token_symbol'] = selected_token['symbol']
        context.user_data['withdraw_token_balance'] = selected_token['balance']
        context.user_data['withdraw_token_decimals'] = selected_token.get('decimals', 18) # Store decimals

        # Ask for withdrawal amount
        formatted_balance = f"{selected_token['balance']:,.6f}" # Display more decimals for tokens
        msg = (
            f"💰 <b>Withdraw {selected_token['symbol']}</b>\n"
            f"• <b>Token:</b> <code>{selected_token['address']}</code>\n"
            f"• <b>Your Balance:</b> <b>{formatted_balance} {selected_token['symbol']}</b>\n"
            f"• <b>Recipient:</b> <code>{context.user_data['withdraw_recipient']}</code>\n\n"
            f"💬 <b>Enter the amount of {selected_token['symbol']} to withdraw:</b>"
        )
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=ForceReply(selective=True))
        return WITHDRAW_AMOUNT
    
    # This part should ideally not be reached if the flow is correct, as withdraw_recipient_address
    # already sends the token list and prompts for the token address.
    # It's kept for robustness in case of unexpected skips in conversation.
    logging.warning("withdraw_token_select received unexpected input type or state.")
    if not update.effective_user:
        if update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❗️ Unable to determine your user ID.",
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    telegram_id = str(update.effective_user.id)
    address, _ = wallet_utils.get_wallet(telegram_id)
    if not address:
        response_text = "❗️ <b>No wallet found.</b> Use /start to create one."
        if update.message:
            await update.message.reply_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(response_text, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=response_text,
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    tokens = get_token_balances_from_explorer(address)
    if not tokens:
        msg = "❗️ <b>No tokens found in your wallet to withdraw.</b>"
        if update.message:
            await update.message.reply_text(msg, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(msg, parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        elif update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=msg,
                parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END
    msg = "<b>Your tokens available for withdrawal:</b>\n"
    for t in tokens:
        formatted_balance = f"{t['balance']:,.2f}"
        msg += f"• <code>{t['symbol']}</code>: <b>{formatted_balance}</b> (<code>{t['address']}</code>)\n"
    msg += "\n🔗 <b>Enter the token address you want to withdraw:</b>"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_home")]
    ])
    if update.message:
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(msg, parse_mode='HTML', reply_markup=keyboard)
    elif update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=msg,
            parse_mode='HTML', reply_markup=keyboard)
    return WITHDRAW_TOKEN_SELECT


async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'withdraw_amount')
    
    if update.callback_query: # This should not be hit if flow is correct
        await update.callback_query.answer()
        await reset_to_menu_handler(update, context)
        return ConversationHandler.END

    text = update.message.text.strip() if update.message and update.message.text else ""
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
        
        withdraw_type = context.user_data.get('withdraw_type')
        recipient = context.user_data['withdraw_recipient']
        
        if withdraw_type == 'eth':
            balance = context.user_data.get('withdraw_eth_balance', 0)
            if amount > balance:
                await update.message.reply_text("❗️ <b>Transfer request exceeds ETH balance, please enter a valid amount.</b>", parse_mode='HTML', reply_markup=ForceReply(selective=True))
                return WITHDRAW_AMOUNT
            context.user_data['withdraw_amount'] = amount
            msg = (
                f"⬆️ <b>Withdraw ETH Summary</b>\n"
                f"• <b>Recipient:</b> <code>{recipient}</code>\n"
                f"• <b>Amount:</b> <code>{amount} ETH</code>\n\n"
                "Do you want to proceed?"
            )
        else: # withdraw_type is 'token'
            balance = context.user_data.get('withdraw_token_balance', 0)
            symbol = context.user_data.get('withdraw_token_symbol', '?')
            if amount > balance:
                await update.message.reply_text(f"❗️ <b>Transfer request exceeds {symbol} balance, please enter a valid amount.</b>", parse_mode='HTML', reply_markup=ForceReply(selective=True))
                return WITHDRAW_AMOUNT
            context.user_data['withdraw_amount'] = amount
            msg = (
                f"⬆️ <b>Withdraw Token Summary</b>\n"
                f"• <b>Token:</b> <code>{context.user_data['withdraw_token_address']}</code>\n"
                f"• <b>Recipient:</b> <code>{recipient}</code>\n"
                f"• <b>Amount:</b> <code>{amount} {symbol}</code>\n\n"
                "Do you want to proceed?"
            )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="withdraw_confirm"),
             InlineKeyboardButton("❌ Cancel", callback_data="withdraw_cancel")]
        ])
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=keyboard)
        return WITHDRAW_CONFIRM
    except ValueError:
        await update.message.reply_text("❗️ <b>Please enter a valid numeric amount.</b>", parse_mode='HTML', reply_markup=ForceReply(selective=True))
        return WITHDRAW_AMOUNT
    except Exception as e:
        logging.error(f"Error in withdraw_amount: {e}")
        await update.message.reply_text(f"❌ <b>Error:</b> {e}", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        return ConversationHandler.END


async def withdraw_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'withdraw_confirm')
    query = update.callback_query
    await query.answer()

    if query.data == "withdraw_confirm":
        if not update.effective_user:
            await query.edit_message_text("❗️ <b>No wallet found.</b> Use /start to create one.", parse_mode='HTML', reply_markup=None)
            await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            return ConversationHandler.END
        telegram_id = str(update.effective_user.id)
        address, encrypted_pk = wallet_utils.get_wallet(telegram_id)
        if not address:
            await query.edit_message_text("❗️ <b>No wallet found.</b> Use /start to create one.", parse_mode='HTML', reply_markup=None)
            await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
            return ConversationHandler.END
        
        private_key = wallet_utils.decrypt_private_key(encrypted_pk)
        withdraw_type = context.user_data['withdraw_type']
        recipient = context.user_data['withdraw_recipient']
        amount = context.user_data['withdraw_amount']
        
        await query.edit_message_text("⏳ <b>Sending withdrawal...</b>", parse_mode='HTML', reply_markup=None)
        
        try:
            if withdraw_type == 'eth':
                value = int(amount * 1e18) # Convert ETH to Wei
                nonce = w3.eth.get_transaction_count(address)
                tx = {
                    'to': Web3.to_checksum_address(recipient),
                    'value': value,
                    'gas': 21000, # Standard ETH transfer gas limit
                    'gasPrice': w3.eth.gas_price,
                    'nonce': nonce,
                    'chainId': CHAIN_ID
                }
                signed_tx = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                await query.edit_message_text(
                    f"✅ <b>ETH sent!</b>\n<a href='https://explorer.inkonchain.com/tx/{tx_hash.hex()}'>View on Explorer</a>",
                    parse_mode='HTML', disable_web_page_preview=True, reply_markup=None)
            else: # withdraw_type is 'token'
                token_address = context.user_data['withdraw_token_address']
                token_decimals = context.user_data.get('withdraw_token_decimals', 18) # Get decimals from stored data
                
                # ERC-20 ABI for transfer function
                erc20_abi = [
                    {
                        "constant": False,
                        "inputs": [
                            {"name": "_to", "type": "address"},
                            {"name": "_value", "type": "uint256"}
                        ],
                        "name": "transfer",
                        "outputs": [
                            {"name": "success", "type": "bool"}
                        ],
                        "type": "function",
                        "stateMutability": "nonpayable"
                    }
                ]
                token_contract = w3.eth.contract(address=Web3.to_checksum_address(token_address), abi=erc20_abi)
                
                value = int(amount * (10**token_decimals)) # Convert token amount to its smallest unit using correct decimals

                nonce = w3.eth.get_transaction_count(address)
                
                tx = token_contract.functions.transfer(Web3.to_checksum_address(recipient), value).build_transaction({
                    'from': address,
                    'gas': 60000, # A common gas limit for ERC-20 transfers, but can vary
                    'gasPrice': w3.eth.gas_price,
                    'nonce': nonce,
                    'chainId': CHAIN_ID
                })
                signed_tx = w3.eth.account.sign_transaction(tx, private_key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                await query.edit_message_text(
                    f"✅ <b>Token sent!</b>\n<a href='https://explorer.inkonchain.com/tx/{tx_hash.hex()}'>View on Explorer</a>",
                    parse_mode='HTML', disable_web_page_preview=True, reply_markup=None)
            
            await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
        except Exception as e:
            logging.error(f"Error executing withdrawal: {e}")
            await query.edit_message_text(f"❌ <b>Error during withdrawal:</b> {e}", parse_mode='HTML', reply_markup=None)
            await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    else: # withdraw_cancel
        await query.edit_message_text("❌ <b>Withdrawal cancelled.</b>", parse_mode='HTML', reply_markup=None)
        await context.bot.send_message(chat_id=query.message.chat_id, text="🏠 <b>Main Menu</b>\nChoose an option below.", parse_mode='HTML', reply_markup=main_menu_inline_keyboard)
    return ConversationHandler.END


async def debug_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'debug_text_handler')
    print(f"DEBUG: Received text message: {update.message.text}")

async def reset_to_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_action(update, context, 'reset_to_menu_handler')
    query = update.callback_query
    await query.answer()
    if hasattr(context, 'user_data') and context.user_data is not None:
        context.user_data.clear()
    
    try:
        telegram_id = str(query.from_user.id) if query.from_user else None
        wallet_address = wallet_utils.get_wallet(telegram_id)[0] if telegram_id else 'N/A'

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🦑 <b>Welcome to <i>Inky Buy Bot</i>!</b>\n\n"
                 f"👛 <b>Your wallet:</b> <code>{wallet_address}</code>\n"
                 f"🌉 <b>Bridge ETH to Ink:</b> <a href='{BRIDGE_URL}'>{BRIDGE_URL}</a>\n\n"
                 "💡 <i>Use the menu below or type a command.</i>",
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=main_menu_inline_keyboard
        )

    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e) or "message is not found" in str(e) or "message can't be edited" in str(e):
            pass # Ignore "Message is not modified" errors or if message is gone
        else:
            logging.error(f"Error sending main menu in reset_to_menu_handler: {e}")
            raise
    return ConversationHandler.END

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("wallet", wallet))
    app.add_handler(CommandHandler("export_keys", export_keys))
    app.add_handler(CommandHandler("reset_wallet", reset_wallet))

    # General menu callback handlers that should not interfere with conversations
    app.add_handler(CallbackQueryHandler(wallet, pattern="^menu_wallet$"))
    app.add_handler(CallbackQueryHandler(manage_wallet, pattern="^menu_manage_wallet$"))
    app.add_handler(CallbackQueryHandler(export_keys, pattern="^manage_export_keys$"))
    app.add_handler(CallbackQueryHandler(reset_wallet, pattern="^manage_reset_wallet$"))
    app.add_handler(CallbackQueryHandler(reset_to_menu_handler, pattern="^menu_home$"))


    # --- Buy Conversation Handler ---
    buy_conv = ConversationHandler(
        entry_points=[
            CommandHandler("buy", buy),
            CallbackQueryHandler(buy, pattern="^menu_buy$")
        ],
        states={
            BUY_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, buy_token),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^buy_cancel$|^menu_home$"),
            ],
            BUY_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, buy_amount),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^buy_back$|^buy_cancel$|^menu_home$"),
            ],
            BUY_CONFIRM: [
                CallbackQueryHandler(buy_confirm),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^buy_cancel$|^menu_home$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", reset_to_menu_handler),
            CallbackQueryHandler(reset_to_menu_handler, pattern="^cancel_flow$"),
        ],
        allow_reentry=True
    )
    app.add_handler(buy_conv)

    # --- Sell Conversation Handler ---
    sell_conv = ConversationHandler(
        entry_points=[
            CommandHandler("sell", sell),
            CallbackQueryHandler(sell, pattern="^menu_sell$")
        ],
        states={
            SELL_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_token),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^sell_cancel$|^menu_home$"),
            ],
            SELL_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, sell_amount),
                CallbackQueryHandler(sell_amount_percent, pattern="^sell_pct_.*$"),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^sell_cancel$|^menu_home$"),
            ],
            SELL_CONFIRM: [
                CallbackQueryHandler(sell_confirm),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^sell_cancel$|^menu_home$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", reset_to_menu_handler),
            CallbackQueryHandler(reset_to_menu_handler, pattern="^cancel_flow$"),
        ],
        allow_reentry=True
    )
    app.add_handler(sell_conv)

    # --- Withdraw Conversation Handler (Revised) ---
    withdraw_conv = ConversationHandler(
        entry_points=[
            CommandHandler("withdraw", withdraw),
            CallbackQueryHandler(withdraw, pattern="^menu_withdraw$")
        ],
        states={
            WITHDRAW_TYPE: [
                CallbackQueryHandler(withdraw_type, pattern="^withdraw_eth$|^withdraw_token$"),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^menu_home$|^withdraw_cancel$"), # Added cancel
            ],
            WITHDRAW_RECIPIENT_ADDRESS: [ # New state name
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_recipient_address), # New function name
                CallbackQueryHandler(reset_to_menu_handler, pattern="^withdraw_cancel$|^menu_home$"),
            ],
            WITHDRAW_TOKEN_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_token_select),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^withdraw_cancel$|^menu_home$"),
            ],
            WITHDRAW_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^withdraw_cancel$|^menu_home$"),
            ],
            WITHDRAW_CONFIRM: [
                CallbackQueryHandler(withdraw_confirm, pattern="^withdraw_confirm$|^withdraw_cancel$"),
                CallbackQueryHandler(reset_to_menu_handler, pattern="^menu_home$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", reset_to_menu_handler),
            CallbackQueryHandler(reset_to_menu_handler, pattern="^cancel_flow$"),
        ],
        allow_reentry=True
    )
    app.add_handler(withdraw_conv)
    
    # Add global debug text handler LAST, so it only catches unhandled text messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, debug_text_handler))

    app.run_polling()

if __name__ == "__main__":
    main()