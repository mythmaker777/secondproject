"""
bot.py  —  Instagram Non-Follower Finder + Admin Commands
Supports PayNow (SGD) and Revolut (international).
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
from instagram_parser import parse_zip, parse_json_file, merge_and_compute

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────
AWAITING_FILE, AWAITING_SECOND_FILE, AWAITING_PAYMENT_METHOD, AWAITING_PAYMENT_REF = range(4)
AWAITING_BROADCAST = 10

# ── Payment method labels ─────────────────────────────────
METHOD_PAYNOW  = "paynow"
METHOD_REVOLUT = "revolut"

# ── Static copy ───────────────────────────────────────────
HOW_TO_EXPORT = """📥 *How to download your Instagram data:*

1. Open Instagram → *Profile* → ☰ Menu
2. Tap *Your activity* → *Download your information*
3. Select *Some of your information*
4. Tick ✅ *Followers and following* only
5. Choose *JSON* format _(not HTML!)_
6. Tap *Create files* — Instagram will email you a link

Download the ZIP from that email and send it here.

⏱ _Instagram usually sends the email within 10–30 minutes — come back when it arrives!_

_You can also send the two files individually: `followers_1.json` and `following.json`._"""

WELCOME = """👋 Welcome to *InstaSpy* 🔍

I'll show you which Instagram accounts you follow that *don't follow you back* — without ever needing your password.

━━━━━━━━━━━━━━━━━━━━
*How it works:*
1️⃣ Download your Instagram data export
2️⃣ Upload it here (ZIP or two JSON files)
3️⃣ See your follower stats — free
4️⃣ Pay a small fee to unlock the full list

Your files are processed in memory and *never stored*. 🔒"""


# ── Guards ────────────────────────────────────────────────

def admin_only(func):
    """Decorator — silently ignore non-admin callers."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
            return
        return await func(update, context)
    return wrapper


# ── Shared helpers ────────────────────────────────────────

async def download_file_bytes(bot, doc) -> bytes:
    tg_file = await bot.get_file(doc.file_id)
    buf = bytearray()
    await tg_file.download_as_bytearray(buf)
    return bytes(buf)


def _payment_method_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📱 PayNow (SGD)",  callback_data=f"pay_method:{METHOD_PAYNOW}"),
        InlineKeyboardButton("💳 Revolut",        callback_data=f"pay_method:{METHOD_REVOLUT}"),
    ]])


def _request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{request_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{request_id}"),
    ]])


def _method_label(method: str) -> str:
    return "PayNow" if method == METHOD_PAYNOW else "Revolut"


def _fmt_request(req: dict) -> str:
    handle = f" (@{req['tg_username']})" if req.get("tg_username") else ""
    method = req.get("payment_method") or "unknown"
    method_emoji = "📱" if method == METHOD_PAYNOW else "💳"
    return (
        f"🆔 Request *#{req['id']}*\n"
        f"👤 Telegram ID: `{req['telegram_id']}`{handle}\n"
        f"📊 Following *{req['following_count']}* · "
        f"Non-followers: *{req['non_follower_count']}*\n"
        f"{method_emoji} Method: *{_method_label(method)}*\n"
        f"💳 Ref: `{req.get('payment_ref') or '—'}`\n"
        f"🕐 {req['created_at'][:16]}"
    )


async def _deliver_results(bot, req: dict):
    """Send the unlocked non-follower list to the user."""
    non_followers = json.loads(req["result_data"] or "[]")
    if not non_followers:
        text = (
            "✅ *Payment approved!*\n\n"
            "🎉 Great news — everyone you follow actually follows you back!"
        )
    else:
        lines = "\n".join(f"• @{u}" for u in non_followers)
        text = (
            f"✅ *Payment approved!*\n\n"
            f"Here are the *{len(non_followers)}* accounts that don't follow you back:\n\n"
            f"{lines}"
        )
    await bot.send_message(chat_id=req["telegram_id"], text=text, parse_mode="Markdown")


# ── User flow ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.upsert_user(user.id, user.username or user.first_name)
    context.user_data.clear()
    await update.message.reply_text(WELCOME, parse_mode="Markdown")
    await update.message.reply_text(HOW_TO_EXPORT, parse_mode="Markdown")
    await update.message.reply_text(
        "⬆️ *Send your file when ready.*\n\n"
        "• `.zip` — the full Instagram export\n"
        "• OR `followers_1.json` then `following.json`\n\n"
        "_/cancel to stop._",
        parse_mode="Markdown",
    )
    return AWAITING_FILE


