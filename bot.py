# bot.py
# Telegram Bot Builder — single-file implementation suitable for the Android "Telegram Bot Hosting" app.
# Requirements: python-telegram-bot 20.x, aiosqlite
# IMPORTANT: Replace MAIN_BUILDER_TOKEN with your builder bot token before deploying.

import asyncio
import aiosqlite
import logging
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, ChatMember
)
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# =======================
# CONFIG — EDIT THIS LINE ONLY
# =======================
MAIN_BUILDER_TOKEN = "8114373508:AAGeh2Dguftc5X6bp2OzuMSO8rfaWm_r0Zo"  # <-- replace with your builder bot token

# =======================
# DO NOT EDIT BELOW UNLESS YOU KNOW WHAT YOU'RE DOING
# =======================
MAIN_OWNER_ID = 7804637246          # your Telegram numeric ID
MAIN_OWNER_USERNAME = "bonesceo"    # without @

# Global required channels (user must join these channels)
GLOBAL_REQUIRED_CHANNELS = ["legitupdateer", "boteratrack", "boterapro"]

# Notifications channel (where mini-admin payout requests are forwarded)
OWNER_PAYOUT_CHANNEL = "@boteratrack"  # ensure your builder bot is an admin or can send messages to this channel

# Earnings
EARN_PER_USER_NAIRA = 1.00
DOWNLINE_EARN_PER_USER_NAIRA = 0.25

# Mini-admin payout limits (default; per-mini-bot admin can later edit inside /admin)
DEFAULT_MIN_WITHDRAW = 100.0
DEFAULT_MAX_WITHDRAW = 3000.0

DB_PATH = "builder.db"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot-builder")

# =======================
# DATABASE SCHEMA
# =======================
INIT_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS creators (
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  first_seen TEXT,
  referrer_id INTEGER,
  total_downline_earn REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mini_bots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner_id INTEGER NOT NULL,
  token TEXT NOT NULL,
  username TEXT,
  title TEXT,
  created_at TEXT,
  currency TEXT DEFAULT 'NGN',
  ref_reward REAL DEFAULT 0,
  min_withdraw REAL DEFAULT {min_wd},
  max_withdraw REAL DEFAULT {max_wd},
  extra_required_channels TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS mini_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  joined_at TEXT,
  ref_by INTEGER,
  UNIQUE(bot_id, user_id)
);

CREATE TABLE IF NOT EXISTS withdraw_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT,          -- 'mini_user' or 'mini_admin_to_owner'
  bot_id INTEGER,
  requester_id INTEGER,
  amount REAL,
  currency TEXT,
  status TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS balances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT,        -- 'builder_user' for mini-admin earnings, 'mini_user' for user balances
  owner_key TEXT,    -- 'user_id' or 'bot_id:user_id'
  balance REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id INTEGER,
  title TEXT,
  reward REAL,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS task_claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER,
  user_id INTEGER,
  proof TEXT,
  status TEXT, -- 'pending','approved','rejected'
  created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mini_users_bot ON mini_users(bot_id);
