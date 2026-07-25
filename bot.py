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


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 👋 به بات کوییز خوش اومدی.\n\n"
        "دستورات:\n"
        "/quiz - شروع یه سوال جدید\n"
        "/score - امتیاز خودت رو ببین\n"
        "/leaderboard - جدول برترین‌ها\n"
    )


async def send_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    q = random.choice(QUESTIONS)

    buttons = [
        [InlineKeyboardButton(opt, callback_data=str(i))]
        for i, opt in enumerate(q["options"])
    ]
    markup = InlineKeyboardMarkup(buttons)

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"❓ {q['question']}",
        reply_markup=markup,
    )

    ACTIVE_QUESTIONS[(chat_id, msg.message_id)] = q["correct"]


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

    if chosen_index == correct_index:
        add_point(user.id, chat_id, username)
        new_score = get_score(user.id, chat_id)
        await query.answer("✅ آفرین، درست بود!")
        await query.edit_message_text(
            f"{query.message.text}\n\n✅ {username} درست جواب داد! (امتیاز: {new_score})"
        )
    else:
        await query.answer("❌ اشتباه بود!")
        await query.edit_message_text(
            f"{query.message.text}\n\n❌ {username} اشتباه جواب داد."
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
    app.add_handler(CommandHandler("quiz", send_quiz))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CallbackQueryHandler(handle_answer))

    logger.info("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