async def show_paywall(result: dict, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Store the result and ask the user to choose a payment method."""
    user = update.effective_user
    request_id = db.create_request(
        telegram_id=user.id,
        ig_username=f"tg:{user.username or user.id}",
        non_followers=result["non_followers"],
        following_count=result["following_count"],
    )
    context.user_data["request_id"] = request_id

    await update.message.reply_text(
        f"✅ *Analysis complete!*\n\n"
        f"👥 You follow *{result['following_count']}* accounts\n"
        f"🤝 *{result['followers_count']}* follow you back\n"
        f"🔒 Your non-follower results are ready\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🔓 *Unlock your full results for SGD {config.PAYMENT_AMOUNT:.2f}*\n\n"
        "How would you like to pay?",
        parse_mode="Markdown",
        reply_markup=_payment_method_keyboard(),
    )
    return AWAITING_PAYMENT_METHOD


async def payment_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tapped PayNow or Revolut — show the relevant payment details."""
    query = update.callback_query
    await query.answer()

    _, method = query.data.split(":")
    context.user_data["payment_method"] = method

    if method == METHOD_PAYNOW:
        text = (
            f"📱 *Pay via PayNow*\n\n"
            f"PayNow to: `{config.PAYNOW_NUMBER}`\n"
            f"Amount: *SGD {config.PAYMENT_AMOUNT:.2f}*\n\n"
            "Once paid, reply here with your *PayNow transaction reference number*. ⚡"
        )
    else:
        text = (
            f"💳 *Pay via Revolut*\n\n"
            f"Revolut RevTag: `{config.REVOLUT_REVTAG}`\n"
            f"Amount: *SGD {config.PAYMENT_AMOUNT:.2f}* (or equivalent)\n\n"
            "Once paid, reply here with your *Revolut transaction reference number*. ⚡\n\n"
            "_Tip: You can find the reference in your Revolut transaction history._"
        )

    await query.edit_message_text(text, parse_mode="Markdown")
    return AWAITING_PAYMENT_REF


async def received_payment_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment_ref    = update.message.text.strip()
    request_id     = context.user_data.get("request_id")
    payment_method = context.user_data.get("payment_method", "unknown")

    if not request_id:
        await update.message.reply_text("⚠️ Session expired. Type /start to begin again.")
        return ConversationHandler.END

    db.update_payment_ref(request_id, payment_ref, payment_method)
    req  = db.get_request(request_id)
    user = update.effective_user
    tg_handle = f"@{user.username}" if user.username else user.first_name

    await update.message.reply_text(
        "📨 *Reference received — thank you!*\n\n"
        "Our team will verify your payment and send your results shortly.\n"
        "⏱ Typical wait: within a few hours.",
        parse_mode="Markdown",
    )

    # Notify admin with approve/reject buttons
    await update.get_bot().send_message(
        chat_id=config.ADMIN_TELEGRAM_ID,
        text=(
            f"🔔 *New payment to verify!*\n\n"
            f"{_fmt_request(req)}\n"
            f"👤 Handle: {tg_handle}"
        ),
        parse_mode="Markdown",
        reply_markup=_request_keyboard(request_id),
    )
    return ConversationHandler.END


async def _handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send a file. Type /start for instructions.")
        return AWAITING_FILE

    fname = (doc.file_name or "file").lower()
    status = await update.message.reply_text("⏳ Reading your file...")
    file_bytes = await download_file_bytes(update.get_bot(), doc)

    # ZIP ──────────────────────────────────────────────────
    if fname.endswith(".zip"):
        result = await asyncio.get_event_loop().run_in_executor(None, parse_zip, file_bytes)
        if not result["success"]:
            await status.edit_text(
                f"❌ *Couldn't read the ZIP.*\n\n{result['error']}\n\nType /start to try again.",
                parse_mode="Markdown",
            )
            return AWAITING_FILE
        await status.delete()
        return await show_paywall(result, update, context)

    # JSON ─────────────────────────────────────────────────
    elif fname.endswith(".json"):
        parsed = parse_json_file(file_bytes, doc.file_name or fname)
        if not parsed["success"]:
            await status.edit_text(
                f"❌ {parsed['error']}\n\nType /start to restart.",
                parse_mode="Markdown",
            )
            return AWAITING_FILE

        file_type  = parsed["type"]
        other_type = "following" if file_type == "followers" else "followers"
        context.user_data[f"json_{file_type}"] = parsed
        await status.delete()

        if context.user_data.get(f"json_{other_type}"):
            result = merge_and_compute(
                context.user_data["json_followers"],
                context.user_data["json_following"],
            )
            if not result["success"]:
                await update.message.reply_text(
                    f"❌ {result['error']}\n\nType /start to restart.",
                    parse_mode="Markdown",
                )
                return AWAITING_FILE
            return await show_paywall(result, update, context)
        else:
            needed = "following.json" if file_type == "followers" else "followers_1.json"
            await update.message.reply_text(
                f"✅ Got your *{file_type}* file! Now send `{needed}`.",
                parse_mode="Markdown",
            )
            return AWAITING_SECOND_FILE
    else:
        await status.edit_text("⚠️ Please send a `.zip` or `.json` file.")
        return AWAITING_FILE


async def received_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_file(update, context)

async def received_second_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_file(update, context)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled. Type /start to begin again.")
    return ConversationHandler.END


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *InstaSpy Help*\n\n"
        "/start — Begin the non-follower analysis\n"
        "/cancel — Cancel the current operation\n"
        "/help — Show this message\n\n"
        "💳 *Payment options:* PayNow (SGD) or Revolut\n\n"
        "📥 *Your privacy:*\n"
        "We never ask for your Instagram password. "
        "You download your own data directly from Instagram and upload the export file. "
        "Files are processed in memory and never stored.",
        parse_mode="Markdown",
    )


