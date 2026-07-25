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
BTN_FIND_OPPONENT = "🔎 پیدا کردن حریف تصادفی"

MAIN_MENU = ReplyKeyboardMarkup(
    [[BTN_QUIZ], [BTN_SCORE, BTN_LEADERBOARD], [BTN_INVITE, BTN_SUGGEST], [BTN_FIND_OPPONENT]],
    resize_keyboard=True,
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---------- Database ----------

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    # If an old per-chat scores table exists (from before the scoring format changed),
    # migrate everyone's total into the new global-score table, then replace it.
    cur.execute("PRAGMA table_info(scores)")
    existing_columns = {row[1] for row in cur.fetchall()}
    if existing_columns and "chat_id" in existing_columns:
        cur.execute("SELECT user_id, username, SUM(score) FROM scores GROUP BY user_id")
        migrated_rows = cur.fetchall()
        cur.execute("ALTER TABLE scores RENAME TO scores_old_per_chat")
        conn.commit()
    else:
        migrated_rows = None

    # One global score per user (not per chat) — simpler mental model.
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS scores (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            score INTEGER DEFAULT 0
        )
        """
    )

    if migrated_rows:
        cur.executemany(
            "INSERT OR REPLACE INTO scores (user_id, username, score) VALUES (?, ?, ?)",
            migrated_rows,
        )
        conn.commit()
        logger.info("Migrated %d users' scores from the old per-chat schema.", len(migrated_rows))

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


def add_point(user_id: int, username: str, amount: int = 1):
    """Adds (or subtracts, with a negative amount) to a user's single global score."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO scores (user_id, username, score)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET score = score + excluded.score, username = excluded.username
        """,
        (user_id, username, amount),
    )
    conn.commit()
    conn.close()


def get_score(user_id: int) -> int:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT score FROM scores WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def get_leaderboard(limit: int = 10):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT username, score FROM scores ORDER BY score DESC LIMIT ?", (limit,))
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

    cur.execute("SELECT username, score FROM scores ORDER BY score DESC LIMIT 5")
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


def get_user_by_id(user_id: int):
    """Returns (user_id, private_chat_id) for a given numeric Telegram ID, or None."""
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, private_chat_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def get_user_by_identifier(identifier: str):
    """Accepts either a @username or a numeric Telegram ID and looks it up either way."""
    identifier = identifier.strip().lstrip("@")
    if identifier.isdigit():
        return get_user_by_id(int(identifier))
    return get_user_by_username(identifier)


def get_username_for(user_id: int) -> str:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else str(user_id)


def get_private_chat_id(user_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT private_chat_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


# ---------- Suggestions ----------

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


# ---------- Questions ----------

def load_questions():
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


QUESTIONS = load_questions()

# Tracks the correct answer index for each currently-active question message.
# Keyed by (chat_id, message_id) -> correct_option_index
ACTIVE_QUESTIONS = {}

# Tracks an in-progress multi-question session per chat.
ACTIVE_SESSIONS = {}

# Pending 1-on-1 game invites. Keyed by target_user_id -> inviter_user_id.
PENDING_INVITES = {}

# User IDs who tapped the "invite friend" button and are now expected to
# type a username as their next message.
AWAITING_INVITE_USERNAME = set()

# Step-by-step "suggest a question" flow. Keyed by user_id -> {"step": int, "data": {...}}
SUGGESTION_STATE = {}

# Once all fields are collected, waits for the user to tap which option is correct.
PENDING_SUGGESTIONS = {}

# Step-by-step admin actions (add/remove score, block, unblock, broadcast).
# Keyed by user_id -> {"action": str, "step": int, "data": {...}}
ADMIN_STATE = {}

# Active duels. Keyed by duel_id -> {
#   "questions": [...],
#   "players": {user_id: {"index": 0, "correct": 0, "username": str, "chat_id": int}},
# }
DUELS = {}

# Random-opponent matchmaking queue. List of (user_id, private_chat_id, username).
MATCHMAKING_QUEUE = []


# ---------- Basic handlers ----------

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
        "/invite <یوزرنیم> - دعوت یه دوست به بازی دونفره\n"
        "/findopponent - جستجوی حریف تصادفی\n",
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
    is_group = update.effective_chat.type != "private"

    ACTIVE_SESSIONS[chat_id] = {
        "questions": session_questions,
        "index": 0,
        "correct_count": 0,
        "participants": set(),
        "participant_scores": {},  # user_id -> {"username": str, "correct": int}
    }

    if is_group:
        await update.message.reply_text(
            f"🎯 یه دوره {length} سوالی برای کل گروه شروع شد! هرکی زودتر و درست‌تر جواب بده، امتیاز می‌گیره. آماده باشید..."
        )
    else:
        await update.message.reply_text(f"🎯 یه دوره {length} سوالی شروع شد! آماده باش...")
    await send_question(chat_id, context, session_questions[0], 1, length)


async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)
    s = get_score(update.effective_user.id)
    await update.message.reply_text(f"امتیاز کلی تو: {s} 🏆")


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)
    rows = get_leaderboard()
    if not rows:
        await update.message.reply_text("هنوز کسی امتیازی نگرفته! با /quiz شروع کن.")
        return

    text = "🏆 جدول برترین‌ها (کل بات):\n\n"
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
    elif text == BTN_FIND_OPPONENT:
        await find_opponent_cmd(update, context)


async def handle_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback router for free-text replies belonging to a multi-step flow, in priority order."""
    user_id = update.effective_user.id

    if user_id in ADMIN_STATE:
        await handle_admin_text_step(update, context)
        return

    if user_id in SUGGESTION_STATE:
        await handle_suggestion_step(update, context)
        return

    if user_id in AWAITING_INVITE_USERNAME:
        AWAITING_INVITE_USERNAME.discard(user_id)
        await process_invite(update, context, update.message.text)
        return


# ---------- Suggest a question (step-by-step, no special format needed) ----------

async def suggest_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)
    user_id = update.effective_user.id
    SUGGESTION_STATE[user_id] = {"step": 0, "data": {}}
    await update.message.reply_text(
        "بریم یه سوال بسازیم! 📝 (هر وقت خواستی منصرف بشی بنویس /cancel)\n\n"
        "اول: متن سوالت رو بنویس."
    )


