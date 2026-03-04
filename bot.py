"""
bot.py - Respectagram Non-Follower Finder + Admin Commands
Supports PayNow (SGD) and Revolut (international).
Accepts ZIP exports in JSON or HTML format.
"""

import asyncio
import json
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

import config
import database as db
from instagram_parser import parse_zip, parse_upload, merge_and_compute

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states
AWAITING_FILE           = 0
AWAITING_SECOND_FILE    = 1
AWAITING_PAYMENT_METHOD = 2
AWAITING_PAYMENT_REF    = 3
AWAITING_BROADCAST      = 10

METHOD_PAYNOW  = "paynow"
METHOD_REVOLUT = "revolut"

HOW_TO_EXPORT = (
    "📥 *How to download your Instagram data:*\n\n"
    "1. Open Instagram → *Profile* → ☰ Menu\n"
    "2. Tap *Accounts Center*\n"
    "3. Tap *Your information and permissions*\n"
    "4. Tap *Download your information*\n"
    "5. Tap *Request a download*\n"
    "6. Select your Instagram profile\n"
    "7. Tap *Some of your information*\n"
    "8. Deselect everything, then select *Followers and following* only\n"
    "9. Tap *Download to device*\n"
    "10. Set the date range to *All time* for the most accurate results\n"
    "11. Choose *JSON* format if available, otherwise leave as HTML\n"
    "12. Tap *Create files*\n\n"
    "Instagram will email you a download link — usually within 10-30 minutes. "
    "Come back when it arrives and send the ZIP file here!"
)

WELCOME = (
    "👋 Welcome to *Respectagram* 🔍\n\n"
    "I'll show you which Instagram accounts you follow that "
    "*don't follow you back* — without ever needing your password.\n\n"
    "━━━━━━━━━━━━━━━━━━━━\n"
    "*How it works:*\n"
    "1️⃣ Download your Instagram data export\n"
    "2️⃣ Upload the ZIP file here\n"
    "3️⃣ See your follower stats — free\n"
    "4️⃣ Pay a small fee to unlock the full list\n\n"
    "Your files are processed in memory and *never stored*. 🔒"
)


# ── Guards ─────────────────────────────────────────────────

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
            return
        return await func(update, context)
    return wrapper


# ── Helpers ────────────────────────────────────────────────

async def download_file_bytes(bot, doc) -> bytes:
    tg_file = await bot.get_file(doc.file_id)
    buf = bytearray()
    await tg_file.download_as_bytearray(buf)
    return bytes(buf)


def _payment_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📱 PayNow (SGD)", callback_data="pay_method:" + METHOD_PAYNOW),
        InlineKeyboardButton("💳 Revolut",       callback_data="pay_method:" + METHOD_REVOLUT),
    ]])


def _request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data="approve:" + str(request_id)),
        InlineKeyboardButton("❌ Reject",  callback_data="reject:"  + str(request_id)),
    ]])


def _method_label(method: str) -> str:
    return "PayNow" if method == METHOD_PAYNOW else "Revolut"


def _fmt_request(req: dict) -> str:
    handle       = (" (@" + req["tg_username"] + ")") if req.get("tg_username") else ""
    method       = req.get("payment_method") or "unknown"
    method_emoji = "📱" if method == METHOD_PAYNOW else "💳"
    return (
        "🆔 Request *#" + str(req["id"]) + "*\n"
        "👤 Telegram ID: `" + str(req["telegram_id"]) + "`" + handle + "\n"
        "📊 Following *" + str(req["following_count"]) + "* · "
        "Non-followers: *" + str(req["non_follower_count"]) + "*\n"
        + method_emoji + " Method: *" + _method_label(method) + "*\n"
        "💳 Ref: `" + str(req.get("payment_ref") or "—") + "`\n"
        "🕐 " + req["created_at"][:16]
    )


