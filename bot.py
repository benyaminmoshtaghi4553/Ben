import asyncio
import json
import logging
import os
import random
import sqlite3
from datetime import datetime, timezone

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_FILE = "scores.db"
QUESTIONS_FILE = "questions.json"

SESSION_LENGTH_DEFAULT = 5
NEXT_QUESTION_DELAY_SECONDS = 2  # short pause before auto-sending next question

# Telegram numeric user IDs allowed to use admin commands.
ADMIN_IDS = {7906761982}

# ---------- Main menu (persistent reply keyboard) ----------

BTN_QUIZ = "🎯 شروع بازی"
BTN_SCORE = "🏆 امتیاز من"
BTN_LEADERBOARD = "📊 جدول برترین‌ها"

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_QUIZ], [BTN_SCORE, BTN_LEADERBOARD]],
    resize_keyboard=True,
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------- Database ----------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER,
            chat_id INTEGER,
            username TEXT,
            score INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, chat_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            private_chat_id INTEGER,
            is_blocked INTEGER DEFAULT 0,
            questions_answered INTEGER DEFAULT 0,
            first_seen TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def track_user(user, chat):
    """Upsert basic info about anyone who interacts with the bot."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    private_chat_id = chat.id if chat.type == "private" else None
    username = user.username or user.first_name
    cur.execute(
        """
        INSERT INTO users (user_id, username, private_chat_id, first_seen)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username = excluded.username,
            private_chat_id = COALESCE(excluded.private_chat_id, users.private_chat_id)
        """,
        (user.id, username, private_chat_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def is_blocked(user_id: int) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0])


def set_blocked(user_id: int, blocked: bool):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE users SET is_blocked = ? WHERE user_id = ?", (1 if blocked else 0, user_id))
    conn.commit()
    conn.close()


def increment_questions_answered(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET questions_answered = questions_answered + 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def add_point(user_id: int, chat_id: int, username: str, amount: int = 1):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scores (user_id, chat_id, username, score)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, chat_id)
        DO UPDATE SET score = score + excluded.score, username = excluded.username
        """,
        (user_id, chat_id, username, amount),
    )
    conn.commit()
    conn.close()


def get_score(user_id: int, chat_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT score FROM scores WHERE user_id = ? AND chat_id = ?",
        (user_id, chat_id),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def get_leaderboard(chat_id: int, limit: int = 10):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT username, score FROM scores WHERE chat_id = ? ORDER BY score DESC LIMIT ?",
        (chat_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_global_stats():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]

    cur.execute("SELECT COALESCE(SUM(questions_answered), 0) FROM users")
    total_answered = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 1")
    total_blocked = cur.fetchone()[0]

    cur.execute(
        """
        SELECT username, SUM(score) as total_score
        FROM scores
        GROUP BY user_id
        ORDER BY total_score DESC
        LIMIT 5
        """
    )
    top_scorers = cur.fetchall()

    conn.close()
    return {
        "total_users": total_users,
        "total_answered": total_answered,
        "total_blocked": total_blocked,
        "top_scorers": top_scorers,
    }


def get_all_private_chat_ids():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT private_chat_id FROM users WHERE private_chat_id IS NOT NULL AND is_blocked = 0")
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


# ---------- Questions ----------

def load_questions():
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


QUESTIONS = load_questions()

# Tracks the correct answer index for each currently-active question message.
# Keyed by (chat_id, message_id) -> correct_option_index
ACTIVE_QUESTIONS = {}

# Tracks an in-progress multi-question session per chat.
# Keyed by chat_id -> {
#   "questions": [list of question dicts for this session],
#   "index": current question number (0-based),
#   "correct_count": how many were answered correctly during the session,
#   "participants": set of user_ids who answered at least one question,
# }
ACTIVE_SESSIONS = {}


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)
    await update.message.reply_text(
        "سلام! 👋 به بات کوییز خوش اومدی.\n\n"
        "از دکمه‌های پایین صفحه استفاده کن، یا این دستورات رو بنویس:\n"
        "/quiz - شروع یه دوره ۵ سوالی (سوالات خودشون پشت‌سرهم میان)\n"
        "/quiz 10 - شروع یه دوره با تعداد دلخواه سوال\n"
        "/score - امتیاز کلی خودت رو ببین\n"
        "/leaderboard - جدول برترین‌ها\n",
        reply_markup=MAIN_MENU,
    )