async def cancel_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cancelled = False
    if user_id in SUGGESTION_STATE:
        del SUGGESTION_STATE[user_id]
        cancelled = True
    if user_id in ADMIN_STATE:
        del ADMIN_STATE[user_id]
        cancelled = True
    if user_id in AWAITING_INVITE_USERNAME:
        AWAITING_INVITE_USERNAME.discard(user_id)
        cancelled = True
    await update.message.reply_text("لغو شد." if cancelled else "چیزی برای لغو کردن نبود.")


OPTIONS_PROMPT = (
    "حالا هر ۴ گزینه رو با هم، هر کدوم تو یه خط، اینجوری بفرست:\n\n"
    "الف) گزینه اول\n"
    "ب) گزینه دوم\n"
    "ج) گزینه سوم\n"
    "د) گزینه چهارم"
)

OPTION_PREFIX_RE = re.compile(r"^\s*(الف|ب|ج|د)\s*[\)\.\-:]\s*")


def parse_option_lines(text: str):
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


# ---------- Direct game invite (1v1 duel) ----------

async def invite_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)

    if not context.args:
        await update.message.reply_text(
            "فرمت درست: /invite یوزرنیم_دوستت  (یا آیدی عددیش)\nمثال: /invite ali_123"
        )
        return

    await process_invite(update, context, context.args[0])


async def process_invite(update: Update, context: ContextTypes.DEFAULT_TYPE, target_identifier: str):
    inviter = update.effective_user
    target = get_user_by_identifier(target_identifier)

    if not target:
        await update.message.reply_text(
            "همچین کاربری پیدا نشد. مطمئن شو یوزرنیم یا آیدی رو درست فرستادی و اون شخص حداقل یه بار با بات صحبت کرده."
        )
        return

    target_id, target_chat_id = target

    if target_id == inviter.id:
        await update.message.reply_text("نمی‌تونی خودتو دعوت کنی! 😄")
        return

    if not target_chat_id:
        await update.message.reply_text(
            "این کاربر رو پیدا کردم، ولی چون قبلاً فقط توی یه گروه با بات تعامل داشته (نه توی چت خصوصی)، "
            "نمی‌تونم براش پیام خصوصی بفرستم. ازش بخواه یه بار توی چت خصوصی بات /start بزنه."
        )
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
    inviter_username = get_username_for(inviter_id)

    await start_duel(
        inviter_id, inviter_chat_id, inviter_username,
        target_user.id, query.message.chat_id, target_username,
        context,
    )