CREATE INDEX IF NOT EXISTS idx_mini_bots_owner ON mini_bots(owner_id);
"""

# =======================
# DB HELPERS
# =======================
async def init_db():
    sql = INIT_SQL.format(min_wd=str(DEFAULT_MIN_WITHDRAW), max_wd=str(DEFAULT_MAX_WITHDRAW))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(sql)
        await db.commit()

async def get_balance(scope: str, owner_key: str) -> float:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT balance FROM balances WHERE scope=? AND owner_key=?", (scope, owner_key))
        row = await cur.fetchone()
        return float(row[0]) if row else 0.0

async def add_balance(scope: str, owner_key: str, amount: float):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, balance FROM balances WHERE scope=? AND owner_key=?", (scope, owner_key))
        row = await cur.fetchone()
        if row:
            newbal = float(row[1]) + amount
            await db.execute("UPDATE balances SET balance=? WHERE id=?", (newbal, row[0]))
        else:
            await db.execute("INSERT INTO balances(scope, owner_key, balance) VALUES(?,?,?)", (scope, owner_key, amount))
        await db.commit()

async def set_creator_if_new(user_id: int, username: Optional[str], referrer_id: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM creators WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO creators(user_id, username, first_seen, referrer_id) VALUES(?,?,?,?)",
                (user_id, username or "", datetime.utcnow().isoformat(), referrer_id),
            )
            await db.commit()

async def create_mini_bot(owner_id: int, token: str, username: str, title: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO mini_bots(owner_id, token, username, title, created_at) VALUES(?,?,?,?,?)",
            (owner_id, token, username, title, datetime.utcnow().isoformat())
        )
        await db.commit()
        cur = await db.execute("SELECT last_insert_rowid()")
        row = await cur.fetchone()
        return int(row[0])

async def get_owner_mini_bots(owner_id: int) -> List[Tuple]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, username, title FROM mini_bots WHERE owner_id=?", (owner_id,))
        return await cur.fetchall()

async def get_mini_bot(bot_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, owner_id, token, username, title, currency, ref_reward, min_withdraw, max_withdraw, extra_required_channels FROM mini_bots WHERE id=?", (bot_id,))
        return await cur.fetchone()

async def update_mini_setting(bot_id: int, field: str, value):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE mini_bots SET {field}=? WHERE id=?", (value, bot_id))
        await db.commit()

async def track_mini_user_join(bot_id: int, user_id: int, ref_by: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO mini_users(bot_id, user_id, joined_at, ref_by) VALUES(?,?,?,?)",
                (bot_id, user_id, datetime.utcnow().isoformat(), ref_by)
            )
            await db.commit()
            return True
        except Exception:
            return False

async def count_mini_users(bot_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM mini_users WHERE bot_id=?", (bot_id,))
        row = await cur.fetchone()
        return int(row[0] or 0)

async def list_mini_user_ids(bot_id: int) -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM mini_users WHERE bot_id=?", (bot_id,))
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def get_all_mini_bots_records():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, owner_id, token, username, title, currency, ref_reward, min_withdraw, max_withdraw, extra_required_channels FROM mini_bots")
        return await cur.fetchall()

# Tasks helpers
async def create_task(bot_id: int, title: str, reward: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO tasks(bot_id, title, reward, created_at) VALUES(?,?,?,?)",
                         (bot_id, title, reward, datetime.utcnow().isoformat()))
        await db.commit()

async def list_tasks(bot_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, title, reward FROM tasks WHERE bot_id=?", (bot_id,))
        return await cur.fetchall()

async def claim_task(task_id: int, user_id: int, proof: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO task_claims(task_id, user_id, proof, status, created_at) VALUES(?,?,?,?,?)",
                         (task_id, user_id, proof or "", "pending", datetime.utcnow().isoformat()))
        await db.commit()

async def list_pending_claims(bot_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT tc.id, tc.task_id, t.title, tc.user_id, tc.proof, tc.status FROM task_claims tc "
            "JOIN tasks t ON tc.task_id=t.id WHERE t.bot_id=? AND tc.status='pending'", (bot_id,))
        return await cur.fetchall()

async def set_claim_status(claim_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE task_claims SET status=? WHERE id=?", (status, claim_id))
        await db.commit()

# =======================
# MULTI-BOT MANAGER
# =======================
class BotManager:
    def __init__(self):
        self.apps: Dict[int, Application] = {}   # bot_id -> Application

    async def start_mini_bot(self, record) -> None:
        bot_id, owner_id, token, username, title, currency, ref_reward, min_wd, max_wd, extra_json = record
        if bot_id in self.apps:
            return

        app = ApplicationBuilder().token(token).build()

        # register handlers for the mini bot
        app.add_handler(CommandHandler("start", self._mini_start))
        app.add_handler(CommandHandler("help", self._mini_help))
        app.add_handler(CommandHandler("admin", self._mini_admin))
        app.add_handler(CommandHandler("broadcast", self._mini_broadcast))
        app.add_handler(CommandHandler("stats", self._mini_stats))
        app.add_handler(CommandHandler("balance", self._mini_balance))
        app.add_handler(CommandHandler("withdraw", self._mini_withdraw))
        app.add_handler(CommandHandler("addtask", self._mini_addtask))      # admin: /addtask Title | 10
        app.add_handler(CommandHandler("tasks", self._mini_tasks))         # /tasks -> list tasks
        app.add_handler(CommandHandler("claimtask", self._mini_claimtask)) # /claimtask <task_id> [proof]
        app.add_handler(CommandHandler("review_tasks", self._mini_review_tasks))  # admin: review pending claims
        app.add_handler(CallbackQueryHandler(self._mini_admin_buttons, pattern="^mb:"))
        app.add_handler(CallbackQueryHandler(self._mini_task_buttons, pattern="^task:"))

        app.bot_data["bot_id"] = bot_id
        app.bot_data["owner_id"] = owner_id
        app.bot_data["title"] = title

        await app.initialize()
        await app.start()
        self.apps[bot_id] = app
        log.info(f"Mini bot started: @{username} (db id {bot_id})")

    async def stop_all(self):
        for app in list(self.apps.values()):
            await app.stop()
            await app.shutdown()
        self.apps.clear()

    # ---------------- mini bot handlers ----------------

    async def _mini_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_id = context.application.bot_data["bot_id"]
        owner_id = context.application.bot_data["owner_id"]
        args = context.args or []
        ref_by = None
        if args:
            try:
                if args[0].startswith("ref="):
                    ref_by = int(args[0].split("=", 1)[1])
            except Exception:
                pass

        # Ensure user joined global channels
        if not await ensure_joined_required(update, context, GLOBAL_REQUIRED_CHANNELS):
            return

        # Track join & credit earnings
        user = update.effective_user
        is_new = await track_mini_user_join(bot_id, user.id, ref_by)
        if is_new:
            # credit mini bot admin ₦1.00
            await add_balance("builder_user", str(owner_id), EARN_PER_USER_NAIRA)
            # credit creator's referrer (downline)
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT referrer_id FROM creators WHERE user_id=?", (owner_id,))
                row = await cur.fetchone()
                if row and row[0]:
                    await add_balance("builder_user", str(row[0]), DOWNLINE_EARN_PER_USER_NAIRA)

        bot_username = (await context.bot.get_me()).username
        link = f"https://t.me/{bot_username}?start=ref={user.id}"
        text = (
            "👋 Welcome!\n\n"
            "This is a referral bot.\n\n"
            f"Your personal invite link:\n`{link}`\n\n"
            "Use /tasks to see available tasks, /balance, /withdraw, /help."
        )
        await update.effective_message.reply_text(text, parse_mode="Markdown")

    async def _mini_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        bot_id = context.application.bot_data["bot_id"]
        owner_id = context.application.bot_data["owner_id"]

        if user.id == owner_id:
            # admin help (inside mini bot)
            txt = (
                "📋 Mini Bot Admin Commands\n"
                "/stats - Show your bot stats\n"
                "/balance - Show your admin builder balance\n"
                "/withdraw - Request payout to owner (admin -> owner) when eligible\n"
                "/broadcast <text> - Broadcast to this bot's users\n"
                "/admin - Manage settings (currency, referral reward, withdraw limits, extra must-join channels)\n"
                "/addtask Title | reward - Add a task (e.g. /addtask Follow @x | 10)\n"
                "/tasks - List tasks\n"
                "/review_tasks - Review pending task claims\n"
                "/help - Show this message\n"
            )
            await update.message.reply_text(txt)
        else:
            # regular mini bot user
            txt = (
                "👋 User Commands\n"
                "/start - Begin & get referral link\n"
                "/tasks - List tasks\n"
                "/claimtask <task_id> [proof] - Claim a task (add proof text)\n"
                "/balance - See your balance\n"
                "/withdraw <amount> - Request withdrawal from mini-bot admin\n"
                "/help - Show this message\n"
            )
            await update.message.reply_text(txt)

    async def _mini_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_id = context.application.bot_data["bot_id"]
        total = await count_mini_users(bot_id)
        await update.message.reply_text(f"📊 Total users in this bot: {total}")

    async def _mini_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_id = context.application.bot_data["bot_id"]
        key = f"{bot_id}:{update.effective_user.id}"
        bal = await get_balance("mini_user", key)
        # if owner, also show builder_user balance for them
        owner_id = context.application.bot_data["owner_id"]
        if update.effective_user.id == owner_id:
            admin_bal = await get_balance("builder_user", str(owner_id))
            await update.message.reply_text(f"💼 Your admin builder balance: ₦{admin_bal:.2f}\nUser balance (if any): ₦{bal:.2f}")
        else:
            await update.message.reply_text(f"💼 Your balance: ₦{bal:.2f}")

    async def _mini_withdraw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # For USERS: request withdrawal from mini-bot admin (record in DB and notify admin)
        bot_id = context.application.bot_data["bot_id"]
        owner_id = context.application.bot_data["owner_id"]
        args = context.args or []
        arg0 = args[0] if args else None

        if update.effective_user.id == owner_id:
            # Admin requesting payout from system -> forward to owner channel (mini-admin -> YOU)
            return await update.message.reply_text("Admin should use /withdraw_admin to request payout to owner (you). Use /withdraw_admin <amount>.")
        # User withdraw
        if not arg0:
            return await update.message.reply_text("Usage: /withdraw <amount>\nThis creates a withdrawal request to the mini-bot admin.")
        try:
            amount = float(arg0)
        except ValueError:
            return await update.message.reply_text("Please provide a numeric amount.")
        key = f"{bot_id}:{update.effective_user.id}"
        bal = await get_balance("mini_user", key)
        if amount > bal:
            return await update.message.reply_text("Insufficient balance.")
        # record withdraw request scoped to mini_user
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO withdraw_requests(scope, bot_id, requester_id, amount, currency, status, created_at) VALUES(?,?,?,?,?,?,?)",
                             ("mini_user", bot_id, update.effective_user.id, amount, "NGN", "pending_admin", datetime.utcnow().isoformat()))
            # deduct user balance (we assume admin will pay externally; we keep record)
            cur = await db.execute("SELECT id, balance FROM balances WHERE scope=? AND owner_key=?", ("mini_user", key))
            row = await cur.fetchone()
            if row:
                newbal = float(row[1]) - amount
                await db.execute("UPDATE balances SET balance=? WHERE id=?", (newbal, row[0]))
            await db.commit()
        # Notify mini-bot admin via their bot account (send message)
        try:
            owner_chat = owner_id
            await context.bot.send_message(chat_id=owner_chat,
                                           text=f"🔔 Withdrawal request from user {update.effective_user.id} in your bot (ID {bot_id})\nAmount: ₦{amount:.2f}\nUse your admin tools to pay them.")
        except Exception:
            pass
        await update.message.reply_text("✅ Withdrawal request sent to the mini-bot admin (they will review and pay).")

    async def _mini_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_id = context.application.bot_data["bot_id"]
        owner_id = context.application.bot_data["owner_id"]
        if update.effective_user.id != owner_id:
            return await update.message.reply_text("Only the bot owner can use /broadcast.")
        if not context.args:
            return await update.message.reply_text("Usage: /broadcast Your message here")
        msg = " ".join(context.args)
        user_ids = await list_mini_user_ids(bot_id)
        sent = 0
        for uid in user_ids:
            try:
                await context.bot.send_message(chat_id=uid, text=msg)
                sent += 1
                await asyncio.sleep(0.03)
            except Exception:
                continue
        await update.message.reply_text(f"✅ Broadcast sent to {sent} users (attempted).")

    async def _mini_addtask(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Admin-only: add task in format: /addtask Title | reward
        bot_id = context.application.bot_data["bot_id"]
        owner_id = context.application.bot_data["owner_id"]
        if update.effective_user.id != owner_id:
            return await update.message.reply_text("Only the owner can add tasks. Usage: /addtask Task description | reward")
        txt = " ".join(context.args or [])
        if not txt or "|" not in txt:
            return await update.message.reply_text("Usage: /addtask Task description | reward\nExample: /addtask Follow @xchannel and screenshot | 10")
        parts = txt.split("|", 1)
        title = parts[0].strip()
        try:
            reward = float(parts[1].strip())
        except Exception:
            return await update.message.reply_text("Please provide a valid numeric reward.")
        await create_task(bot_id, title, reward)
        await update.message.reply_text("✅ Task added.")

    async def _mini_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_id = context.application.bot_data["bot_id"]
        rows = await list_tasks(bot_id)
        if not rows:
            return await update.message.reply_text("No tasks available right now.")
        lines = [f"{r[0]}. {r[1]} — Reward: ₦{r[2]:.2f}" for r in rows]
        await update.message.reply_text("Available tasks:\n" + "\n".join(lines))

    async def _mini_claimtask(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_id = context.application.bot_data["bot_id"]
        args = context.args or []
        if not args:
            return await update.message.reply_text("Usage: /claimtask <task_id> [proof text]")
        try:
            task_id = int(args[0])
        except Exception:
            return await update.message.reply_text("Invalid task id.")
        proof = " ".join(args[1:]) if len(args) > 1 else ""
        await claim_task(task_id, update.effective_user.id, proof)
        await update.message.reply_text("✅ Task claim submitted — owner will review.")

    async def _mini_review_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Admin-only: list pending claims with inline approve/reject
        bot_id = context.application.bot_data["bot_id"]
        owner_id = context.application.bot_data["owner_id"]
        if update.effective_user.id != owner_id:
            return await update.message.reply_text("Owner only.")
        rows = await list_pending_claims(bot_id)
        if not rows:
            return await update.message.reply_text("No pending task claims.")
        for r in rows:
            claim_id, task_id, title, user_id, proof, status = r
            text = f"Claim ID: {claim_id}\nTask: {title}\nUser: {user_id}\nProof: {proof}\n"
            kb = [
                [InlineKeyboardButton("Approve", callback_data=f"task:approve:{claim_id}"),
                 InlineKeyboardButton("Reject", callback_data=f"task:reject:{claim_id}")]
            ]
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

    async def _mini_task_buttons(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        parts = (q.data or "").split(":")
        if len(parts) != 3:
            return await q.edit_message_text("Invalid action.")
        _, action, claim_id_s = parts
        try:
            claim_id = int(claim_id_s)
        except Exception:
            return await q.edit_message_text("Invalid claim id.")
        # get claim info and task info
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT task_id, user_id FROM task_claims WHERE id=?", (claim_id,))
            row = await cur.fetchone()
            if not row:
                return await q.edit_message_text("Claim not found.")
            task_id, user_id = row
            cur = await db.execute("SELECT reward FROM tasks WHERE id=?", (task_id,))
            trow = await cur.fetchone()
            reward = float(trow[0]) if trow else 0.0
            # owner id and bot id from context
            bot_id = context.application.bot_data["bot_id"]
            owner_id = context.application.bot_data["owner_id"]

        if action == "approve":
            # Check owner (admin) has enough builder_user balance to pay
            owner_bal = await get_balance("builder_user", str(owner_id))
            if owner_bal < reward:
                await set_claim_status(claim_id, "rejected")
                await q.edit_message_text("Cannot approve: insufficient admin balance to pay task reward.")
                return
            # Deduct from owner's builder_user balance, credit mini_user balance
            await add_balance("builder_user", str(owner_id), -reward)
            key = f"{bot_id}:{user_id}"
            await add_balance("mini_user", key, reward)
            await set_claim_status(claim_id, "approved")
            await q.edit_message_text(f"✅ Approved and paid ₦{reward:.2f} to user {user_id}.")
        else:
            await set_claim_status(claim_id, "rejected")
            await q.edit_message_text("❌ Rejected.")

    async def _mini_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        bot_id = context.application.bot_data["bot_id"]
        owner_id = context.application.bot_data["owner_id"]
        if update.effective_user.id != owner_id:
            return await update.message.reply_text("This panel is for the owner only.")
        rec = await get_mini_bot(bot_id)
        extra = json.loads(rec[9] or "[]")
        txt = (
            "⚙️ *Admin Panel*\n\n"
            f"Currency: `{rec[5]}`\n"
            f"Referral reward (per user): `{rec[6]:.2f}`\n"
            f"Min withdraw: `{rec[7]:.2f}`\n"
            f"Max withdraw: `{rec[8]:.2f}`\n"
            f"Extra must-join channels: {', '.join('@'+c for c in extra) if extra else 'None'}\n\n"
            "Use buttons to configure."
        )
        kb = [
            [InlineKeyboardButton("Set Currency", callback_data="mb:set:currency")],
            [InlineKeyboardButton("Set Ref Reward", callback_data="mb:set:refreward")],
            [InlineKeyboardButton("Set Min Withdraw", callback_data="mb:set:minwd")],
            [InlineKeyboardButton("Set Max Withdraw", callback_data="mb:set:maxwd")],
            [InlineKeyboardButton("Set Extra Must-Join", callback_data="mb:set:extra")],
            [InlineKeyboardButton("Request Payout to Owner", callback_data="mb:request_payout")],
        ]
        await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))

    async def _mini_admin_buttons(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        data = q.data or ""
        parts = data.split(":")
        if len(parts) < 3:
            # handle request payout
            if data == "mb:request_payout":
                return await self._mini_request_payout_callback(q, context)
            return await q.edit_message_text("Invalid.")
        _, action, what = parts
        bot_id = context.application.bot_data["bot_id"]
        owner_id = context.application.bot_data["owner_id"]
        if q.from_user.id != owner_id:
            return await q.edit_message_text("Owner only.")
        prompt_map = {
            "currency": "Send new currency code (e.g., NGN, USDT, TON).",
            "refreward": "Send new referral reward amount (number).",
            "minwd": "Send new *minimum* withdrawal (number).",
            "maxwd": "Send new *maximum* withdrawal (number).",
            "extra": "Send *comma-separated* channel usernames without @ (e.g., chan1,chan2).",
        }
        prompt = prompt_map.get(what, "Send value")
        context.user_data["pending_setting"] = (bot_id, what)
        await q.edit_message_text(prompt, parse_mode="Markdown")

    async def _mini_request_payout_callback(self, q, context):
        # callable from inline button: ask owner to type /request_payout <amount>
        await q.edit_message_text("To request payout from system to owner, use command: /request_payout <amount> (will forward to @boteratrack).")

# manager instance
MANAGER = BotManager()

# =======================
# UTILITIES
# =======================
async def ensure_joined_required(update: Update, context: ContextTypes.DEFAULT_TYPE, channels: List[str]) -> bool:
    user = update.effective_user
    bot = context.bot
    for ch in channels:
        try:
            member = await bot.get_chat_member(f"@{ch}", user.id)
            # statuses that indicate NOT joined
            if member.status in ["left", "kicked"]:
                raise Exception("not joined")
        except Exception:
            buttons = [[InlineKeyboardButton(f"Join @{ch}", url=f"https://t.me/{ch}")]]
            buttons.append([InlineKeyboardButton("✅ I joined. Continue", callback_data="check_join")])
            await update.effective_message.reply_text(
                "🚫 You must join all required channels to continue.",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return False
    return True

# =======================
# MAIN BUILDER HANDLERS
# =======================
async def start_builder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    referrer_id = None
    if args:
        try:
            if args[0].startswith("ref="):
                referrer_id = int(args[0].split("=", 1)[1])
        except Exception:
            pass

    if not await ensure_joined_required(update, context, GLOBAL_REQUIRED_CHANNELS):
        return

    user = update.effective_user
    await set_creator_if_new(user.id, user.username, referrer_id)
    link = f"https://t.me/{(await context.bot.get_me()).username}?start=ref={user.id}"
    txt = (
        "🤖 *Welcome to the Bot Builder!*\n\n"
        "• Create your own mini referral bot quickly.\n"
        "• You earn ₦1 per direct user of your mini bot.\n"
        "• You earn ₦0.25 per user of bots created by people you referred.\n\n"
        f"🔗 Your builder referral link:\n`{link}`\n\n"
        "Use /createbot to connect your BotFather token, or /token_template to see how to paste the token."
    )
    await update.message.reply_text(txt, parse_mode="Markdown")

async def createbot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_joined_required(update, context, GLOBAL_REQUIRED_CHANNELS):
        return
    await update.message.reply_text(
        "Send your *BotFather token* in this exact format:\n\n"
        "`TOKEN: 123456789:AA...YourBotTokenHere`\n\n"
        "After sending, your mini bot will be started and you'll become the owner.",
        parse_mode="Markdown"
    )

async def token_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tpl = (
        "🛠 HOW TO CONNECT YOUR BOT\n\n"
        "1) Open @BotFather and send /newbot to create a bot.\n"
        "2) After creation BotFather will give you a token like:\n\n"
        "`123456789:AAE4-ExampleGeneratedTokenHere`\n\n"
        "3) Copy that token.\n"
        "4) In this builder send:\n\n"
        "`TOKEN: 123456789:AAE4-ExampleGeneratedTokenHere`\n\n"
        "⚠️ Keep your token private. Do not share it publicly."
    )
    await update.message.reply_text(tpl, parse_mode="Markdown")

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip()

    # handle pending setting flows (owner inside mini bot)
    if "pending_setting" in context.user_data:
        bot_id, what = context.user_data.pop("pending_setting")
        if what == "currency":
            await update_mini_setting(bot_id, "currency", txt.upper())
            await update.message.reply_text("✅ Currency updated.")
        elif what == "refreward":
            try:
                v = float(txt)
                await update_mini_setting(bot_id, "ref_reward", v)
                await update.message.reply_text("✅ Referral reward updated.")
            except ValueError:
                await update.message.reply_text("❌ Please send a valid number.")
        elif what == "minwd":
            try:
                v = float(txt)
                await update_mini_setting(bot_id, "min_withdraw", v)
                await update.message.reply_text("✅ Min withdrawal updated.")
            except ValueError:
                await update.message.reply_text("❌ Please send a valid number.")
        elif what == "maxwd":
            try:
                v = float(txt)
                await update_mini_setting(bot_id, "max_withdraw", v)
                await update.message.reply_text("✅ Max withdrawal updated.")
            except ValueError:
                await update.message.reply_text("❌ Please send a valid number.")
        elif what == "extra":
            channels = [c.strip().lstrip("@") for c in txt.split(",") if c.strip()]
            await update_mini_setting(bot_id, "extra_required_channels", json.dumps(channels))
            await update.message.reply_text("✅ Extra must-join channels updated.")
        return

    # Accept builder token format
    if txt.upper().startswith("TOKEN:"):
        token = txt.split(":", 1)[1].strip()
        user = update.effective_user
        # validate token by trying to get bot info
        try:
            temp_app = ApplicationBuilder().token(token).build()
            await temp_app.initialize()
            me = await temp_app.bot.get_me()
            username = me.username
            title = me.first_name or me.username
            await temp_app.shutdown()
        except Exception as e:
            log.exception("Token validation failed", exc_info=e)
            return await update.message.reply_text("❌ Invalid token or the bot is not activated. Make sure you copied the exact token.")

        # create mini bot in DB and start it
        bot_id = await create_mini_bot(user.id, token, username, title)
        rec = await get_mini_bot(bot_id)
        await MANAGER.start_mini_bot(rec)

        # notify owner (you)
        try:
            builder_app: Application = context.application
            await builder_app.bot.send_message(
                chat_id=MAIN_OWNER_ID,
                text=f"📢 New Mini Bot Created!\nOwner: @{user.username or user.id}\nBot: @{username}\nID: {bot_id}\nDate: {datetime.utcnow().isoformat()}"
            )
        except Exception:
            pass

        return await update.message.reply_text(
            f"✅ Mini bot started: @{username}\n\n"
            "You are the owner. Open your mini bot and use /admin to configure settings (currency, referral reward, withdraw limits, extra required channels).",
            parse_mode="Markdown"
        )

async def mybots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = await get_owner_mini_bots(user.id)
    if not rows:
        return await update.message.reply_text("You don't have any mini bots yet. Use /createbot to add one.")
    lines = [f"• ID {r[0]} — @{r[1]} — {r[2]}" for r in rows]
    await update.message.reply_text("Your mini bots:\n" + "\n".join(lines))

async def builder_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bal = await get_balance("builder_user", str(user.id))
    await update.message.reply_text(f"💼 Your builder earnings (available to request): ₦{bal:.2f}")

async def request_payout_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # mini-admin command to request payout from system (mini-admin -> owner channel)
    user = update.effective_user
    # find if this user owns any mini bots
    rows = await get_owner_mini_bots(user.id)
    if not rows:
        return await update.message.reply_text("You are not a mini-bot owner.")
    # their builder_user balance
    bal = await get_balance("builder_user", str(user.id))
    if bal < DEFAULT_MIN_WITHDRAW:
        return await update.message.reply_text(f"Minimum payout request is ₦{DEFAULT_MIN_WITHDRAW:.2f}. Your balance: ₦{bal:.2f}")
    if bal > DEFAULT_MAX_WITHDRAW:
        return await update.message.reply_text(f"Maximum payout per request is ₦{DEFAULT_MAX_WITHDRAW:.2f}. Please request a smaller amount or split requests.")
    # If user provided an amount, use it; else send entire balance
    args = context.args or []
    amount = bal if not args else float(args[0])
    if amount < DEFAULT_MIN_WITHDRAW or amount > DEFAULT_MAX_WITHDRAW:
        return await update.message.reply_text(f"Request amount must be between ₦{DEFAULT_MIN_WITHDRAW:.2f} and ₦{DEFAULT_MAX_WITHDRAW:.2f}.")
    if amount > bal:
        return await update.message.reply_text("Insufficient balance.")
    # record and forward to owner's channel
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO withdraw_requests(scope, bot_id, requester_id, amount, currency, status, created_at) VALUES(?,?,?,?,?,?,?)",
                         ("mini_admin_to_owner", None, user.id, amount, "NGN", "pending_owner", datetime.utcnow().isoformat()))
        # deduct balance (we assume owner will pay)
        cur = await db.execute("SELECT id, balance FROM balances WHERE scope=? AND owner_key=?", ("builder_user", str(user.id)))
        row = await cur.fetchone()
        if row:
            newbal = float(row[1]) - amount
            await db.execute("UPDATE balances SET balance=? WHERE id=?", (newbal, row[0]))
        await db.commit()
    # forward to payout channel
    try:
        builder_app: Application = context.application
        text = (f"💸 Withdrawal Request\n👤 Mini Admin: @{user.username or user.id}\n"
                f"Amount: ₦{amount:.2f}\nDate: {datetime.utcnow().isoformat()}")
        await builder_app.bot.send_message(chat_id=OWNER_PAYOUT_CHANNEL, text=text)
    except Exception:
        pass
    await update.message.reply_text("✅ Your payout request has been sent to the payout channel for processing.")

# Owner / global broadcast
async def broadcast_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MAIN_OWNER_ID:
        return await update.message.reply_text("Only the main owner can use this command.")
    if not context.args:
        return await update.message.reply_text("Usage: /broadcastall Your message here")
    msg = " ".join(context.args)
    sent = 0
    recs = await get_all_mini_bots_records()
    for rec in recs:
        bot_id = rec[0]
        app = MANAGER.apps.get(bot_id)
        if not app:
            continue
        user_ids = await list_mini_user_ids(bot_id)
        for uid in user_ids:
            try:
                await app.bot.send_message(chat_id=uid, text=msg)
                sent += 1
                await asyncio.sleep(0.03)
            except Exception:
                continue
    await update.message.reply_text(f"✅ Broadcast attempted to {sent} users across all mini bots.")

async def stats_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MAIN_OWNER_ID:
        return await update.message.reply_text("Only the main owner can use this command.")
    recs = await get_all_mini_bots_records()
    total_bots = len(recs)
    total_users = 0
    for rec in recs:
        bot_id = rec[0]
        total_users += await count_mini_users(bot_id)
    await update.message.reply_text(f"📊 System Stats\n🤖 Total Mini Bots: {total_bots}\n👥 Total Users Across All Bots: {total_users}")

# Contextual help for builder (owner) and top-level builder users
async def help_builder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == MAIN_OWNER_ID:
        txt = (
            "🛠 Bot Builder Owner Commands\n"
            "/createbot - Create a new mini bot\n"
            "/mybots - Show your bots\n"
            "/builderstats - Show builder earnings\n"
            "/request_payout <amount?> - Mini admin: request payout to owner channel\n"
            "/broadcastall <text> - Broadcast to all mini-bot users\n"
            "/stats_all - Show total bots & total users\n"
            "/token_template - Show how to paste BotFather token\n"
            "/help - Show this message\n"
        )
        await update.message.reply_text(txt)
    else:
        txt = (
            "🤖 Bot Builder Commands\n"
            "/createbot - Connect your bot token to create a mini bot\n"
            "/mybots - List your mini bots\n"
            "/builderstats - See your builder earnings (downline)\n"
            "/token_template - Show how to paste BotFather token\n"
            "/help - Show this message\n"
        )
        await update.message.reply_text(txt)

# =======================
# BOOTSTRAP
# =======================
async def set_commands(app: Application):
    cmds = [
        BotCommand("start", "Start / show referral link"),
        BotCommand("createbot", "Connect your bot token"),
        BotCommand("mybots", "List your mini bots"),
        BotCommand("builderstats", "See your builder earnings"),
        BotCommand("request_payout", "Mini admin: request payout to owner channel"),
        BotCommand("broadcastall", "Owner: broadcast to all mini-bot users"),
        BotCommand("stats_all", "Owner: show system stats"),
        BotCommand("token_template", "Show token insertion guide"),
        BotCommand("help", "Show help"),
    ]
    await app.bot.set_my_commands(cmds)

async def main():
    await init_db()

    builder = ApplicationBuilder().token(MAIN_BUILDER_TOKEN).build()

    builder.add_handler(CommandHandler("start", start_builder))
    builder.add_handler(CommandHandler("help", help_builder))
    builder.add_handler(CommandHandler("createbot", createbot))
    builder.add_handler(CommandHandler("token_template", token_template))
    builder.add_handler(CommandHandler("mybots", mybots))
    builder.add_handler(CommandHandler("builderstats", builder_stats))
    builder.add_handler(CommandHandler("broadcastall", broadcast_all))
    builder.add_handler(CommandHandler("stats_all", stats_all))
    builder.add_handler(CommandHandler("request_payout", request_payout_command))
    builder.add_handler(CommandHandler("token_template", token_template))
    builder.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    builder.add_handler(CallbackQueryHandler(lambda u, c: None, pattern="^check_join$"))

    await builder.initialize()
    await set_commands(builder)
    await builder.start()

    log.info("Builder bot started.")

    # Auto-start mini bots from DB
    recs = await get_all_mini_bots_records()
    for rec in recs:
        try:
            await MANAGER.start_mini_bot(rec)
        except Exception as e:
            log.exception(f"Failed to start mini bot id={rec[0]}: {e}")

    try:
        await asyncio.Event().wait()
    finally:
        await MANAGER.stop_all()
        await builder.stop()
        await builder.shutdown()

if __name__ == "__main__":
    asyncio.run(main())