async def _deliver_results(bot, req: dict):
    non_followers = json.loads(req["result_data"] or "[]")
    if not non_followers:
        text = (
            "✅ *Payment approved!*\n\n"
            "🎉 Great news — everyone you follow actually follows you back!"
        )
    else:
        lines = "\n".join("• @" + u for u in non_followers)
        text = (
            "✅ *Payment approved!*\n\n"
            "Here are the *" + str(len(non_followers)) + "* accounts "
            "that don't follow you back:\n\n" + lines + "\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📋 *How to cross-check these results:*\n\n"
            "Open Instagram and search each username in your following list. "
            "If the account appears, they are following you — "
            "they simply chose not to follow back.\n\n"
            "If the account cannot be found or shows as unavailable, "
            "it has been deactivated or deleted since you followed it — "
            "not an unfollower, just a ghost account. 👻"
        )
    await bot.send_message(chat_id=req["telegram_id"], text=text, parse_mode="Markdown")


# ── User flow ──────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username or user.first_name)
    context.user_data.clear()
    await update.message.reply_text(WELCOME, parse_mode="Markdown")
    await update.message.reply_text(HOW_TO_EXPORT, parse_mode="Markdown")
    await update.message.reply_text(
        "⬆️ *Send your ZIP file when ready.*\n\n"
        "You can also send the individual files: "
        "`followers_1.json` then `following.json` "
        "(or `.html` if that is the format you received)\n\n"
        "_/cancel to stop._",
        parse_mode="Markdown",
    )
    return AWAITING_FILE