async def start_duel(id_a: int, chat_a: int, username_a: str, id_b: int, chat_b: int, username_b: str, context: ContextTypes.DEFAULT_TYPE):
    """Creates a fresh duel between two players and sends each their first question."""
    duel_id = uuid.uuid4().hex[:8]
    duel_questions = random.sample(QUESTIONS, min(DUEL_LENGTH, len(QUESTIONS)))

    DUELS[duel_id] = {
        "questions": duel_questions,
        "players": {
            id_a: {"index": 0, "correct": 0, "username": username_a, "chat_id": chat_a},
            id_b: {"index": 0, "correct": 0, "username": username_b, "chat_id": chat_b},
        },
    }

    await context.bot.send_message(chat_id=chat_a, text=f"🎮 بازی با {username_b} شروع شد! سوال اول:")
    await context.bot.send_message(chat_id=chat_b, text=f"🎮 بازی با {username_a} شروع شد! سوال اول:")

    await send_duel_question(duel_id, id_a, context)
    await send_duel_question(duel_id, id_b, context)


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
        add_point(user.id, player["username"])
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


# ---------- Random-opponent matchmaking ----------

async def find_opponent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_user(update.effective_user, update.effective_chat)
    user = update.effective_user
    chat = update.effective_chat

    if chat.type != "private":
        await update.message.reply_text("این قابلیت فقط توی چت خصوصی با بات کار می‌کنه، نه توی گروه.")
        return

    if any(u[0] == user.id for u in MATCHMAKING_QUEUE):
        await update.message.reply_text("قبلاً تو صف جستجو هستی! صبر کن حریف پیدا بشه، یا برای لغو بنویس /cancelsearch")
        return

    username = user.username or user.first_name

    if MATCHMAKING_QUEUE:
        opponent_id, opponent_chat_id, opponent_username = MATCHMAKING_QUEUE.pop(0)
        await start_duel(opponent_id, opponent_chat_id, opponent_username, user.id, chat.id, username, context)
    else:
        MATCHMAKING_QUEUE.append((user.id, chat.id, username))
        await update.message.reply_text(
            "🔎 دنبال حریف می‌گردم... به محض پیدا شدن یه نفر خبرت می‌کنم.\nبرای لغو جستجو: /cancelsearch"
        )


async def cancel_search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    before = len(MATCHMAKING_QUEUE)
    MATCHMAKING_QUEUE[:] = [u for u in MATCHMAKING_QUEUE if u[0] != user_id]
    if len(MATCHMAKING_QUEUE) < before:
        await update.message.reply_text("جستجو لغو شد.")
    else:
        await update.message.reply_text("تو صف جستجو نبودی.")


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
        add_point(user.id, username)
        await query.answer("✅ درست بود!")
        if session is not None:
            session["correct_count"] += 1
            p_score = session["participant_scores"].setdefault(user.id, {"username": username, "correct": 0})
            p_score["correct"] += 1
            p_score["username"] = username
    else:
        await query.answer("❌ اشتباه بود!")
        if session is not None:
            session["participant_scores"].setdefault(user.id, {"username": username, "correct": 0})

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
            participant_scores = session["participant_scores"]
            del ACTIVE_SESSIONS[chat_id]

            text = f"🏁 دوره تموم شد!\nاز {total} سوال، {correct_count} تا درست جواب داده شد.\n"

            if len(participant_scores) > 1:
                text += "\n📊 نتیجه هر نفر:\n"
                ranked = sorted(participant_scores.values(), key=lambda p: p["correct"], reverse=True)
                medals = ["🥇", "🥈", "🥉"]
                for i, p in enumerate(ranked):
                    prefix = medals[i] if i < 3 else f"{i + 1}."
                    text += f"{prefix} {p['username']} — {p['correct']} درست\n"

            text += "\nبرای شروع دوباره: /quiz"
            await context.bot.send_message(chat_id=chat_id, text=text)


# ---------- Callback dispatcher ----------

async def on_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    if data.startswith("ans_"):
        await handle_quiz_answer(update, context)
    elif data.startswith("suggcorrect_"):
        await handle_suggestion_correct_choice(update, context)
    elif data.startswith("suggapprove_") or data.startswith("suggreject_"):
        await handle_suggestion_review(update, context)
    elif data.startswith("duelans_"):
        await handle_duel_answer(update, context)
    elif data.startswith("inviteacc_") or data.startswith("invitedec_"):
        await handle_invite_response(update, context)
    elif data.startswith("adm_"):
        await handle_admin_button(update, context)