async def unexpected_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please send your Instagram export file, or type /start."
    )


# ── Admin commands ────────────────────────────────────────

@admin_only
async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reqs = db.get_all_requests(status_filter="payment_submitted")
    if not reqs:
        await update.message.reply_text("✅ No pending payments right now.")
        return
    await update.message.reply_text(
        f"⏳ *{len(reqs)} pending payment(s):*", parse_mode="Markdown"
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
        f"📊 *InstaSpy Stats*\n\n"
        f"👤 Total users:    *{s['total_users']}*\n"
        f"📋 Total requests: *{s['total_requests']}*\n"
        f"⏳ Pending:        *{s['pending']}*\n"
        f"✅ Approved:       *{s['approved']}*\n"
        f"❌ Rejected:       *{s['rejected']}*",
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
        await update.message.reply_text(f"❌ Request #{request_id} not found.")
        return
    if req["status"] == "approved":
        await update.message.reply_text("Already approved.")
        return
    db.update_request_status(request_id, "approved")
    await _deliver_results(update.get_bot(), req)
    await update.message.reply_text(f"✅ Request #{request_id} approved — results sent.")


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
        await update.message.reply_text(f"❌ Request #{request_id} not found.")
        return
    db.update_request_status(request_id, "rejected")
    await update.get_bot().send_message(
        chat_id=req["telegram_id"],
        text=(
            f"❌ *Your payment could not be verified.*\n\n"
            f"Reason: {reason}\n\n"
            "Please try again with /start or contact support."
        ),
        parse_mode="Markdown",
    )
    await update.message.reply_text(f"❌ Request #{request_id} rejected.")


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
    await update.message.reply_text(f"📢 Done. ✅ Sent: {sent}  ❌ Failed: {failed}")
    return ConversationHandler.END


# ── Inline button callbacks ───────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Payment method selection (user-facing)
    if query.data.startswith("pay_method:"):
        return await payment_method_callback(update, context)

    # Admin approve/reject
    if query.from_user.id != config.ADMIN_TELEGRAM_ID:
        await query.answer("Not authorised.", show_alert=True)
        return

    action, rid_str = query.data.split(":")
    request_id = int(rid_str)
    req = db.get_request(request_id)

    if not req:
        await query.edit_message_text("❌ Request not found.")
        return

    if req["status"] in ("approved", "rejected"):
        await query.edit_message_text(
            query.message.text + f"\n\n_Already {req['status']}._",
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


# ── App wiring ────────────────────────────────────────────

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
            AWAITING_PAYMENT_METHOD: [
                # Handled via CallbackQueryHandler below (inline buttons don't
                # go through MessageHandler), but we keep the state so the
                # conversation doesn't time out waiting for a text message.
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
    # Single CallbackQueryHandler catches both pay_method and approve/reject
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(user_conv)
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
