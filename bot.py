import asyncio
import json
import logging
import os
import random
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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
    conn.commit()
    conn.close()


def add_point(user_id: int, chat_id: int, username: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scores (user_id, chat_id, username, score)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(user_id, chat_id)
        DO UPDATE SET score = score + 1, username = excluded.username
        """,
        (user_id, chat_id, username),
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
# }
ACTIVE_SESSIONS = {}


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋 به بات کوییز خوش اومدی.\n\n"
        "دستورات:\n"
        "/quiz - شروع یه دوره ۵ سوالی (سوالات خودشون پشت‌سرهم میان)\n"
        "/quiz 10 - شروع یه دوره با تعداد دلخواه سوال\n"
        "/score - امتیاز کلی خودت رو ببین\n"
        "/leaderboard - جدول برترین‌ها\n"
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

    correct_index = ACTIVE_QUESTIONS.pop(key)
    chosen_index = int(query.data)
    user = query.from_user
    username = user.username or user.first_name

    session = ACTIVE_SESSIONS.get(chat_id)

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
    user = update.effective_user
    chat_id = update.effective_chat.id
    s = get_score(user.id, chat_id)
    await update.message.reply_text(f"امتیاز تو: {s} 🏆")


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    app.add_handler(CallbackQueryHandler(handle_answer))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