# ---------- Admin panel ----------

ADM_STATS = "adm_stats"
ADM_PLAYING = "adm_playing"
ADM_ADDSCORE = "adm_addscore"
ADM_REMOVESCORE = "adm_removescore"
ADM_BLOCK = "adm_block"
ADM_UNBLOCK = "adm_unblock"
ADM_BROADCAST = "adm_broadcast"


def admin_menu_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار کلی", callback_data=ADM_STATS)],
        [InlineKeyboardButton("🎮 بازی‌های فعال الان", callback_data=ADM_PLAYING)],
        [
            InlineKeyboardButton("➕ افزودن امتیاز", callback_data=ADM_ADDSCORE),
            InlineKeyboardButton("➖ کم کردن امتیاز", callback_data=ADM_REMOVESCORE),
        ],
        [
            InlineKeyboardButton("🚫 مسدود کردن", callback_data=ADM_BLOCK),
            InlineKeyboardButton("✅ رفع مسدودی", callback_data=ADM_UNBLOCK),
        ],
        [InlineKeyboardButton("📢 پیام همگانی", callback_data=ADM_BROADCAST)],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔧 پنل ادمین — یکی رو انتخاب کن:", reply_markup=admin_menu_markup())


def format_stats_text() -> str:
    s = get_global_stats()
    text = (
        "📊 آمار کلی بات\n\n"
        f"👥 تعداد کل کاربران: {s['total_users']}\n"
        f"❓ کل سوالات جواب‌داده‌شده: {s['total_answered']}\n"
        f"🚫 کاربران مسدود: {s['total_blocked']}\n\n"
        "🏆 پرامتیازترین‌ها:\n"
    )
    if s["top_scorers"]:
        medals = ["🥇", "🥈", "🥉"]
        for i, (username, total_score) in enumerate(s["top_scorers"]):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            text += f"{prefix} {username} — {total_score}\n"
    else:
        text += "هنوز کسی امتیازی نگرفته.\n"
    return text


def format_playing_text() -> str:
    if not ACTIVE_SESSIONS and not DUELS:
        return "الان هیچ‌کس وسط بازی نیست."

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
        for duel in DUELS.values():
            names = " و ".join(p["username"] for p in duel["players"].values())
            text += f"{names}\n"
    return text


async def handle_admin_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("این پنل فقط برای ادمینه.", show_alert=True)
        return

    data = query.data
    user_id = query.from_user.id

    if data == ADM_STATS:
        await query.answer()
        await context.bot.send_message(chat_id=query.message.chat_id, text=format_stats_text())
    elif data == ADM_PLAYING:
        await query.answer()
        await context.bot.send_message(chat_id=query.message.chat_id, text=format_playing_text())
    elif data == ADM_ADDSCORE:
        await query.answer()
        ADMIN_STATE[user_id] = {"action": "addscore", "step": 0, "data": {}}
        await context.bot.send_message(chat_id=query.message.chat_id, text="آیدی عددی یا یوزرنیم کاربر رو بفرست: (برای انصراف /cancel)")
    elif data == ADM_REMOVESCORE:
        await query.answer()
        ADMIN_STATE[user_id] = {"action": "removescore", "step": 0, "data": {}}
        await context.bot.send_message(chat_id=query.message.chat_id, text="آیدی عددی یا یوزرنیم کاربر رو بفرست: (برای انصراف /cancel)")
    elif data == ADM_BLOCK:
        await query.answer()
        ADMIN_STATE[user_id] = {"action": "block", "step": 0, "data": {}}
        await context.bot.send_message(chat_id=query.message.chat_id, text="آیدی عددی یا یوزرنیم کاربری که می‌خوای مسدود کنی رو بفرست: (برای انصراف /cancel)")
    elif data == ADM_UNBLOCK:
        await query.answer()
        ADMIN_STATE[user_id] = {"action": "unblock", "step": 0, "data": {}}
        await context.bot.send_message(chat_id=query.message.chat_id, text="آیدی عددی یا یوزرنیم کاربری که می‌خوای آزاد کنی رو بفرست: (برای انصراف /cancel)")
    elif data == ADM_BROADCAST:
        await query.answer()
        ADMIN_STATE[user_id] = {"action": "broadcast", "step": 0, "data": {}}
        await context.bot.send_message(chat_id=query.message.chat_id, text="متن پیام همگانی رو بفرست: (برای انصراف /cancel)")