async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE, q: dict, q_number: int, total: int):
    buttons = [
        [InlineKeyboardButton(opt, callback_data=str(i))]
        for i, opt in enumerate(q["options"])
    ]
    markup = InlineKeyboardMarkup(buttons)

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"❓ سوال {q_number}/{total}\n\n{q['question']}",
        reply_markup=markup,
    )

    ACTIVE_QUESTIONS[(chat_id, msg.message_id)] = q["correct"]


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)

    if is_blocked(update.effective_user.id):
        await update.message.reply_text("متاسفانه دسترسی تو به بات مسدود شده.")
        return

    chat_id = update.effective_chat.id

    if chat_id in ACTIVE_SESSIONS:
        await update.message.reply_text("یه دوره کوییز همین الان در حال اجراست، اول اونو تموم کن! 🙂")
        return

    # Optional argument: /quiz 10
    length = SESSION_LENGTH_DEFAULT
    if context.args:
        try:
            length = max(1, min(int(context.args[0]), len(QUESTIONS)))
        except ValueError:
            pass

    length = min(length, len(QUESTIONS))
    session_questions = random.sample(QUESTIONS, length)

    ACTIVE_SESSIONS[chat_id] = {
        "questions": session_questions,
        "index": 0,
        "correct_count": 0,
        "participants": set(),
    }

    await update.message.reply_text(f"🎯 یه دوره {length} سوالی شروع شد! آماده باش...")
    await send_question(chat_id, context, session_questions[0], 1, length)


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    key = (chat_id, message_id)

    if key not in ACTIVE_QUESTIONS:
        await query.answer("این سوال قبلاً جواب داده شده!", show_alert=True)
        return

    user = query.from_user
    track_user(user, query.message.chat)

    if is_blocked(user.id):
        await query.answer("دسترسی تو به بات مسدود شده.", show_alert=True)
        return

    correct_index = ACTIVE_QUESTIONS.pop(key)
    chosen_index = int(query.data)
    username = user.username or user.first_name

    session = ACTIVE_SESSIONS.get(chat_id)
    increment_questions_answered(user.id)

    if chosen_index == correct_index:
        add_point(user.id, chat_id, username)
        new_score = get_score(user.id, chat_id)
        await query.answer("✅ آفرین، درست بود!")
        await query.edit_message_text(
            f"{query.message.text}\n\n✅ {username} درست جواب داد! (امتیاز کلی: {new_score})"
        )
        if session is not None:
            session["correct_count"] += 1
    else:
        await query.answer("❌ اشتباه بود!")
        correct_text = query.message.reply_markup.inline_keyboard[correct_index][0].text
        await query.edit_message_text(
            f"{query.message.text}\n\n❌ {username} اشتباه جواب داد. (جواب درست: {correct_text})"
        )

    # If this question belongs to an active session, move on automatically.
    if session is not None:
        session["participants"].add(user.id)
        session["index"] += 1
        total = len(session["questions"])

        if session["index"] < total:
            await asyncio.sleep(NEXT_QUESTION_DELAY_SECONDS)
            next_q = session["questions"][session["index"]]
            await send_question(chat_id, context, next_q, session["index"] + 1, total)
        else:
            correct_count = session["correct_count"]
            del ACTIVE_SESSIONS[chat_id]
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🏁 دوره تموم شد!\n"
                    f"از {total} سوال، {correct_count} تا درست جواب داده شد.\n\n"
                    f"برای شروع دوباره: /quiz"
                ),
            )


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)
    user = update.effective_user
    chat_id = update.effective_chat.id
    s = get_score(user.id, chat_id)
    await update.message.reply_text(f"امتیاز تو: {s} 🏆")


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)
    chat_id = update.effective_chat.id
    rows = get_leaderboard(chat_id)
    if not rows:
        await update.message.reply_text("هنوز کسی امتیازی نگرفته! با /quiz شروع کن.")
        return

    text = "🏆 جدول برترین‌ها:\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (username, s) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        text += f"{prefix} {username} — {s} امتیاز\n"

    await update.message.reply_text(text)


