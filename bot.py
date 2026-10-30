import os
import io
import json
import time
import random
import asyncio
import logging
import re
import threading
import urllib.request
import pymongo
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import List, Dict, Set, Optional
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from openai import AsyncOpenAI

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("GroqTelegramBot")

# Load environment variables from .env
load_dotenv()

# Telegram Token, Owner ID & Group Link
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN") or "yahan_botfather_wala_token"
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
GROUP_LINK = "https://t.me/+CnibVehEsoNiNTdl"

def escape_html(text: str) -> str:
    """Safely escapes HTML special characters for Telegram HTML parse_mode."""
    if not text:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

# ==================== PERSISTENT STORAGE ====================
USERS_DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users_data.json")
REMINDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")
KEYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "groq_keys.json")

# ==================== MONGO DATABASE SETUP ====================
MONGO_URI = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
mongo_client = None
mongo_db = None

def get_mongo_db():
    global mongo_client, mongo_db
    if mongo_db is not None:
        return mongo_db
    if MONGO_URI:
        try:
            mongo_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
            mongo_db = mongo_client["sylvi_bot_db"]
            mongo_client.admin.command('ping')
            logger.info("✅ Connected to MongoDB Atlas (database: sylvi_bot_db)!")
            return mongo_db
        except Exception as e:
            logger.warning(f"⚠️ MongoDB connection failed, fallback to local JSON storage: {e}")
            mongo_client = None
            mongo_db = None
    return None

def load_stored_keys() -> List[str]:
    """Loads initial keys from .env and merges with persistent keys in groq_keys.json or MongoDB."""
    env_keys = os.getenv("GROQ_KEYS")
    keys_list = []
    if env_keys:
        keys_list = [k.strip() for k in env_keys.split(",") if k.strip()]
    else:
        k1 = os.getenv("GROQ_KEY_1")
        k2 = os.getenv("GROQ_KEY_2")
        k3 = os.getenv("GROQ_KEY_3")
        k4 = os.getenv("GROQ_KEY_4")
        for k in [k1, k2, k3, k4]:
            if k and k.strip():
                keys_list.append(k.strip())

    if not keys_list:
        keys_list = ["gsk_key_account_1", "gsk_key_account_2"]

    # MongoDB load
    db = get_mongo_db()
    if db is not None:
        try:
            doc = db["keys"].find_one({"_id": "groq_keys"})
            if doc and "keys" in doc:
                for mk in doc["keys"]:
                    if mk and mk.strip() and mk.strip() not in keys_list:
                        keys_list.append(mk.strip())
        except Exception as e:
            logger.error(f"Error loading keys from MongoDB: {e}")

    # Merge persistent added keys from local file
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                file_keys = data.get("keys", [])
                for fk in file_keys:
                    if fk and fk.strip() and fk.strip() not in keys_list:
                        keys_list.append(fk.strip())
        except Exception as e:
            logger.error(f"Error loading groq_keys.json: {e}")

    return keys_list