async def show_paywall(result: dict, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    request_id = db.create_request(
        telegram_id=user.id,
        ig_username="tg:" + str(user.username or user.id),
        non_followers=result["non_followers"],
        following_count=result["following_count"],
    )
    context.user_data["request_id"] = request_id

    await update.message.reply_text(
        "✅ *Analysis complete!*\n\n"
        "We've finished scanning your Instagram data. "
        "You might be surprised by what we found — some of the results are... unexpected. 👀\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔓 *Unlock your full non-follower list for SGD " + str(config.PAYMENT_AMOUNT) + "*\n\n"
        "How would you like to pay?",
        parse_mode="Markdown",
        reply_markup=_payment_method_keyboard(),
    )
    return AWAITING_PAYMENT_METHOD


async def payment_method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Handles PayNow / Revolut button tap.
    Registered INSIDE the ConversationHandler under AWAITING_PAYMENT_METHOD
    so the state correctly advances to AWAITING_PAYMENT_REF.
    """
    query = update.callback_query
    await query.answer()

    method = query.data.split(":")[1]
    context.user_data["payment_method"] = method

    if method == METHOD_PAYNOW:
        text = (
            "📱 *Pay via PayNow*\n\n"
            "PayNow to: `" + config.PAYNOW_NUMBER + "`\n"
            "Amount: *SGD " + str(config.PAYMENT_AMOUNT) + "*\n\n"
            "Once paid, *reply here* with your PayNow transaction reference number. ⚡"
        )
    else:
        text = (
            "💳 *Pay via Revolut*\n\n"
            "Revolut RevTag: `" + config.REVOLUT_REVTAG + "`\n"
            "Amount: *SGD " + str(config.PAYMENT_AMOUNT) + "* (or equivalent)\n\n"
            "Once paid, *reply here* with your Revolut transaction reference number. ⚡\n\n"
            "_Tip: Find the reference in your Revolut transaction history._"
        )

    await query.edit_message_text(text, parse_mode="Markdown")
    return AWAITING_PAYMENT_REF


async def received_payment_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    payment_ref    = update.message.text.strip()
    request_id     = context.user_data.get("request_id")
    payment_method = context.user_data.get("payment_method", "unknown")

    if not request_id:
        await update.message.reply_text(
            "⚠️ Session expired. Please type /start to begin again."
        )
        return ConversationHandler.END

    db.update_payment_ref(request_id, payment_ref, payment_method)
    req       = db.get_request(request_id)
    user      = update.effective_user
    tg_handle = ("@" + user.username) if user.username else user.first_name

    await update.message.reply_text(
        "📨 *Reference received — thank you!*\n\n"
        "Our team will verify your payment and send your results shortly.\n"
        "⏱ Typical wait: within a few hours.",
        parse_mode="Markdown",
    )

    await update.get_bot().send_message(
        chat_id=config.ADMIN_TELEGRAM_ID,
        text=(
            "🔔 *New payment to verify!*\n\n"
            + _fmt_request(req) + "\n"
            "👤 Handle: " + tg_handle
        ),
        parse_mode="Markdown",
        reply_markup=_request_keyboard(request_id),
    )
    return ConversationHandler.END


async def _handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc:
        await update.message.reply_text(
            "Please send a file. Type /start for instructions."
        )
        return AWAITING_FILE

    fname      = (doc.file_name or "file").lower()
    status     = await update.message.reply_text("⏳ Reading your file...")
    file_bytes = await download_file_bytes(update.get_bot(), doc)

    # ZIP — auto-detects JSON or HTML inside
    if fname.endswith(".zip"):
        result = await asyncio.get_event_loop().run_in_executor(None, parse_zip, file_bytes)
        if not result["success"]:
            await status.edit_text(
                "❌ *Couldn't read the ZIP.*\n\n" + result["error"] + "\n\nType /start to try again.",
                parse_mode="Markdown",
            )
            return AWAITING_FILE
        await status.delete()
        return await show_paywall(result, update, context)

    # Individual JSON or HTML file
    elif fname.endswith(".json") or fname.endswith(".html"):
        parsed = parse_upload(file_bytes, doc.file_name or fname)
        if not parsed["success"]:
            await status.edit_text(
                "❌ " + parsed["error"] + "\n\nType /start to restart.",
                parse_mode="Markdown",
            )
            return AWAITING_FILE

        file_type  = parsed["type"]
        other_type = "following" if file_type == "followers" else "followers"
        context.user_data["upload_" + file_type] = parsed
        await status.delete()

        if context.user_data.get("upload_" + other_type):
            result = merge_and_compute(
                context.user_data["upload_followers"],
                context.user_data["upload_following"],
            )
            if not result["success"]:
                await update.message.reply_text(
                    "❌ " + result["error"] + "\n\nType /start to restart.",
                    parse_mode="Markdown",
                )
                return AWAITING_FILE
            return await show_paywall(result, update, context)
        else:
            if file_type == "followers":
                needed = "following.json (or following.html)"
            else:
                needed = "followers_1.json (or followers_1.html)"
            await update.message.reply_text(
                "✅ Got your *" + file_type + "* file! Now send `" + needed + "`.",
                parse_mode="Markdown",
            )
            return AWAITING_SECOND_FILE

    else:
        await status.edit_text(
            "⚠️ Please send the `.zip` file from your Instagram export email.\n\n"
            "You can also send the individual files directly: "
            "`followers_1.json` and `following.json` "
            "(or `.html` versions if that is the format you received)."
        )
        return AWAITING_FILE


async def received_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_file(update, context)


async def received_second_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_file(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled. Type /start to begin again.")
    return ConversationHandler.END


async def unexpected_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please send your Instagram export ZIP file, or type /start."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Respectagram Help*\n\n"
        "/start — Begin the non-follower analysis\n"
        "/cancel — Cancel the current operation\n"
        "/help — Show this message\n\n"
        "💳 *Payment options:* PayNow (SGD) or Revolut\n\n"
        "🔒 *Your privacy:* We never ask for your Instagram password. "
        "Files are processed in memory and never stored.",
        parse_mode="Markdown",
    )


# ── Admin commands ──────────────────────────────────────────

@admin_only
async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reqs = db.get_all_requests(status_filter="payment_submitted")
    if not reqs:
        await update.message.reply_text("✅ No pending payments right now.")
        return
    await update.message.reply_text(
        "⏳ *" + str(len(reqs)) + " pending payment(s):*", parse_mode="Markdown"
    )
    for req in reqs:
        await update.message.reply_text(
            _fmt_request(req),
            parse_mode="Markdown",
            reply_markup=_request_keyboard(req["id"]),
        )


@admin_only
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = db.get_stats()
    await update.message.reply_text(
        "📊 *Respectagram Stats*\n\n"
        "👤 Total users:    *" + str(s["total_users"])    + "*\n"
        "📋 Total requests: *" + str(s["total_requests"]) + "*\n"
        "⏳ Pending:        *" + str(s["pending"])        + "*\n"
        "✅ Approved:       *" + str(s["approved"])       + "*\n"
        "❌ Rejected:       *" + str(s["rejected"])       + "*",
        parse_mode="Markdown",
    )


@admin_only
async def admin_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /approve <request_id>")
        return
    request_id = int(args[0])
    req = db.get_request(request_id)
    if not req:
        await update.message.reply_text("❌ Request #" + str(request_id) + " not found.")
        return
    if req["status"] == "approved":
        await update.message.reply_text("Already approved.")
        return
    db.update_request_status(request_id, "approved")
    await _deliver_results(update.get_bot(), req)
    await update.message.reply_text("✅ Request #" + str(request_id) + " approved — results sent.")


@admin_only
async def admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /reject <request_id> [reason]")
        return
    request_id = int(args[0])
    reason = " ".join(args[1:]) if len(args) > 1 else "Payment could not be verified."
    req = db.get_request(request_id)
    if not req:
        await update.message.reply_text("❌ Request #" + str(request_id) + " not found.")
        return
    db.update_request_status(request_id, "rejected")
    await update.get_bot().send_message(
        chat_id=req["telegram_id"],
        text=(
            "❌ *Your payment could not be verified.*\n\n"
            "Reason: " + reason + "\n\n"
            "Please try again with /start or contact support."
        ),
        parse_mode="Markdown",
    )
    await update.message.reply_text("❌ Request #" + str(request_id) + " rejected.")


@admin_only
async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📢 *Broadcast mode*\n\nType the message to send to all users.\n_/cancel to abort._",
        parse_mode="Markdown",
    )
    return AWAITING_BROADCAST


@admin_only
async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message.text.strip()
    users   = db.get_all_users()
    sent = failed = 0
    for u in users:
        try:
            await update.get_bot().send_message(chat_id=u["telegram_id"], text=message)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        "📢 Done. ✅ Sent: " + str(sent) + "  ❌ Failed: " + str(failed)
    )
    return ConversationHandler.END


# ── Admin inline button callbacks ──────────────────────────

async def admin_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != config.ADMIN_TELEGRAM_ID:
        await query.answer("Not authorised.", show_alert=True)
        return

    parts      = query.data.split(":")
    action     = parts[0]
    request_id = int(parts[1])
    req        = db.get_request(request_id)

    if not req:
        await query.edit_message_text("❌ Request not found.")
        return

    if req["status"] in ("approved", "rejected"):
        await query.edit_message_text(
            query.message.text + "\n\n_Already " + req["status"] + "._",
            parse_mode="Markdown",
        )
        return

    if action == "approve":
        db.update_request_status(request_id, "approved")
        await _deliver_results(context.bot, req)
        await query.edit_message_text(
            query.message.text + "\n\n✅ *Approved — results sent to user.*",
            parse_mode="Markdown",
        )
    elif action == "reject":
        db.update_request_status(request_id, "rejected")
        await context.bot.send_message(
            chat_id=req["telegram_id"],
            text=(
                "❌ *Your payment could not be verified.*\n\n"
                "Please try again with /start or contact support."
            ),
            parse_mode="Markdown",
        )
        await query.edit_message_text(
            query.message.text + "\n\n❌ *Rejected.*",
            parse_mode="Markdown",
        )


# ── App wiring ─────────────────────────────────────────────

def main():
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", admin_broadcast_start)],
        states={
            AWAITING_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    user_conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            AWAITING_FILE: [
                MessageHandler(filters.Document.ALL, received_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, unexpected_text),
            ],
            AWAITING_SECOND_FILE: [
                MessageHandler(filters.Document.ALL, received_second_file),
            ],
            # pay_method button handled INSIDE conversation so state advances correctly
            AWAITING_PAYMENT_METHOD: [
                CallbackQueryHandler(payment_method_chosen, pattern="^pay_method:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, unexpected_text),
            ],
            AWAITING_PAYMENT_REF: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, received_payment_ref),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(broadcast_conv)
    app.add_handler(CommandHandler("pending",  admin_pending))
    app.add_handler(CommandHandler("stats",    admin_stats))
    app.add_handler(CommandHandler("approve",  admin_approve))
    app.add_handler(CommandHandler("reject",   admin_reject))
    app.add_handler(CommandHandler("help",     cmd_help))
    # Admin approve/reject buttons — outside conversation, pattern-matched
    app.add_handler(CallbackQueryHandler(admin_button_callback, pattern="^(approve|reject):"))
    # User conversation last
    app.add_handler(user_conv)

    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
