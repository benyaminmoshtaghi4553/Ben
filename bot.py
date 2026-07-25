import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import uuid
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
DUEL_LENGTH = 5
NEXT_QUESTION_DELAY_SECONDS = 2  # short pause before auto-sending next question

# Telegram numeric user IDs allowed to use admin commands.
ADMIN_IDS = {7906761982}

# ---------- Main menu (persistent reply keyboard) ----------

BTN_QUIZ = "🎯 شروع بازی"
BTN_SCORE = "🏆 امتیاز من"
BTN_LEADERBOARD = "📊 جدول برترین‌ها"
BTN_INVITE = "🎮 دعوت دوست"
BTN_SUGGEST = "📝 پیشنهاد سوال"

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_QUIZ], [BTN_SCORE, BTN_LEADERBOARD], [BTN_INVITE, BTN_SUGGEST]],
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
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            question TEXT,
            option1 TEXT,
            option2 TEXT,
            option3 TEXT,
            option4 TEXT,
            correct_index INTEGER,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            question_text TEXT,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT,
            option1 TEXT,
            option2 TEXT,
            option3 TEXT,
            option4 TEXT,
            correct_index INTEGER
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


def get_user_by_username(username: str):
    """Returns (user_id, private_chat_id) for a given username, or None."""
    username = username.lstrip("@")
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT user_id, private_chat_id FROM users WHERE LOWER(username) = LOWER(?)",
        (username,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_private_chat_id(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT private_chat_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


# ---------- Suggestions & reports ----------

def add_suggestion(user_id: int, username: str, question: str, options: list, correct_index: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO suggestions (user_id, username, question, option1, option2, option3, option4, correct_index, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, username, question, options[0], options[1], options[2], options[3],
         correct_index, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    suggestion_id = cur.lastrowid
    conn.close()
    return suggestion_id


def get_suggestion(suggestion_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,))
    row = cur.fetchone()
    conn.close()
    return row


def set_suggestion_status(suggestion_id: int, status: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE suggestions SET status = ? WHERE id = ?", (status, suggestion_id))
    conn.commit()
    conn.close()


def add_custom_question(question: str, options: list, correct_index: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO custom_questions (question, option1, option2, option3, option4, correct_index)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (question, options[0], options[1], options[2], options[3], correct_index),
    )
    conn.commit()
    conn.close()


def load_custom_questions():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT question, option1, option2, option3, option4, correct_index FROM custom_questions")
    rows = cur.fetchall()
    conn.close()
    result = []
    for q, o1, o2, o3, o4, correct in rows:
        result.append({"question": q, "options": [o1, o2, o3, o4], "correct": correct})
    return result


def add_report(user_id: int, username: str, question_text: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reports (user_id, username, question_text, created_at) VALUES (?, ?, ?, ?)",
        (user_id, username, question_text, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


# ---------- Questions ----------

def load_questions():
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


QUESTIONS = load_questions()

# Tracks the correct answer index for each currently-active question message.
# Keyed by (chat_id, message_id) -> correct_option_index
ACTIVE_QUESTIONS = {}

# Keeps the question text available for reporting even briefly after it's answered.
# Keyed by (chat_id, message_id) -> question text
QUESTION_TEXT_BY_MESSAGE = {}

# Tracks an in-progress multi-question session per chat.
ACTIVE_SESSIONS = {}

# Pending 1-on-1 game invites. Keyed by target_user_id -> inviter_user_id.
PENDING_INVITES = {}

# User IDs who tapped the "invite friend" button and are now expected to
# type a username as their next message.
AWAITING_INVITE_USERNAME = set()

# Step-by-step "suggest a question" flow.
# Keyed by user_id -> {"step": int, "data": {...}}
SUGGESTION_STATE = {}

# Once all 5 fields are collected, waits for the user to tap which option is correct.
# Keyed by user_id -> {"question":..., "option1":..., ...}
PENDING_SUGGESTIONS = {}

# Active duels. Keyed by duel_id -> {
#   "questions": [...],
#   "players": {user_id: {"index": 0, "correct": 0, "username": str, "chat_id": int}},
# }
DUELS = {}


# ---------- Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)
    await update.message.reply_text(
        "سلام! 👋 به بات کوییز خوش اومدی.\n\n"
        "از دکمه‌های پایین صفحه استفاده کن، یا این دستورات رو بنویس:\n"
        "/quiz - شروع یه دوره ۵ سوالی\n"
        "/quiz 10 - دوره با تعداد دلخواه سوال\n"
        "/score - امتیاز کلی خودت\n"
        "/leaderboard - جدول برترین‌ها\n"
        "/suggest - پیشنهاد یه سوال جدید\n"
        "/invite <یوزرنیم> - دعوت یه دوست به بازی دونفره\n",
        reply_markup=MAIN_MENU,
    )


async def send_question(chat_id: int, context: ContextTypes.DEFAULT_TYPE, q: dict, q_number: int, total: int):
    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"ans_{i}")]
        for i, opt in enumerate(q["options"])
    ]

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=f"❓ سوال {q_number}/{total}\n\n{q['question']}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

    ACTIVE_QUESTIONS[(chat_id, msg.message_id)] = q["correct"]
    QUESTION_TEXT_BY_MESSAGE[(chat_id, msg.message_id)] = q["question"]

    # Add the report button as a second step so we can reference the message_id.
    buttons_with_report = buttons + [
        [InlineKeyboardButton("🚩 گزارش این سوال", callback_data=f"report_{chat_id}_{msg.message_id}")]
    ]
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=msg.message_id,
            reply_markup=InlineKeyboardMarkup(buttons_with_report),
        )
    except Exception as e:
        logger.warning("Could not attach report button: %s", e)


async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)

    if is_blocked(update.effective_user.id):
        await update.message.reply_text("متاسفانه دسترسی تو به بات مسدود شده.")
        return

    chat_id = update.effective_chat.id

    if chat_id in ACTIVE_SESSIONS:
        await update.message.reply_text("یه دوره کوییز همین الان در حال اجراست، اول اونو تموم کن! 🙂")
        return

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
    text = update.message.text
    if text == BTN_QUIZ:
        await quiz(update, context)
    elif text == BTN_SCORE:
        await score(update, context)
    elif text == BTN_LEADERBOARD:
        await leaderboard(update, context)
    elif text == BTN_INVITE:
        AWAITING_INVITE_USERNAME.add(update.effective_user.id)
        await update.message.reply_text("یوزرنیم دوستت رو بفرست (بدون @) تا دعوتش کنم به یه بازی دونفره.")
    elif text == BTN_SUGGEST:
        await suggest_start(update, context)


async def handle_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback router for free-text replies that belong to a multi-step flow
    (question suggestion or invite-friend), checked in priority order."""
    user_id = update.effective_user.id

    if user_id in SUGGESTION_STATE:
        await handle_suggestion_step(update, context)
        return

    if user_id in AWAITING_INVITE_USERNAME:
        AWAITING_INVITE_USERNAME.discard(user_id)
        await process_invite(update, context, update.message.text)
        return


# ---------- Suggest a question ----------

async def suggest_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kicks off the step-by-step question-suggestion flow (from /suggest or the button)."""
    track_user(update.effective_user, update.effective_chat)
    user_id = update.effective_user.id
    SUGGESTION_STATE[user_id] = {"step": 0, "data": {}}
    await update.message.reply_text(
        "بریم یه سوال بسازیم! 📝 (هر وقت خواستی منصرف بشی بنویس /cancel)\n\n"
        "اول: متن سوالت رو بنویس."
    )


async def cancel_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in SUGGESTION_STATE:
        del SUGGESTION_STATE[user_id]
        await update.message.reply_text("لغو شد.")


OPTIONS_PROMPT = (
    "حالا هر ۴ گزینه رو با هم، هر کدوم تو یه خط، اینجوری بفرست:\n\n"
    "الف) گزینه اول\n"
    "ب) گزینه دوم\n"
    "ج) گزینه سوم\n"
    "د) گزینه چهارم"
)

OPTION_PREFIX_RE = re.compile(r"^\s*(الف|ب|ج|د)\s*[\)\.\-:]\s*")


def parse_option_lines(text: str):
    """Parses 4 lines like 'الف) گزینه' into a plain list of 4 option strings, or None if invalid."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) != 4:
        return None
    cleaned = []
    for line in lines:
        match = OPTION_PREFIX_RE.match(line)
        cleaned.append(line[match.end():].strip() if match else line)
    if any(not c for c in cleaned):
        return None
    return cleaned


async def handle_suggestion_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = SUGGESTION_STATE[user_id]
    step = state["step"]
    text = update.message.text.strip()

    if step == 0:
        state["data"]["question"] = text
        state["step"] = 1
        await update.message.reply_text(OPTIONS_PROMPT)
        return

    # step == 1: expecting all 4 options at once
    options = parse_option_lines(text)
    if options is None:
        await update.message.reply_text(
            "درست تشخیص ندادم 🙁 لطفاً دقیقاً ۴ خط بفرست، هر خط یه گزینه:\n\n" + OPTIONS_PROMPT
        )
        return

    data = state["data"]
    data["options"] = options
    del SUGGESTION_STATE[user_id]
    PENDING_SUGGESTIONS[user_id] = data

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"الف) {options[0]}", callback_data="suggcorrect_0")],
        [InlineKeyboardButton(f"ب) {options[1]}", callback_data="suggcorrect_1")],
        [InlineKeyboardButton(f"ج) {options[2]}", callback_data="suggcorrect_2")],
        [InlineKeyboardButton(f"د) {options[3]}", callback_data="suggcorrect_3")],
    ])
    await update.message.reply_text("عالی! حالا بگو کدوم گزینه جواب درسته:", reply_markup=buttons)


async def handle_suggestion_correct_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = PENDING_SUGGESTIONS.get(user.id)

    if not data:
        await query.answer("این پیشنهاد منقضی شده، دوباره از /suggest شروع کن.", show_alert=True)
        return

    del PENDING_SUGGESTIONS[user.id]
    correct_index = int(query.data.rsplit("_", 1)[1])

    username = user.username or user.first_name
    options = data["options"]
    suggestion_id = add_suggestion(user.id, username, data["question"], options, correct_index)

    await query.answer("ثبت شد!")
    await query.edit_message_text("ممنون! سوالت برای بررسی ادمین ارسال شد. ✅")

    admin_text = (
        f"📝 پیشنهاد سوال جدید (#{suggestion_id})\n"
        f"از طرف: {username}\n\n"
        f"❓ {data['question']}\n"
        f"1) {options[0]}\n2) {options[1]}\n3) {options[2]}\n4) {options[3]}\n\n"
        f"✅ جواب درست: گزینه {correct_index + 1}"
    )
    admin_buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تایید", callback_data=f"suggapprove_{suggestion_id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"suggreject_{suggestion_id}"),
        ]
    ])
    for admin_id in ADMIN_IDS:
        admin_chat_id = get_private_chat_id(admin_id)
        if admin_chat_id:
            try:
                await context.bot.send_message(chat_id=admin_chat_id, text=admin_text, reply_markup=admin_buttons)
            except Exception as e:
                logger.warning("Could not notify admin %s: %s", admin_id, e)


async def handle_suggestion_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("این دکمه فقط برای ادمینه.", show_alert=True)
        return

    approve = query.data.startswith("suggapprove_")
    suggestion_id = int(query.data.rsplit("_", 1)[1])
    row = get_suggestion(suggestion_id)

    if not row:
        await query.answer("این پیشنهاد پیدا نشد (شاید قبلاً بررسی شده).", show_alert=True)
        return

    (_id, s_user_id, s_username, s_question, o1, o2, o3, o4, s_correct, s_status, _created) = row

    if s_status != "pending":
        await query.answer("این پیشنهاد قبلاً بررسی شده.", show_alert=True)
        return

    if approve:
        set_suggestion_status(suggestion_id, "approved")
        add_custom_question(s_question, [o1, o2, o3, o4], s_correct)
        QUESTIONS.append({"question": s_question, "options": [o1, o2, o3, o4], "correct": s_correct})
        await query.answer("تایید شد و به بانک سوالات اضافه شد ✅")
        await query.edit_message_text(query.message.text + "\n\n✅ تایید شد.")
        notify_text = "🎉 سوالی که پیشنهاد داده بودی تایید شد و الان توی بات فعاله!"
    else:
        set_suggestion_status(suggestion_id, "rejected")
        await query.answer("رد شد.")
        await query.edit_message_text(query.message.text + "\n\n❌ رد شد.")
        notify_text = "سوالی که پیشنهاد داده بودی این‌بار تایید نشد. ممنون بابت مشارکتت 🙏"

    suggester_chat_id = get_private_chat_id(s_user_id)
    if suggester_chat_id:
        try:
            await context.bot.send_message(chat_id=suggester_chat_id, text=notify_text)
        except Exception as e:
            logger.warning("Could not notify suggester %s: %s", s_user_id, e)


# ---------- Report a question ----------

async def handle_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, chat_id_str, message_id_str = query.data.split("_")
    chat_id, message_id = int(chat_id_str), int(message_id_str)

    question_text = QUESTION_TEXT_BY_MESSAGE.get((chat_id, message_id), "متن سوال پیدا نشد")
    user = query.from_user
    username = user.username or user.first_name
    add_report(user.id, username, question_text)

    await query.answer("گزارش شما برای ادمین ارسال شد. ممنون! 🙏", show_alert=True)

    report_text = f"🚩 گزارش سوال\nاز طرف: {username}\n\n❓ {question_text}"
    for admin_id in ADMIN_IDS:
        admin_chat_id = get_private_chat_id(admin_id)
        if admin_chat_id:
            try:
                await context.bot.send_message(chat_id=admin_chat_id, text=report_text)
            except Exception as e:
                logger.warning("Could not notify admin %s about report: %s", admin_id, e)


# ---------- Direct game invite (1v1 duel) ----------

async def invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)

    if not context.args:
        await update.message.reply_text("فرمت درست: /invite یوزرنیم_دوستت  (مثلاً /invite ali_123)")
        return

    await process_invite(update, context, context.args[0])


async def process_invite(update: Update, context: ContextTypes.DEFAULT_TYPE, target_username: str):
    inviter = update.effective_user
    target_username = target_username.strip()
    target = get_user_by_username(target_username)

    if not target:
        await update.message.reply_text("این کاربر هنوز با بات آشنا نشده (باید حداقل یه بار /start بزنه).")
        return

    target_id, target_chat_id = target

    if target_id == inviter.id:
        await update.message.reply_text("نمی‌تونی خودتو دعوت کنی! 😄")
        return

    if not target_chat_id:
        await update.message.reply_text("این کاربر باید اول توی چت خصوصی با بات /start بزنه تا بتونی دعوتش کنی.")
        return

    PENDING_INVITES[target_id] = inviter.id

    inviter_username = inviter.username or inviter.first_name
    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ قبول می‌کنم", callback_data=f"inviteacc_{inviter.id}"),
            InlineKeyboardButton("❌ نه، ممنون", callback_data=f"invitedec_{inviter.id}"),
        ]
    ])
    try:
        await context.bot.send_message(
            chat_id=target_chat_id,
            text=f"🎮 {inviter_username} می‌خواد باهات یه بازی کوییز {DUEL_LENGTH} سوالی انجام بده. قبول می‌کنی؟",
            reply_markup=buttons,
        )
        await update.message.reply_text("دعوت‌نامه فرستاده شد! منتظر جواب دوستت باش.")
    except Exception as e:
        logger.warning("Could not send invite: %s", e)
        await update.message.reply_text("نتونستم پیام دعوت رو بفرستم، شاید بات رو بلاک کرده.")


async def handle_invite_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    accept = query.data.startswith("inviteacc_")
    inviter_id = int(query.data.rsplit("_", 1)[1])
    target_user = query.from_user

    if PENDING_INVITES.get(target_user.id) != inviter_id:
        await query.answer("این دعوت دیگه معتبر نیست.", show_alert=True)
        return

    del PENDING_INVITES[target_user.id]

    inviter_chat_id = get_private_chat_id(inviter_id)

    if not accept:
        await query.answer("دعوت رد شد.")
        await query.edit_message_text(query.message.text + "\n\n❌ رد شد.")
        if inviter_chat_id:
            await context.bot.send_message(chat_id=inviter_chat_id, text="دوستت دعوتت رو رد کرد 🙁")
        return

    await query.answer("بازی شروع شد! 🎉")
    await query.edit_message_text(query.message.text + "\n\n✅ قبول شد! بازی شروع میشه...")

    if not inviter_chat_id:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="متاسفانه نتونستم بازی رو شروع کنم چون اطلاعات دعوت‌کننده در دسترس نیست.",
        )
        return

    target_username = target_user.username or target_user.first_name
    inviter_username = None  # filled in below once we track_user for them via DB lookup fallback

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE user_id = ?", (inviter_id,))
    row = cur.fetchone()
    conn.close()
    inviter_username = row[0] if row else str(inviter_id)

    duel_id = uuid.uuid4().hex[:8]
    duel_questions = random.sample(QUESTIONS, min(DUEL_LENGTH, len(QUESTIONS)))

    DUELS[duel_id] = {
        "questions": duel_questions,
        "players": {
            inviter_id: {"index": 0, "correct": 0, "username": inviter_username, "chat_id": inviter_chat_id},
            target_user.id: {"index": 0, "correct": 0, "username": target_username, "chat_id": query.message.chat_id},
        },
    }

    await context.bot.send_message(chat_id=inviter_chat_id, text=f"🎮 بازی با {target_username} شروع شد! سوال اول:")
    await context.bot.send_message(chat_id=query.message.chat_id, text=f"🎮 بازی با {inviter_username} شروع شد! سوال اول:")

    await send_duel_question(duel_id, inviter_id, context)
    await send_duel_question(duel_id, target_user.id, context)


async def send_duel_question(duel_id: str, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    duel = DUELS.get(duel_id)
    if not duel:
        return
    player = duel["players"][user_id]
    idx = player["index"]
    total = len(duel["questions"])

    if idx >= total:
        return

    q = duel["questions"][idx]
    buttons = [
        [InlineKeyboardButton(opt, callback_data=f"duelans_{duel_id}_{i}")]
        for i, opt in enumerate(q["options"])
    ]
    await context.bot.send_message(
        chat_id=player["chat_id"],
        text=f"❓ سوال {idx + 1}/{total}\n\n{q['question']}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_duel_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    body = query.data[len("duelans_"):]
    duel_id, chosen_str = body.rsplit("_", 1)
    chosen_index = int(chosen_str)

    duel = DUELS.get(duel_id)
    if not duel:
        await query.answer("این بازی دیگه فعال نیست.", show_alert=True)
        return

    user = query.from_user
    player = duel["players"].get(user.id)
    if not player:
        await query.answer("این بازی مال تو نیست.", show_alert=True)
        return

    idx = player["index"]
    if idx >= len(duel["questions"]):
        await query.answer("قبلاً به همه سوالات جواب دادی.", show_alert=True)
        return

    correct_index = duel["questions"][idx]["correct"]

    if chosen_index == correct_index:
        player["correct"] += 1
        add_point(user.id, player["chat_id"], player["username"])
        await query.answer("✅ درست بود!")
    else:
        await query.answer("❌ اشتباه بود!")

    player["index"] += 1

    try:
        await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
    except Exception as e:
        logger.warning("Could not delete duel question message: %s", e)

    if player["index"] < len(duel["questions"]):
        await asyncio.sleep(NEXT_QUESTION_DELAY_SECONDS)
        await send_duel_question(duel_id, user.id, context)
    else:
        await context.bot.send_message(chat_id=player["chat_id"], text="منتظر بمون تا حریفت هم تموم کنه... ⏳")

    # If both players finished, announce the result.
    if all(p["index"] >= len(duel["questions"]) for p in duel["players"].values()):
        players = list(duel["players"].items())
        (id_a, p_a), (id_b, p_b) = players[0], players[1]

        if p_a["correct"] > p_b["correct"]:
            result_a = f"🏆 بردی! ({p_a['correct']} به {p_b['correct']})"
            result_b = f"😢 باختی. ({p_b['correct']} به {p_a['correct']})"
        elif p_b["correct"] > p_a["correct"]:
            result_a = f"😢 باختی. ({p_a['correct']} به {p_b['correct']})"
            result_b = f"🏆 بردی! ({p_b['correct']} به {p_a['correct']})"
        else:
            result_a = f"🤝 مساوی شدید! ({p_a['correct']} به {p_b['correct']})"
            result_b = f"🤝 مساوی شدید! ({p_b['correct']} به {p_a['correct']})"

        await context.bot.send_message(chat_id=p_a["chat_id"], text=f"🏁 نتیجه بازی با {p_b['username']}:\n{result_a}")
        await context.bot.send_message(chat_id=p_b["chat_id"], text=f"🏁 نتیجه بازی با {p_a['username']}:\n{result_b}")

        del DUELS[duel_id]


# ---------- Normal quiz-session answer ----------

async def handle_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    chosen_index = int(query.data[len("ans_"):])
    username = user.username or user.first_name

    session = ACTIVE_SESSIONS.get(chat_id)
    increment_questions_answered(user.id)

    if chosen_index == correct_index:
        add_point(user.id, chat_id, username)
        await query.answer("✅ درست بود!")
        if session is not None:
            session["correct_count"] += 1
    else:
        await query.answer("❌ اشتباه بود!")

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning("Could not delete question message %s in chat %s: %s", message_id, chat_id, e)

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


# ---------- Callback dispatcher ----------

async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data.startswith("ans_"):
        await handle_quiz_answer(update, context)
    elif data.startswith("report_"):
        await handle_report(update, context)
    elif data.startswith("suggcorrect_"):
        await handle_suggestion_correct_choice(update, context)
    elif data.startswith("suggapprove_") or data.startswith("suggreject_"):
        await handle_suggestion_review(update, context)
    elif data.startswith("duelans_"):
        await handle_duel_answer(update, context)
    elif data.startswith("inviteacc_") or data.startswith("invitedec_"):
        await handle_invite_response(update, context)


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

    if not ACTIVE_SESSIONS and not DUELS:
        await update.message.reply_text("الان هیچ‌کس وسط بازی نیست.")
        return

    text = ""
    if ACTIVE_SESSIONS:
        text += "🎮 دوره‌های کوییز فعال:\n\n"
        for chat_id, session in ACTIVE_SESSIONS.items():
            text += (
                f"چت {chat_id}: سوال {session['index'] + 1}/{len(session['questions'])} "
                f"— {len(session['participants'])} شرکت‌کننده تا الان\n"
            )
    if DUELS:
        text += "\n⚔️ بازی‌های دونفره فعال:\n\n"
        for duel_id, duel in DUELS.items():
            names = " و ".join(p["username"] for p in duel["players"].values())
            text += f"{names}\n"

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
    QUESTIONS.extend(load_custom_questions())

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("suggest", suggest_start))
    app.add_handler(CommandHandler("cancel", cancel_suggestion))
    app.add_handler(CommandHandler("invite", invite_cmd))
    app.add_handler(
        MessageHandler(
            filters.Regex(f"^({BTN_QUIZ}|{BTN_SCORE}|{BTN_LEADERBOARD}|{BTN_INVITE}|{BTN_SUGGEST})$"),
            menu_button,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))
    app.add_handler(CallbackQueryHandler(on_callback_query))

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