async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes taps on the persistent reply-keyboard buttons to the matching command."""
    text = update.message.text
    if text == BTN_QUIZ:
        await quiz(update, context)
    elif text == BTN_SCORE:
        await score(update, context)
    elif text == BTN_LEADERBOARD:
        await leaderboard(update, context)


# ---------- Admin commands ----------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "🔧 پنل ادمین\n\n"
        "/stats - آمار کلی بات\n"
        "/playing - کاربرانی که الان وسط بازی‌ان\n"
        "/addscore <user_id> <amount> - افزودن امتیاز (توی همین چت)\n"
        "/removescore <user_id> <amount> - کم کردن امتیاز (توی همین چت)\n"
        "/block <user_id> - مسدود کردن کاربر\n"
        "/unblock <user_id> - رفع مسدودی کاربر\n"
        "/broadcast <متن پیام> - ارسال پیام همگانی به همه کاربرا\n"
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    s = get_global_stats()
    text = (
        "📊 آمار کلی بات\n\n"
        f"👥 تعداد کل کاربران: {s['total_users']}\n"
        f"❓ کل سوالات جواب‌داده‌شده: {s['total_answered']}\n"
        f"🚫 کاربران مسدود: {s['total_blocked']}\n\n"
        "🏆 پرامتیازترین‌ها (کل بات):\n"
    )
    if s["top_scorers"]:
        medals = ["🥇", "🥈", "🥉"]
        for i, (username, total_score) in enumerate(s["top_scorers"]):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            text += f"{prefix} {username} — {total_score}\n"
    else:
        text += "هنوز کسی امتیازی نگرفته.\n"

    await update.message.reply_text(text)


async def playing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if not ACTIVE_SESSIONS:
        await update.message.reply_text("الان هیچ‌کس وسط بازی نیست.")
        return

    text = "🎮 دوره‌های کوییز فعال الان:\n\n"
    for chat_id, session in ACTIVE_SESSIONS.items():
        text += (
            f"چت {chat_id}: سوال {session['index'] + 1}/{len(session['questions'])} "
            f"— {len(session['participants'])} شرکت‌کننده تا الان\n"
        )
    await update.message.reply_text(text)


async def addscore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await _adjust_score(update, context, sign=1)


async def removescore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await _adjust_score(update, context, sign=-1)


async def _adjust_score(update: Update, context: ContextTypes.DEFAULT_TYPE, sign: int):
    if len(context.args) < 2:
        await update.message.reply_text("فرمت درست: /addscore <user_id> <amount>")
        return
    try:
        target_id = int(context.args[0])
        amount = int(context.args[1]) * sign
    except ValueError:
        await update.message.reply_text("آیدی و مقدار باید عدد باشن.")
        return

    chat_id = update.effective_chat.id
    add_point(target_id, chat_id, username=str(target_id), amount=amount)
    new_score = get_score(target_id, chat_id)
    await update.message.reply_text(f"انجام شد. امتیاز جدید کاربر {target_id} توی این چت: {new_score}")


async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("فرمت درست: /block <user_id>")
        return
    target_id = int(context.args[0])
    set_blocked(target_id, True)
    await update.message.reply_text(f"کاربر {target_id} مسدود شد.")


async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("فرمت درست: /unblock <user_id>")
        return
    target_id = int(context.args[0])
    set_blocked(target_id, False)
    await update.message.reply_text(f"کاربر {target_id} آزاد شد.")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("فرمت درست: /broadcast <متن پیام>")
        return

    message_text = " ".join(context.args)
    chat_ids = get_all_private_chat_ids()

    sent, failed = 0, 0
    for cid in chat_ids:
        try:
            await context.bot.send_message(chat_id=cid, text=f"📢 {message_text}")
            sent += 1
        except Exception as e:
            logger.warning("Broadcast failed for %s: %s", cid, e)
            failed += 1

    await update.message.reply_text(f"پیام همگانی ارسال شد. موفق: {sent} — ناموفق: {failed}")


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "متغیر محیطی BOT_TOKEN تنظیم نشده. توکن بات رو از BotFather بگیر و ست کن."
        )

    init_db()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(
        MessageHandler(
            filters.Regex(f"^({BTN_QUIZ}|{BTN_SCORE}|{BTN_LEADERBOARD})$"),
            menu_button,
        )
    )
    app.add_handler(CallbackQueryHandler(handle_answer))

    # Admin-only commands
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("playing", playing_cmd))
    app.add_handler(CommandHandler("addscore", addscore_cmd))
    app.add_handler(CommandHandler("removescore", removescore_cmd))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