async def handle_admin_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = ADMIN_STATE[user_id]
    action = state["action"]
    text = update.message.text.strip()

    if action in ("addscore", "removescore"):
        if state["step"] == 0:
            target = get_user_by_identifier(text)
            if not target:
                await update.message.reply_text("همچین کاربری پیدا نشد. دوباره امتحان کن یا /cancel بزن.")
                return
            state["data"]["target_id"] = target[0]
            state["step"] = 1
            await update.message.reply_text("چقدر امتیاز؟ (فقط عدد)")
            return
        else:
            try:
                amount = int(text)
            except ValueError:
                await update.message.reply_text("باید یه عدد بفرستی.")
                return
            target_id = state["data"]["target_id"]
            username = get_username_for(target_id)
            signed = amount if action == "addscore" else -amount
            add_point(target_id, username, signed)
            new_score = get_score(target_id)
            del ADMIN_STATE[user_id]
            verb = "اضافه" if action == "addscore" else "کم"
            await update.message.reply_text(f"انجام شد ✅ {amount} امتیاز {verb} شد. امتیاز جدید {username}: {new_score}")
            return

    if action in ("block", "unblock"):
        target = get_user_by_identifier(text)
        if not target:
            await update.message.reply_text("همچین کاربری پیدا نشد. دوباره امتحان کن یا /cancel بزن.")
            return
        target_id = target[0]
        set_blocked(target_id, action == "block")
        del ADMIN_STATE[user_id]
        verb = "مسدود شد 🚫" if action == "block" else "آزاد شد ✅"
        await update.message.reply_text(f"کاربر {get_username_for(target_id)} {verb}")
        return

    if action == "broadcast":
        del ADMIN_STATE[user_id]
        chat_ids = get_all_private_chat_ids()
        sent, failed = 0, 0
        for cid in chat_ids:
            try:
                await context.bot.send_message(chat_id=cid, text=f"📢 {text}")
                sent += 1
            except Exception as e:
                logger.warning("Broadcast failed for %s: %s", cid, e)
                failed += 1
        await update.message.reply_text(f"پیام همگانی ارسال شد. موفق: {sent} — ناموفق: {failed}")
        return


# Legacy direct text-commands — still handy for admins who prefer typing.

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(format_stats_text())


async def playing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(format_playing_text())


async def addscore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await _adjust_score_cmd(update, context, sign=1)


async def removescore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await _adjust_score_cmd(update, context, sign=-1)


async def _adjust_score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, sign: int):
    if len(context.args) < 2:
        await update.message.reply_text("فرمت درست: /addscore <آیدی_یا_یوزرنیم> <مقدار>")
        return
    target = get_user_by_identifier(context.args[0])
    if not target:
        await update.message.reply_text("همچین کاربری پیدا نشد.")
        return
    try:
        amount = int(context.args[1]) * sign
    except ValueError:
        await update.message.reply_text("مقدار باید عدد باشه.")
        return

    target_id = target[0]
    username = get_username_for(target_id)
    add_point(target_id, username, amount)
    new_score = get_score(target_id)
    await update.message.reply_text(f"انجام شد. امتیاز جدید {username}: {new_score}")


async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("فرمت درست: /block <آیدی_یا_یوزرنیم>")
        return
    target = get_user_by_identifier(context.args[0])
    if not target:
        await update.message.reply_text("همچین کاربری پیدا نشد.")
        return
    set_blocked(target[0], True)
    await update.message.reply_text(f"کاربر {get_username_for(target[0])} مسدود شد.")


async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("فرمت درست: /unblock <آیدی_یا_یوزرنیم>")
        return
    target = get_user_by_identifier(context.args[0])
    if not target:
        await update.message.reply_text("همچین کاربری پیدا نشد.")
        return
    set_blocked(target[0], False)
    await update.message.reply_text(f"کاربر {get_username_for(target[0])} آزاد شد.")


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
    app.add_handler(CommandHandler("findopponent", find_opponent_cmd))
    app.add_handler(CommandHandler("cancelsearch", cancel_search_cmd))
    app.add_handler(
        MessageHandler(
            filters.Regex(f"^({BTN_QUIZ}|{BTN_SCORE}|{BTN_LEADERBOARD}|{BTN_INVITE}|{BTN_SUGGEST}|{BTN_FIND_OPPONENT})$"),
            menu_button,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_free_text))
    app.add_handler(CallbackQueryHandler(on_callback_query))

    # Admin
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