def save_stored_keys(keys: List[str]):
    try:
        with open(KEYS_FILE, "w", encoding="utf-8") as f:
            json.dump({"keys": keys}, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving groq_keys.json: {e}")

    db = get_mongo_db()
    if db is not None:
        try:
            db["keys"].replace_one({"_id": "groq_keys"}, {"_id": "groq_keys", "keys": keys}, upsert=True)
        except Exception as e:
            logger.error(f"Error saving keys to MongoDB: {e}")

GROQ_KEYS: List[str] = load_stored_keys()

def load_users_data() -> Dict[str, Dict]:
    db = get_mongo_db()
    if db is not None:
        try:
            docs = list(db["users"].find({}))
            if docs:
                m_data = {}
                for d in docs:
                    d_id = str(d.pop("_id"))
                    m_data[d_id] = d
                return m_data
        except Exception as e:
            logger.error(f"Error loading users from MongoDB: {e}")

    if os.path.exists(USERS_DATA_FILE):
        try:
            with open(USERS_DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading users_data.json: {e}")
    return {}

def save_users_data(data: Dict[str, Dict]):
    try:
        with open(USERS_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving users_data.json: {e}")

    db = get_mongo_db()
    if db is not None and data:
        try:
            col = db["users"]
            bulk = [pymongo.ReplaceOne({"_id": u_id}, {"_id": u_id, **u_info}, upsert=True) for u_id, u_info in data.items()]
            col.bulk_write(bulk)
        except Exception as e:
            logger.error(f"Error saving users to MongoDB: {e}")

users_db: Dict[str, Dict] = load_users_data()

def load_reminders() -> Dict[str, List[Dict]]:
    db = get_mongo_db()
    if db is not None:
        try:
            docs = list(db["reminders"].find({}))
            if docs:
                m_data = {}
                for d in docs:
                    d_id = str(d.pop("_id"))
                    m_data[d_id] = d.get("items", [])
                return m_data
        except Exception as e:
            logger.error(f"Error loading reminders from MongoDB: {e}")

    if os.path.exists(REMINDERS_FILE):
        try:
            with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading reminders.json: {e}")
    return {}

def save_reminders(data: Dict[str, List[Dict]]):
    try:
        with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving reminders.json: {e}")

    db = get_mongo_db()
    if db is not None and data:
        try:
            col = db["reminders"]
            bulk = [pymongo.ReplaceOne({"_id": c_id}, {"_id": c_id, "items": items}, upsert=True) for c_id, items in data.items()]
            col.bulk_write(bulk)
        except Exception as e:
            logger.error(f"Error saving reminders to MongoDB: {e}")

reminders_db: Dict[str, List[Dict]] = load_reminders()

def track_user_data(user) -> None:
    if not user or getattr(user, "is_bot", False):
        return
    user_id_str = str(user.id)
    now = time.time()
    
    if user_id_str not in users_db:
        users_db[user_id_str] = {
            "id": user.id,
            "username": user.username or "",  # Unique Telegram username handle
            "first_name": user.first_name or "",  # Display name
            "last_name": user.last_name or "",
            "is_bot": user.is_bot,
            "language_code": getattr(user, 'language_code', None) or "",
            "first_seen": now,
            "last_seen": now,
            "message_count": 1
        }
    else:
        u = users_db[user_id_str]
        u["username"] = user.username or u.get("username", "")
        u["first_name"] = user.first_name or u.get("first_name", "")
        u["last_name"] = user.last_name or u.get("last_name", "")
        u["last_seen"] = now
        u["message_count"] = u.get("message_count", 0) + 1

    save_users_data(users_db)

def get_user_tag(user) -> str:
    """Returns unique Telegram @username handle if present, else First Name."""
    if not user:
        return "User"
    if user.username:
        return f"@{user.username}"
    return user.first_name or "Bhai"

def is_owner(user_id: int) -> bool:
    if OWNER_ID == 0:
        return False
    return user_id == OWNER_ID

def find_user_in_db(identifier: str) -> Optional[Dict]:
    """
    Look up user in tracked users_db matching display name, unique handle (@username), or user ID.
    """
    clean_id = identifier.strip().lower().lstrip('@')
    if not clean_id or not users_db:
        return None

    for u in users_db.values():
        uname = (u.get("username") or "").lower()
        fname = (u.get("first_name") or "").lower()
        lname = (u.get("last_name") or "").lower()
        uid_str = str(u.get("id", ""))

        if clean_id == uname or clean_id == fname or clean_id == uid_str or clean_id in fname or clean_id in uname:
            return u
    return None

def get_tracked_users_context() -> str:
    """Generates a summary list of known group users with Display Name and Unique @username handle."""
    if not users_db:
        return ""
    lines = []
    for u in users_db.values():
        display = u.get("first_name", "User")
        uname = u.get("username")
        handle_str = f"@{uname}" if uname else "No @username"
        lines.append(f"• Name: {display} | Unique Handle: {handle_str}")
    return "\n".join(lines[:30])

# ==================== REMINDER / LEAVE-MESSAGE LOGIC ====================
def add_pending_reminder(chat_id: int, from_user_name: str, target_query: str, note_text: str):
    """
    Saves a pending reminder for target in chat_id.
    Syncs target_query with users_db to fetch exact user_id, @username, and first_name.
    """
    chat_key = str(chat_id)
    if chat_key not in reminders_db:
        reminders_db[chat_key] = []

    clean_target = target_query.strip().lower().lstrip('@')
    matched_user = find_user_in_db(clean_target)

    target_id = matched_user.get("id") if matched_user else None
    target_username = matched_user.get("username") if matched_user else clean_target
    target_first_name = matched_user.get("first_name") if matched_user else clean_target

    reminder_entry = {
        "from": from_user_name,
        "target_query": clean_target,
        "target_id": target_id,
        "target_username": target_username,
        "target_first_name": target_first_name,
        "note": note_text.strip(),
        "time": time.time()
    }

    reminders_db[chat_key].append(reminder_entry)
    save_reminders(reminders_db)
    logger.info(f"📌 Saved synced reminder from '{from_user_name}' for target '{clean_target}' (MatchedID={target_id}) in chat {chat_id}")

def check_and_get_pending_reminders(chat_id: int, user) -> List[Dict]:
    """
    Checks if there are any pending reminders/messages for the user in chat_id.
    Matches against user ID, @username, First Name, or original target_query.
    """
    chat_key = str(chat_id)
    if chat_key not in reminders_db or not reminders_db[chat_key]:
        return []

    user_id = user.id if user else None
    user_handle = (user.username or "").lower() if user else ""
    user_fname = (user.first_name or "").lower() if user else ""

    matched = []
    remaining = []

    for item in reminders_db[chat_key]:
        item_id = item.get("target_id")
        item_uname = (item.get("target_username") or "").lower().lstrip('@')
        item_fname = (item.get("target_first_name") or "").lower()
        item_query = (item.get("target_query") or "").lower().lstrip('@')

        is_match = False
        if user_id and item_id and user_id == item_id:
            is_match = True
        elif user_handle and (user_handle == item_uname or user_handle == item_query):
            is_match = True
        elif user_fname and (user_fname == item_fname or user_fname == item_query or item_query in user_fname):
            is_match = True

        if is_match:
            matched.append(item)
        else:
            remaining.append(item)

    if matched:
        reminders_db[chat_key] = remaining
        save_reminders(reminders_db)

    return matched

def parse_and_store_reminder_intent(chat_id: int, from_user_name: str, user_text: str):
    """
    Detects if user is asking to leave a message/reminder for someone else, e.g.:
    - 'Koji jese hi message kare usse bolo chup hoja'
    - 'Rahul ko bolna ki kal milna hai'
    - 'Jab @rahul01 message kare toh bolna paise bhej diye'
    - 'Priya ko bol dena meeting 5 baje hai'
    - 'Tell @rahul_01 that project is finished'
    """
    patterns = [
        # "Koji (jese hi|jab|jab bhi) message kare (usse|usko)? (ye)? (bolo|bolna|kehna)? (ki)? <note>"
        r"(@?[\w\d_]+)\s+(?:jese\s+hi|jab|jab\s+bhi)\s+(?:message|chat|online)\s+(?:kare|aaye)\s*(?:toh|tab)?\s*(?:usse|usko)?\s*(?:ye|yeh)?\s*(?:bolo|bolna|kehna|keh\s+dena|batao)?\s*(?:ki)?\s*(.+)",
        # "<name> ko (bolna|bol dena|kehna|keh dena|bata dena) ki <message>"
        r"(@?[\w\d_]+)\s+ko\s+(?:bolna|bol\s+dena|kehna|keh\s+dena|bata\s+dena|batana)\s+(?:ki\s+)?(.+)",
        # "jab (@?name) (aaye|message kare) (toh|tab) (usko|usse)?\s*(?:bolna|kehna)?\s*(?:ki)?\s*(.+)"
        r"jab\s+(@?[\w\d_]+)\s+(?:aaye|message\s+kare|chat\s+kare|online\s+aaye)\s+(?:toh|tab)?\s*(?:usko|usse)?\s*(?:ye|yeh)?\s*(?:bolna|kehna|bol\s+dena|bata\s+dena)?\s*(?:ki)?\s*(.+)",
        # "tell/remind (@?name) that/to/about <message>"
        r"(?:tell|remind)\s+(@?[\w\d_]+)\s+(?:that|about|to)?\s*(.+)"
    ]

    for pat in patterns:
        m = re.search(pat, user_text, re.IGNORECASE)
        if m:
            target = m.group(1)
            note = m.group(2)
            add_pending_reminder(chat_id, from_user_name, target, note)
            break

# ==================== GROUP MEMORY & JOIN STATS TRACKER ====================
class GroupChatMemory:
    """
    Stores rolling chat history per group (up to 15 recent messages) and tracks member joins.
    """
    def __init__(self, max_history_per_group: int = 15):
        self.max_history = max_history_per_group
        # chat_id -> list of {"name": str, "text": str, "time": float}
        self.chat_history: Dict[int, List[Dict[str, str]]] = {}
        # chat_id -> {"joined_members": int, "total_messages": int}
        self.chat_stats: Dict[int, Dict[str, int]] = {}

    def add_message(self, chat_id: int, sender_name: str, text: str):
        if chat_id not in self.chat_history:
            self.chat_history[chat_id] = []
        if chat_id not in self.chat_stats:
            self.chat_stats[chat_id] = {"joined_members": 0, "total_messages": 0}

        self.chat_stats[chat_id]["total_messages"] += 1
        self.chat_history[chat_id].append({
            "name": sender_name,
            "text": text,
            "time": time.time()
        })

        # Keep rolling window
        if len(self.chat_history[chat_id]) > self.max_history:
            self.chat_history[chat_id].pop(0)

    def record_new_joins(self, chat_id: int, count: int = 1):
        if chat_id not in self.chat_stats:
            self.chat_stats[chat_id] = {"joined_members": 0, "total_messages": 0}
        self.chat_stats[chat_id]["joined_members"] += count

    def get_recent_history_prompt(self, chat_id: int) -> List[Dict[str, str]]:
        """Formats rolling group chat history into OpenAI context messages."""
        formatted = []
        history = self.chat_history.get(chat_id, [])
        if not history:
            return formatted

        # Combine recent group chatter as context
        history_snippet = "\n".join([f"{item['name']}: {item['text']}" for item in history[:-1]])
        if history_snippet:
            formatted.append({
                "role": "system",
                "content": f"[Recent Group Chat Context & Memory]:\n{history_snippet}"
            })
        return formatted

group_memory = GroupChatMemory()

# ==================== 4-LETTER WORD GUESSING GAME ====================
class WordGame4Letters:
    """
    Manages active 4-letter word guessing games per chat.
    Allows up to 30 tries per game with hints for matching letters.
    """
    def __init__(self):
        # chat_id -> {"secret_word": str, "attempts_left": int, "max_attempts": int, "guesses": list, "started_by": str, "start_time": float}
        self.active_games: Dict[int, Dict] = {}
        # Curated pool of clean 4-letter English words
        self.words_pool = [
            "GAME", "CODE", "MIND", "PLAY", "STAR", "TIME", "FIRE", "BLUE", "WIND", "BIRD",
            "MOON", "RAIN", "DARK", "BEAT", "ROAD", "GOLD", "LION", "FISH", "PARK", "TREE",
            "BOOK", "DOOR", "RING", "SONG", "CITY", "TEAM", "WORK", "SHIP", "WAVE", "HERO",
            "LIFE", "LOVE", "COOL", "BEST", "FAST", "SAFE", "FREE", "WISH", "HOPE", "SOUL",
            "CARD", "BALL", "LAKE", "SNOW", "ROSE", "KING", "WOLF", "BEAR", "DUCK", "FROG",
            "ROCK", "SAND", "HILL", "WOOD", "LEAF", "SEED", "HEAT", "COLD", "ICE", "LAMP",
            "CAKE", "MILK", "SOUP", "RICE", "COIN", "BANK", "SHOP", "HOME", "DESK", "PAGE",
            "WORD", "NOTE", "TEXT", "CALL", "TALK", "CHAT", "READ", "WALK", "ZOOM", "PEAK"
        ]

    def is_game_active(self, chat_id: int) -> bool:
        return chat_id in self.active_games

    def start_game(self, chat_id: int, user_name: str) -> Dict:
        secret = random.choice(self.words_pool).upper()
        game_data = {
            "secret_word": secret,
            "attempts_left": 30,
            "max_attempts": 30,
            "revealed_slots": ["⬜", "⬜", "⬜", "⬜"],
            "guesses_history": [],  # List of {"guess": str, "user": str}
            "guessed_words_set": set(),
            "started_by": user_name,
            "start_time": time.time()
        }
        self.active_games[chat_id] = game_data
        return game_data

    def get_game(self, chat_id: int) -> Optional[Dict]:
        return self.active_games.get(chat_id)

    def stop_game(self, chat_id: int) -> Optional[Dict]:
        return self.active_games.pop(chat_id, None)

    def process_guess(self, chat_id: int, guess_word: str, user_name: str) -> Dict:
        game = self.active_games.get(chat_id)
        if not game:
            return {"status": "no_game"}

        guess = guess_word.strip().upper()
        secret = game["secret_word"]

        if len(guess) != 4 or not guess.isalpha():
            return {"status": "invalid_length"}

        if guess in game["guessed_words_set"]:
            return {
                "status": "already_guessed",
                "guess": guess,
                "attempts_left": game["attempts_left"]
            }

        game["guessed_words_set"].add(guess)
        game["attempts_left"] -= 1
        tries_used = game["max_attempts"] - game["attempts_left"]

        # Update revealed_slots for exact match letters in green (🟩 {ch})
        revealed = game.get("revealed_slots", ["⬜", "⬜", "⬜", "⬜"])
        for i in range(4):
            if guess[i] == secret[i]:
                revealed[i] = f"🟩 {secret[i]}"

        game["revealed_slots"] = revealed
        hint_display = "  ".join(revealed)

        # Record into history
        game["guesses_history"].append({
            "guess": guess,
            "user": user_name
        })

        # Win condition
        if guess == secret:
            self.stop_game(chat_id)
            return {
                "status": "win",
                "secret_word": secret,
                "tries_used": tries_used,
                "user_name": user_name,
                "hint_display": hint_display,
                "guesses_history": game["guesses_history"]
            }

        # Game over condition
        if game["attempts_left"] <= 0:
            history_copy = list(game["guesses_history"])
            self.stop_game(chat_id)
            return {
                "status": "game_over",
                "secret_word": secret,
                "user_name": user_name,
                "tries_used": tries_used,
                "hint_display": hint_display,
                "guesses_history": history_copy
            }

        return {
            "status": "hint",
            "guess": guess,
            "hint_display": hint_display,
            "guesses_history": game["guesses_history"],
            "attempts_left": game["attempts_left"],
            "max_attempts": game["max_attempts"],
            "tries_used": tries_used,
            "user_name": user_name
        }

word_game = WordGame4Letters()

# ==================== SLIDING WINDOW RATE LIMIT TRACKER ====================
class KeyRateTracker:
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.num_keys = len(keys)
        self.request_timestamps: Dict[int, List[float]] = {i: [] for i in range(self.num_keys)}
        self.cooldown_until: Dict[int, float] = {i: 0.0 for i in range(self.num_keys)}
        self._current_index = 0

    def add_key(self, new_key: str):
        """Dynamically adds a new Groq API key to tracker."""
        if new_key not in self.keys:
            self.keys.append(new_key)
            new_idx = len(self.keys) - 1
            self.num_keys = len(self.keys)
            self.request_timestamps[new_idx] = []
            self.cooldown_until[new_idx] = 0.0
            logger.info(f"🔑 Dynamically added new Groq API key (Index #{new_idx + 1}). Total keys: {self.num_keys}")

    def _clean_old_timestamps(self, key_idx: int, now: float):
        cutoff = now - 60.0
        self.request_timestamps[key_idx] = [
            t for t in self.request_timestamps[key_idx] if t > cutoff
        ]

    def get_best_key_index(self) -> Optional[int]:
        now = time.time()
        best_idx = None
        min_recent_requests = 9999

        for offset in range(self.num_keys):
            idx = (self._current_index + offset) % self.num_keys
            if self.cooldown_until[idx] > now:
                continue

            self._clean_old_timestamps(idx, now)
            recent_count = len(self.request_timestamps[idx])

            if recent_count < 28:
                self._current_index = (idx + 1) % self.num_keys
                return idx

            if recent_count < min_recent_requests:
                min_recent_requests = recent_count
                best_idx = idx

        if best_idx is not None:
            self._current_index = (best_idx + 1) % self.num_keys
            return best_idx

        return None

    def record_request(self, key_idx: int):
        now = time.time()
        self._clean_old_timestamps(key_idx, now)
        self.request_timestamps[key_idx].append(now)

    def set_cooldown(self, key_idx: int, duration_seconds: float = 60.0):
        now = time.time()
        self.cooldown_until[key_idx] = max(self.cooldown_until[key_idx], now + duration_seconds)
        logger.warning(
            f"⚠️ Key index {key_idx} ({self.keys[key_idx][:10]}...) placed on cooldown for {duration_seconds}s "
            f"(Until {time.strftime('%H:%M:%S', time.localtime(self.cooldown_until[key_idx]))})"
        )

    def get_min_cooldown_wait(self) -> float:
        now = time.time()
        active_cooldowns = [self.cooldown_until[i] - now for i in range(self.num_keys) if self.cooldown_until[i] > now]
        return min(active_cooldowns) if active_cooldowns else 1.0

rate_tracker = KeyRateTracker(GROQ_KEYS)

async def get_llm_reply(messages: List[Dict[str, str]]) -> str:
    total_keys = len(GROQ_KEYS)
    if total_keys == 0:
        return "Koi API Key set nahi hai."

    preferred_models = [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant"
    ]

    for attempt in range(total_keys * 2):
        key_idx = rate_tracker.get_best_key_index()
        
        if key_idx is None:
            wait_time = max(1.0, min(rate_tracker.get_min_cooldown_wait(), 5.0))
            logger.info(f"⏳ All keys are cooling down/near RPM limit. Waiting {wait_time:.1f}s before retrying...")
            await asyncio.sleep(wait_time)
            key_idx = rate_tracker.get_best_key_index()
            if key_idx is None:
                continue

        api_key = GROQ_KEYS[key_idx]
        rate_tracker.record_request(key_idx)
        recent_req_count = len(rate_tracker.request_timestamps[key_idx])

        for m_name in preferred_models:
            try:
                logger.info(f"🚀 Calling Groq API ({m_name}) with Key #{key_idx + 1}/{total_keys} (RPM Window: {recent_req_count}/30 reqs)...")
                client = AsyncOpenAI(
                    base_url="https://api.groq.com/openai/v1",
                    api_key=api_key
                )
                response = await client.chat.completions.create(
                    model=m_name,
                    messages=messages,
                    max_tokens=800
                )
                return response.choices[0].message.content

            except Exception as e:
                err_str = str(e).lower()
                logger.warning(f"❌ Key #{key_idx + 1} with model '{m_name}' failed with error: {e}")

                if "rpd" in err_str or "tpd" in err_str or "daily" in err_str:
                    logger.error(f"🔴 Key #{key_idx + 1} hit DAILY limit (RPD/TPD). Setting 24h cooldown...")
                    rate_tracker.set_cooldown(key_idx, duration_seconds=86400.0)
                    break
                elif "429" in err_str or "rate limit" in err_str or "rpm" in err_str or "tpm" in err_str or "quota" in err_str:
                    logger.warning(f"🟡 Key #{key_idx + 1} hit MINUTE limit (RPM/TPM/429). Setting 60s cooldown and rotating...")
                    rate_tracker.set_cooldown(key_idx, duration_seconds=60.0)
                    break
                elif "model" in err_str or "404" in err_str or "not found" in err_str or "does not exist" in err_str:
                    logger.warning(f"⚠️ Model '{m_name}' not found on Groq, trying fallback model...")
                    continue
                else:
                    rate_tracker.set_cooldown(key_idx, duration_seconds=15.0)
                    break

    return "Abhi saare API keys busy/rate-limited hain, thodi der baad try karein."

# ==================== NEW MEMBER JOIN HANDLER ====================
async def welcome_new_members_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.new_chat_members:
        return

    chat_id = update.message.chat.id
    joined_count = len(update.message.new_chat_members)
    group_memory.record_new_joins(chat_id, joined_count)

    for member in update.message.new_chat_members:
        track_user_data(member)
        name = member.first_name or "New Member"
        logger.info(f"👤 New member joined in group {chat_id}: {name}")

# ==================== COMMAND HANDLERS ====================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays help menu with all available commands."""
    if not update.message:
        return

    user = update.message.from_user
    track_user_data(user)
    user_id = user.id if user else 0

    help_text = (
        "🤖 <b>Sylvi AI Group Assistant &amp; Chat Bot - Help Menu</b>\n\n"
        "📌 <b>General Commands:</b>\n"
        "• <code>/help</code> ya <code>/start</code> - Sabhi available commands ki list dekhein.\n"
        "• <code>/statsgc</code> - Current group chat ki stats aur memory info dekhein.\n"
        "• <code>/4words</code> - Start a 4-letter word guessing game (30 tries + hints!).\n"
        "• <code>/4words stop</code> - End the active 4-letter word game.\n\n"
        "🔥 <b>Bot Features &amp; Style:</b>\n"
        "• <b>AI Name:</b> Bot ka naam <b>Sylvi</b> hai! Group mein message reply karke, <code>@username</code> mention karke, ya <b>Sylvi</b> bolkar sawaal poocho.\n"
        "• <b>Normal Human Vibe:</b> Bot bilkul normal, friendly aur helpful insaan ki tarah Hinglish mein baat karta hai.\n"
        "• <b>Unique Username Tagging:</b> Jab kisi ko tag karne bolenge, bot us user ke profile ka unique handle (<code>@username</code>) dhoond kar tag karega.\n"
        "• <b>Automatic Reminders / Target Waiting Notes:</b> Agar aap kisi ke Name ya Username par message chhodenge (e.g. 'Rahul jab message kare usko bolna meeting 5 baje hai'), toh jaise hi target user GC mein message karega, bot use turant wo message bol dega!\n"
        "• <b>Comprehensive User Data Sync:</b> Past chat history, replies, new joins se sabhi users ke Name aur Unique Handle database mein sync hotey hain.\n"
    )

    if is_owner(user_id):
        help_text += (
            "\n👑 <b>Owner Special Commands:</b>\n"
            "• <code>/addapi &lt;key&gt;</code> - Dynamic Nayi Groq API key add karein.\n"
            "• <code>/listapi</code> - Sabhi active Groq API keys ka status dekhein.\n"
            "• <code>/allusers</code> - Total collected users database stats dekhein.\n"
            "• <code>/mongoexport</code> - Mongo DB backup JSON export karein.\n"
            "• <code>/mongoimport</code> - Local JSON data Mongo DB mein sync karein.\n"
        )

    await update.message.reply_text(help_text, parse_mode='HTML')

async def addapi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to add a new Groq API key dynamically."""
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if not is_owner(user_id):
        await update.message.reply_text("🚫 Only the bot owner can use this command!")
        return

    if not context.args or len(context.args) == 0:
        await update.message.reply_text("⚠️ Usage: `/addapi <gsk_your_groq_api_key>`", parse_mode='Markdown')
        return

    new_key = context.args[0].strip()
    if not new_key.startswith("gsk_") and len(new_key) < 15:
        await update.message.reply_text("⚠️ Invalid Groq API key format! Keys usually start with `gsk_`.", parse_mode='Markdown')
        return

    if new_key in GROQ_KEYS:
        await update.message.reply_text("ℹ️ This API key is already added!", parse_mode='Markdown')
        return

    GROQ_KEYS.append(new_key)
    save_stored_keys(GROQ_KEYS)
    rate_tracker.add_key(new_key)

    total_keys = len(GROQ_KEYS)
    masked_key = f"{new_key[:8]}...{new_key[-4:]}"
    logger.info(f"Owner {user_id} added new Groq API key dynamically: {masked_key}. Total keys: {total_keys}")
    await update.message.reply_text(
        f"✅ **New Groq API Key Added Successfully!**\n"
        f"• **Key:** `{masked_key}`\n"
        f"• **Total Configured Keys:** `{total_keys}`",
        parse_mode='Markdown'
    )

async def listapi_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to list all configured Groq API keys."""
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if not is_owner(user_id):
        await update.message.reply_text("🚫 Only the bot owner can use this command!")
        return

    total_keys = len(GROQ_KEYS)
    masked_list = []
    for idx, k in enumerate(GROQ_KEYS, start=1):
        m = f"{k[:8]}...{k[-4:]}" if len(k) > 12 else "gsk_***"
        masked_list.append(f"• Key #{idx}: `{m}`")

    keys_str = "\n".join(masked_list)
    await update.message.reply_text(
        f"🔑 **Groq API Keys Status ({total_keys} total active):**\n\n{keys_str}",
        parse_mode='Markdown'
    )

async def statsgc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays stats for the current group chat."""
    if not update.message:
        return

    if update.message.from_user:
        track_user_data(update.message.from_user)

    chat_id = update.message.chat.id
    stats = group_memory.chat_stats.get(chat_id, {"joined_members": 0, "total_messages": 0})
    mem_count = len(group_memory.chat_history.get(chat_id, []))

    await update.message.reply_text(
        "📊 **Group Chat Stats & Memory Status:**\n\n"
        f"• 👥 **New Members Joined:** `{stats['joined_members']}`\n"
        f"• 💬 **Total Messages Processed:** `{stats['total_messages']}`\n"
        f"• 🧠 **Active History Memory:** `{mem_count}` recent messages saved",
        parse_mode='Markdown'
    )

async def allusers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to view collected users data statistics."""
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if not is_owner(user_id):
        await update.message.reply_text("🚫 Only the bot owner can use this command!")
        return

    total_users = len(users_db)
    if total_users == 0:
        await update.message.reply_text("📁 <b>No user data collected yet.</b>", parse_mode='HTML')
        return

    # Sort top active users
    sorted_users = sorted(
        users_db.values(),
        key=lambda x: x.get("message_count", 0) if isinstance(x, dict) else 0,
        reverse=True
    )
    top_list = []
    for u in sorted_users[:10]:
        if not isinstance(u, dict):
            continue
        uid = u.get("id", "N/A")
        username = u.get("username", "")
        fname = u.get("first_name", "")

        if username:
            uname = f"@{escape_html(str(username))}"
        elif fname:
            uname = escape_html(str(fname))
        else:
            uname = "User"

        msg_cnt = u.get("message_count", 0)
        top_list.append(f"• <code>{uid}</code> | {uname} | Messages: <code>{msg_cnt}</code>")

    top_str = "\n".join(top_list)
    await update.message.reply_text(
        f"👥 <b>Collected Users Database Summary:</b>\n"
        f"• <b>Total Tracked Users:</b> <code>{total_users}</code>\n\n"
        f"🏆 <b>Top Active Users:</b>\n{top_str}",
        parse_mode='HTML'
    )

async def mongoexport_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to export MongoDB collections as a downloadable JSON backup file."""
    if not update.message or not update.message.from_user:
        return
    user_id = update.message.from_user.id
    if not is_owner(user_id):
        await update.message.reply_text("🚫 Only the bot owner can use this command!")
        return

    export_data = {
        "users": users_db,
        "reminders": reminders_db,
        "groq_keys": GROQ_KEYS,
        "exported_at": time.time()
    }

    export_json = json.dumps(export_data, indent=2, ensure_ascii=False)
    bytes_io = io.BytesIO(export_json.encode("utf-8"))
    filename = f"sylvi_mongo_export_{int(time.time())}.json"

    await update.message.reply_document(
        document=bytes_io,
        filename=filename,
        caption="📤 <b>MongoDB Data Export Complete!</b>\nAll users, reminders, and Groq API keys exported successfully.",
        parse_mode="HTML"
    )

async def mongoimport_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to import data into MongoDB collections."""
    if not update.message or not update.message.from_user:
        return
    user_id = update.message.from_user.id
    if not is_owner(user_id):
        await update.message.reply_text("🚫 Only the bot owner can use this command!")
        return

    doc = update.message.document
    if not doc:
        # Sync current in-memory / local JSON data to MongoDB
        save_users_data(users_db)
        save_reminders(reminders_db)
        save_stored_keys(GROQ_KEYS)
        await update.message.reply_text("📥 <b>Local Data Synced to MongoDB Successfully!</b>", parse_mode="HTML")
        return

    try:
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()
        imp_data = json.loads(file_bytes.decode("utf-8"))

        imp_users = imp_data.get("users", {})
        imp_reminders = imp_data.get("reminders", {})
        imp_keys = imp_data.get("groq_keys", [])

        if imp_users:
            users_db.update(imp_users)
            save_users_data(users_db)

        if imp_reminders:
            reminders_db.update(imp_reminders)
            save_reminders(reminders_db)

        if imp_keys:
            for k in imp_keys:
                if k not in GROQ_KEYS:
                    GROQ_KEYS.append(k)
            save_stored_keys(GROQ_KEYS)

        await update.message.reply_text(
            f"📥 <b>MongoDB Data Import Successful!</b>\n"
            f"• Users: <code>{len(imp_users)}</code>\n"
            f"• Reminders: <code>{len(imp_reminders)}</code>\n"
            f"• Keys: <code>{len(imp_keys)}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to import JSON data to MongoDB: {e}")

async def fourwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /4words command to start, check, or stop a 4-letter word guessing game."""
    if not update.message:
        return

    chat_id = update.message.chat.id
    user = update.message.from_user
    user_name = user.first_name if user else "Player"
    track_user_data(user)

    args = context.args or []
    subcmd = args[0].lower() if args else ""

    if subcmd in ["stop", "end", "quit", "reset"]:
        stopped = word_game.stop_game(chat_id)
        if stopped:
            secret = escape_html(stopped["secret_word"])
            await update.message.reply_text(
                f"🛑 <b>Game Stopped!</b>\n\n"
                f"The hidden 4-letter word was <b>\"{secret}\"</b>.\n"
                f"Type <code>/4words</code> anytime to start a new game!",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text("ℹ️ No active game in this chat. Type <code>/4words</code> to start one!", parse_mode='HTML')
        return

    # Check if a game is already active
    if word_game.is_game_active(chat_id):
        game = word_game.get_game(chat_id)
        tries = game.get("max_attempts", 30) - game.get("attempts_left", 30)
        revealed = game.get("revealed_slots", ["⬜", "⬜", "⬜", "⬜"])
        hint_display = "  ".join(revealed)

        text = (
            f"<b>4-letter mode</b>\n\n"
            f"<i>{tries}/30 guesses</i>\n\n"
            f"{hint_display}\n\n"
            f"💡 Type any 4-letter word in chat to guess!\n"
            f"🛑 Type <code>/4words stop</code> to end game."
        )
        await update.message.reply_text(text, parse_mode='HTML')
        return

    # Start a new game
    game = word_game.start_game(chat_id, user_name)
    text = (
        f"<b>4-letter mode</b>\n\n"
        f"<i>0/30 guesses</i>\n\n"
        f"🎯 <b>Goal:</b> Guess the secret 4-letter word!\n"
        f"💡 Send any 4-letter English word in chat to make a guess.\n"
        f"🛑 Type <code>/4words stop</code> to end anytime."
    )
    await update.message.reply_text(text, parse_mode='HTML')

# ==================== MAIN MESSAGE HANDLER ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    message = update.message
    chat_type = message.chat.type
    user_text = message.text.strip()
    if not user_text:
        return

    # Ignore command messages starting with '/'
    if user_text.startswith('/'):
        return

    # Collect user data from sender
    user = message.from_user
    if user:
        track_user_data(user)

    # Collect user data from replied-to user if present
    if message.reply_to_message and message.reply_to_message.from_user:
        track_user_data(message.reply_to_message.from_user)

    sender_name = user.first_name if user else "Bantai"
    sender_user_handle = get_user_tag(user)
    chat_id = message.chat.id

    # ==================== 4-LETTER WORD GAME GUESS CHECK ====================
    if word_game.is_game_active(chat_id):
        clean_word = user_text.strip()
        if len(clean_word) == 4 and clean_word.isalpha():
            result = word_game.process_guess(chat_id, clean_word, sender_name)
            status = result.get("status")

            if status == "win":
                secret = escape_html(result["secret_word"])
                tries = result["tries_used"]
                hint_display = result["hint_display"]
                uname = escape_html(sender_name)

                text = (
                    f"🎉 <b>CONGRATULATIONS {uname}! YOU WON!</b> 🏆\n\n"
                    f"<b>4-letter mode</b>\n"
                    f"<i>{tries}/30 guesses</i>\n\n"
                    f"{hint_display}\n\n"
                    f"🎯 Secret Word: <b>{secret}</b>\n"
                    f"Type <code>/4words</code> to start a new game!"
                )
                await message.reply_text(text, parse_mode='HTML')
                return

            elif status == "game_over":
                secret = escape_html(result["secret_word"])
                tries = result["tries_used"]
                hint_display = result["hint_display"]

                text = (
                    f"❌ <b>GAME OVER!</b> 💀\n\n"
                    f"<b>4-letter mode</b>\n"
                    f"<i>{tries}/30 guesses</i>\n\n"
                    f"{hint_display}\n\n"
                    f"🎯 Hidden Word was: <b>{secret}</b>\n"
                    f"Type <code>/4words</code> to start a new game!"
                )
                await message.reply_text(text, parse_mode='HTML')
                return

            elif status == "hint":
                tries = result["tries_used"]
                hint_display = result["hint_display"]

                text = (
                    f"<b>4-letter mode</b>\n\n"
                    f"<i>{tries}/30 guesses</i>\n\n"
                    f"{hint_display}"
                )
                await message.reply_text(text, parse_mode='HTML')
                return

            elif status == "already_guessed":
                attempts_left = result["attempts_left"]
                await message.reply_text(
                    f"⚠️ <b>'{escape_html(result['guess'])}' was already guessed!</b> Try another word. (<i>{attempts_left}/30 guesses left</i>)",
                    parse_mode='HTML'
                )
                return

    # ==================== PRIVATE CHAT HANDLING ====================
    if chat_type == 'private':
        user_id = user.id if user else 0
        user_is_owner = is_owner(user_id)
        role_label = " (Owner)" if user_is_owner else ""
        logger.info(f"Private DM message received from {sender_name} ({user_id}){role_label}: {user_text[:30]}...")

        owner_context = " Aap apne Owner ke saath baat kar rahe ho." if user_is_owner else ""
        messages_payload = [
            {
                "role": "system",
                "content": (
                    f"Aapka naam Sylvi hai. Aap ek friendly, smart aur helpful human-like Hinglish AI assistant ho.{owner_context} "
                    "Aap Telegram user ke saath private DM mein baat kar rahe ho. "
                    "Friendly, polite, natural human tone aur helpful manner mein clear Hinglish/English mein reply do."
                )
            },
            {"role": "user", "content": f"{sender_name}: {user_text}"}
        ]

        reply_text = await get_llm_reply(messages_payload)
        await message.reply_text(reply_text)
        return

    # ==================== GROUP CHAT HANDLING ====================
    if chat_type in ['group', 'supergroup']:
        chat_id = message.chat.id

        # 1. CHECK & DELIVER ANY PENDING REMINDERS FOR THIS USER (TARGET WAITING NOTES)
        pending_notes = check_and_get_pending_reminders(chat_id, user)
        if pending_notes:
            for item in pending_notes:
                sender_who_left_note = item.get("from", "Someone")
                note_content = item.get("note", "")
                user_tag = get_user_tag(user)
                reminder_msg = (
                    f"📌 **Hey {user_tag}!**\n"
                    f"👤 **{sender_who_left_note}** ne tere liye message chhoda tha:\n"
                    f"👉 *\"{note_content}\"*"
                )
                await message.reply_text(reminder_msg, parse_mode='Markdown')
                logger.info(f"✅ Delivered pending synced reminder to {user_tag} from {sender_who_left_note}")

        # 2. CHECK IF CURRENT MESSAGE IS A REMINDER INTENT FOR SOMEONE ELSE (e.g. 'Koji jese hi message kare usse bolo chup hoja')
        parse_and_store_reminder_intent(chat_id, sender_name, user_text)

        # Record into rolling group memory
        group_memory.add_message(chat_id, sender_name, user_text)

        bot_username = context.bot.username if context.bot else ""
        is_direct_bot_reply = False
        is_bot_mentioned = False
        is_name_called = False

        # 1. Check if user replied to bot's message
        if message.reply_to_message and message.reply_to_message.from_user:
            if context.bot and message.reply_to_message.from_user.id == context.bot.id:
                is_direct_bot_reply = True

        # 2. Check if bot username is mentioned
        if bot_username and f"@{bot_username.lower()}" in user_text.lower():
            is_bot_mentioned = True

        # 3. Check if bot's AI name "Sylvi" is called/tagged in message
        if re.search(r'\bsylvi\b', user_text, re.IGNORECASE):
            is_name_called = True

        # STRICT GROUP REPLY TRIGGER: ONLY reply if replied to bot's message, tagged by @username, or called by AI name Sylvi
        should_reply = is_direct_bot_reply or is_bot_mentioned or is_name_called

        if not should_reply:
            logger.info("Skipping group message (not replied to bot, not mentioned @username, and 'Sylvi' not called).")
            return

        logger.info(f"Processing group message in chat {chat_id} (DirectReply={is_direct_bot_reply}, Mention={is_bot_mentioned}, NameCalled={is_name_called}): {user_text[:30]}...")

        # Get tracked group users list to help LLM find exact unique @username handles from past chat data
        tracked_users_context = get_tracked_users_context()

        # System Normal Human Prompt for Sylvi
        system_instruction = (
            "Aapka naam Sylvi hai. Aap ek friendly, smart aur helpful human-like Hinglish AI assistant ho. "
            "Aap aam insaan ki tarah naturally, politely aur clearly Hinglish mein baat karte ho.\n\n"
            "STRICT RULES:\n"
            "1. PERSONALITY & TONE: Normal, friendly aur helpful human tone mein clear Hinglish/English mein jawab do. Kisi bhi tarah ke street slang, abusive language, ya chapri words bilkul use mat karo. Politeness aur clarity maintain rakho.\n"
            "2. UNIQUE @USERNAME TAGGING RULES:\n"
            "   - Niche [Tracked Group Users Database] di gayi hai. Agar koi user kisi ko tag karke bolne ke liye kahe (e.g. 'Rahul ko tag karke bolo...', 'Priya ko bolo...'), toh Database se us user ka exact unique Telegram `@username` handle dhoond kar wahi handle tag karo (e.g. '@rahul_official'). Agar unke profile par @username nahi hai, toh unka Name use karo.\n"
            "   - Direct reply karte waqt user ko bewaja tag MAT karo.\n"
            "3. REMINDERS / WAITING TARGET MESSAGES:\n"
            "   - Agar koi user kahe ki kisi target user ke message karte hi usse ye bolna (e.g., 'Koji jese hi message kare usse bolo chup hoja' ya 'Rahul jab message kare usko bolna meeting 5 baje hai'), toh aap polite aur clear way mein confirm kar do: 'Sure! Us member ke group mein message karte hi main unhe ye message deliver kar dunga. 👍'\n\n"
            f"[Tracked Group Users Database]:\n{tracked_users_context}"
        )

        messages_payload = [{"role": "system", "content": system_instruction}]

        # Attach recent group chat context & memory
        history_context = group_memory.get_recent_history_prompt(chat_id)
        if history_context:
            messages_payload.extend(history_context)

        # Add context if user replied to another message
        if message.reply_to_message and message.reply_to_message.text:
            reply_msg = message.reply_to_message
            if is_direct_bot_reply:
                messages_payload.append({"role": "assistant", "content": reply_msg.text})
            else:
                replied_user = reply_msg.from_user
                replied_user_name = replied_user.first_name if replied_user else "User"
                replied_handle = get_user_tag(replied_user)
                messages_payload.append({
                    "role": "user",
                    "content": f"[Context - Replying to {replied_user_name} ({replied_handle}): \"{reply_msg.text}\"]"
                })

        # Add current user message with sender name & handle
        messages_payload.append({
            "role": "user",
            "content": f"Sender: {sender_name} ({sender_user_handle})\nMessage: {user_text}"
        })

        # Get response from LLM
        reply_text = await get_llm_reply(messages_payload)

        # Send reply directly
        await message.reply_text(reply_text, reply_to_message_id=message.message_id)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Sylvi Groq AI Bot is running")

    def log_message(self, format, *args):
        pass  # Suppress HTTP access logs to keep console logs clean

def start_health_check_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info(f"🌐 Health check HTTP server started on port {port}")

        # Start self-ping keep-alive background thread to prevent Render free tier sleeping
        def keep_alive_ping():
            time.sleep(15)  # Wait for server startup
            target_urls = [f"http://127.0.0.1:{port}/"]
            ext_url = os.getenv("RENDER_EXTERNAL_URL")
            if ext_url:
                if not ext_url.endswith("/"):
                    ext_url += "/"
                target_urls.append(ext_url)

            while True:
                for u in target_urls:
                    try:
                        req = urllib.request.Request(u, headers={"User-Agent": "SylviBotKeepAlive/1.0"})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            pass
                    except Exception as ex:
                        logger.debug(f"Keep-alive ping to {u} result: {ex}")
                time.sleep(240)  # Ping every 4 minutes (before Render 15-minute sleep threshold)

        ping_thread = threading.Thread(target=keep_alive_ping, daemon=True)
        ping_thread.start()
        logger.info("⚡ Render auto-ping keep-alive service started (4-min interval).")

    except Exception as e:
        logger.warning(f"Could not start health check HTTP server: {e}")

def main():
    logger.info("Starting Sylvi Groq AI Telegram Bot (Owner DM + Dynamic API Keys Active)...")
    start_health_check_server()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Public & Owner Command Handlers
    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("addapi", addapi_command))
    app.add_handler(CommandHandler("listapi", listapi_command))
    app.add_handler(CommandHandler("statsgc", statsgc_command))
    app.add_handler(CommandHandler("allusers", allusers_command))
    app.add_handler(CommandHandler("mongoexport", mongoexport_command))
    app.add_handler(CommandHandler("mongoimport", mongoimport_command))
    app.add_handler(CommandHandler("4words", fourwords_command))

    # Member Join Event Handler
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members_handler))

    # General Text Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()

if __name__ == '__main__':
    main()
