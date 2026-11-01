import os
import io
import json
import time
import random
import string
import asyncio
import logging
import re
import threading
import urllib.request
import math
import pymongo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import List, Dict, Set, Optional
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, WebAppInfo
from telegram.ext import Application, MessageHandler, CommandHandler, CallbackQueryHandler, InlineQueryHandler, filters, ContextTypes
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
BOT_START_TIME = time.time()

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

# ==================== ACTIVITY LOGS SETUP ====================
LOGS_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs_config.json")

def load_logs_gc_id() -> int:
    db = get_mongo_db()
    if db is not None:
        try:
            doc = db["logs_config"].find_one({"_id": "logs_gc"})
            if doc and "logs_gc_id" in doc:
                return int(doc["logs_gc_id"])
        except Exception as e:
            logger.error(f"Error loading logs_gc_id from MongoDB: {e}")

    if os.path.exists(LOGS_CONFIG_FILE):
        try:
            with open(LOGS_CONFIG_FILE, "r", encoding="utf-8") as f:
                return int(json.load(f).get("logs_gc_id", 0))
        except Exception:
            pass

    return int(os.getenv("LOGS_GC_ID", "0"))

def save_logs_gc_id(chat_id: int):
    try:
        with open(LOGS_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"logs_gc_id": chat_id}, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving logs_config.json: {e}")

    db = get_mongo_db()
    if db is not None:
        try:
            db["logs_config"].replace_one(
                {"_id": "logs_gc"},
                {"_id": "logs_gc", "logs_gc_id": chat_id, "updated_at": time.time()},
                upsert=True
            )
            logger.info(f"✅ Saved LOGS_GC_ID ({chat_id}) to MongoDB logs_config collection.")
        except Exception as e:
            logger.error(f"Error saving logs_gc_id to MongoDB: {e}")

LOGS_GC_ID = load_logs_gc_id()

def send_telegram_log(text: str):
    global LOGS_GC_ID
    if LOGS_GC_ID == 0:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": LOGS_GC_ID,
            "text": f"📊 <b>BOT ACTIVITY LOG</b>\n\n{text}",
            "parse_mode": "HTML"
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    except Exception as e:
        logger.warning(f"Could not send Telegram activity log: {e}")

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

# ==================== CULTIVATION CARD & ECONOMY STORAGE ====================
CARDS_POOL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cards_pool.json")
USER_CARDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_cards.json")
USER_ECONOMY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "user_economy.json")

DEFAULT_CARDS_POOL = []

def assign_pool_card_codes_in_list(pool: List[Dict]):
    used_codes = {str(c.get("code")) for c in pool if c.get("code")}
    updated = False
    for c in pool:
        if not c.get("code"):
            while True:
                code = ''.join(random.choices(string.digits, k=4))
                if code not in used_codes:
                    used_codes.add(code)
                    c["code"] = code
                    updated = True
                    break
    if updated:
        save_cards_pool(pool)

def find_user_card_by_code(user_id: int, code: str) -> Optional[Dict]:
    code = code.strip().upper()
    for key, c in user_cards_db.items():
        if c.get("owner_id") == user_id and str(c.get("code", "")).strip().upper() == code:
            return c
    return None

def remove_user_card_by_code(user_id: int, code: str):
    code = code.strip().upper()
    keys_to_remove = []
    for key, c in user_cards_db.items():
        if c.get("owner_id") == user_id and str(c.get("code", "")).strip().upper() == code:
            keys_to_remove.append(key)
    for key in keys_to_remove:
        user_cards_db.pop(key, None)
        db = get_mongo_db()
        if db is not None:
            try:
                db["user_cards"].delete_one({"_id": key})
            except Exception as e:
                logger.error(f"Error deleting card {key} from MongoDB: {e}")
    save_user_cards(user_cards_db)

def load_cards_pool() -> List[Dict]:
    db = get_mongo_db()
    pool = []
    if db is not None:
        try:
            docs = list(db["cards_pool"].find({}))
            if docs:
                for d in docs:
                    d.pop("_id", None)
                    pool.append(d)
        except Exception as e:
            logger.error(f"Error loading cards_pool from MongoDB: {e}")

    if not pool and os.path.exists(CARDS_POOL_FILE):
        try:
            with open(CARDS_POOL_FILE, "r", encoding="utf-8") as f:
                pool = json.load(f)
        except Exception as e:
            logger.error(f"Error loading cards_pool.json: {e}")

    if not pool:
        pool = list(DEFAULT_CARDS_POOL)

    assign_pool_card_codes_in_list(pool)
    return pool

def save_cards_pool(pool: List[Dict]):
    try:
        with open(CARDS_POOL_FILE, "w", encoding="utf-8") as f:
            json.dump(pool, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving cards_pool.json: {e}")

    db = get_mongo_db()
    if db is not None and pool:
        try:
            col = db["cards_pool"]
            bulk = [pymongo.ReplaceOne({"id": c["id"]}, {"_id": c["id"], **c}, upsert=True) for c in pool]
            col.bulk_write(bulk)
        except Exception as e:
            logger.error(f"Error saving cards_pool to MongoDB: {e}")

cards_pool_db: List[Dict] = load_cards_pool()

def load_user_cards() -> Dict[str, Dict]:
    db = get_mongo_db()
    if db is not None:
        try:
            docs = list(db["user_cards"].find({}))
            if docs:
                m_data = {}
                for d in docs:
                    doc_id = str(d.pop("_id"))
                    if "_" not in doc_id and "owner_id" in d:
                        doc_id = f"{d['owner_id']}_{doc_id}"
                    m_data[doc_id] = d
                return m_data
        except Exception as e:
            logger.error(f"Error loading user_cards from MongoDB: {e}")

    if os.path.exists(USER_CARDS_FILE):
        try:
            with open(USER_CARDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                m_data = {}
                for k, v in data.items():
                    if "_" not in k and isinstance(v, dict) and "owner_id" in v:
                        k = f"{v['owner_id']}_{k}"
                    m_data[k] = v
                return m_data
        except Exception as e:
            logger.error(f"Error loading user_cards.json: {e}")
    return {}

def save_user_cards(data: Dict[str, Dict]):
    try:
        with open(USER_CARDS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving user_cards.json: {e}")

    db = get_mongo_db()
    if db is not None:
        try:
            col = db["user_cards"]
            if data:
                bulk = [pymongo.ReplaceOne({"_id": k}, {"_id": k, **c_info}, upsert=True) for k, c_info in data.items()]
                col.bulk_write(bulk)
        except Exception as e:
            logger.error(f"Error saving user_cards to MongoDB: {e}")

user_cards_db: Dict[str, Dict] = load_user_cards()

def load_user_economy() -> Dict[str, Dict]:
    db = get_mongo_db()
    if db is not None:
        try:
            docs = list(db["user_economy"].find({}))
            if docs:
                m_data = {}
                for d in docs:
                    uid = str(d.pop("_id"))
                    m_data[uid] = d
                return m_data
        except Exception as e:
            logger.error(f"Error loading user_economy from MongoDB: {e}")

    if os.path.exists(USER_ECONOMY_FILE):
        try:
            with open(USER_ECONOMY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading user_economy.json: {e}")
    return {}

def save_user_economy(data: Dict[str, Dict]):
    try:
        with open(USER_ECONOMY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error saving user_economy.json: {e}")

    db = get_mongo_db()
    if db is not None:
        try:
            col = db["user_economy"]
            if data:
                bulk = [pymongo.ReplaceOne({"_id": uid}, {"_id": uid, **u_info}, upsert=True) for uid, u_info in data.items()]
                col.bulk_write(bulk)
        except Exception as e:
            logger.error(f"Error saving user_economy to MongoDB: {e}")

user_economy_db: Dict[str, Dict] = load_user_economy()

active_card_offers: Dict[str, Dict] = {}  # offer_id -> {"code", "seller_id", "price_ss", "time"}
active_card_swaps: Dict[str, Dict] = {}   # swap_id -> {"from_user_id", "to_user_id", "from_code", "to_code", "tier", "time"}
group_message_counts: Dict[int, int] = {} # chat_id -> msg_count for 100-msg drop trigger
active_drops: Dict[int, Dict] = {}        # chat_id -> dropped card info

user_summon_cooldowns: Dict[int, float] = {}  # user_id -> last_summon_timestamp
chat_summon_cooldowns: Dict[int, float] = {}  # chat_id -> last_summon_timestamp

TIER_WORTH = {
    "Mortal": 200,
    "Awakened": 500,
    "Transcendent": 1200,
    "Celestial": 3000,
    "Primordial": 10000
}

TIER_SYMBOLS = {
    "Mortal": "⚪️",
    "Awakened": "🟢",
    "Transcendent": "🔵",
    "Celestial": "🟣",
    "Primordial": "🔥"
}

def get_card_worth(card: Dict) -> int:
    if "worth" in card and card["worth"]:
        return card["worth"]
    tier = card.get("tier", "Mortal").capitalize()
    return TIER_WORTH.get(tier, 200)

def generate_unique_card_code() -> str:
    """Generates a unique 4-digit numeric code ('0000'-'9999') for a claimed card."""
    while True:
        code = ''.join(random.choices(string.digits, k=4))
        if code not in user_cards_db:
            return code

def clean_expired_cards():
    """Removes non-permanent cards from user_cards_db whose stay limit has expired."""
    now = time.time()
    expired_codes = []
    for code, card in list(user_cards_db.items()):
        if not card.get("is_permanent", False):
            expire_at = card.get("expire_at", 0)
            if now > expire_at:
                expired_codes.append(code)

    if expired_codes:
        for code in expired_codes:
            user_cards_db.pop(code, None)
            db = get_mongo_db()
            if db is not None:
                try:
                    db["user_cards"].delete_one({"_id": code})
                except Exception as e:
                    logger.error(f"Error deleting expired card {code} from MongoDB: {e}")
        save_user_cards(user_cards_db)
        logger.info(f"🧹 Cleaned {len(expired_codes)} expired cards from roster.")

def format_time_left(card: Dict) -> str:
    if card.get("is_permanent", False):
        return "Eternal Life / Immortal Body ♾️"
    expire_at = card.get("expire_at", 0)
    now = time.time()
    diff = expire_at - now
    if diff <= 0:
        return "Expired 💀"
    days = int(diff // 86400)
    hours = int((diff % 86400) // 3600)
    mins = int((diff % 3600) // 60)
    if days > 0:
        return f"{days} Day{'s' if days > 1 else ''} remaining"
    elif hours > 0:
        return f"{hours} Hour{'s' if hours > 1 else ''} remaining"
    else:
        return f"{mins} Min{'s' if mins > 1 else ''} remaining"

def get_user_economy(user_id: int) -> Dict:
    uid_str = str(user_id)
    if uid_str not in user_economy_db:
        user_economy_db[uid_str] = {
            "user_id": user_id,
            "spirit_stones": 0,
            "pills": {
                "Qi Gathering Pill": 0,
                "Jade Spirit Pill": 0,
                "Heavenly Life Pill": 0,
                "Emperor's Longevity Pill": 0,
                "Eternal Life Pill": 0
            },
            "last_daily": 0.0
        }
        save_user_economy(user_economy_db)
    else:
        u = user_economy_db[uid_str]
        if "pills" not in u:
            u["pills"] = {
                "Qi Gathering Pill": 0,
                "Jade Spirit Pill": 0,
                "Heavenly Life Pill": 0,
                "Emperor's Longevity Pill": 0,
                "Eternal Life Pill": 0
            }
    return user_economy_db[uid_str]

TIER_EMOJIS = {
    "Mortal": "⚪ Mortal",
    "Awakened": "🟢 Awakened",
    "Transcendent": "🔵 Transcendent",
    "Celestial": "🟣 Celestial",
    "Primordial": "🔥 Primordial"
}

TIER_RATES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tier_rates.json")
DEFAULT_TIER_PROBABILITIES = {
    "Primordial": 0.01,
    "Celestial": 0.20,
    "Transcendent": 1.20,
    "Awakened": 3.50
}

def load_tier_probabilities() -> Dict[str, float]:
    rates = dict(DEFAULT_TIER_PROBABILITIES)
    db = get_mongo_db()
    if db is not None:
        try:
            doc = db["logs_config"].find_one({"_id": "tier_rates"})
            if doc and "rates" in doc:
                rates.update(doc["rates"])
                return rates
        except Exception as e:
            logger.error(f"Error loading tier rates from MongoDB: {e}")

    if os.path.exists(TIER_RATES_FILE):
        try:
            with open(TIER_RATES_FILE, "r", encoding="utf-8") as f:
                rates.update(json.load(f))
        except Exception as e:
            logger.error(f"Error loading tier_rates.json: {e}")
    return rates

def save_tier_probabilities(rates: Dict[str, float]):
    try:
        with open(TIER_RATES_FILE, "w", encoding="utf-8") as f:
            json.dump(rates, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving tier_rates.json: {e}")

    db = get_mongo_db()
    if db is not None:
        try:
            db["logs_config"].replace_one(
                {"_id": "tier_rates"},
                {"_id": "tier_rates", "rates": rates, "updated_at": time.time()},
                upsert=True
            )
        except Exception as e:
            logger.error(f"Error saving tier rates to MongoDB: {e}")

TIER_PROBABILITIES: Dict[str, float] = load_tier_probabilities()

def roll_card_tier() -> str:
    """
    Dynamic Tier Probabilities (adjustable via /rarity):
    """
    p_p = TIER_PROBABILITIES.get("Primordial", 0.01)
    p_c = TIER_PROBABILITIES.get("Celestial", 0.20)
    p_t = TIER_PROBABILITIES.get("Transcendent", 1.20)
    p_a = TIER_PROBABILITIES.get("Awakened", 3.50)

    roll = random.uniform(0, 100)

    cum_p = p_p
    if roll < cum_p:
        return "Primordial"

    cum_c = cum_p + p_c
    if roll < cum_c:
        return "Celestial"

    cum_t = cum_c + p_t
    if roll < cum_t:
        return "Transcendent"

    cum_a = cum_t + p_a
    if roll < cum_a:
        return "Awakened"

    return "Mortal"

def create_card_instance(owner_id: int, forced_tier: Optional[str] = None) -> Dict:
    tier = forced_tier if forced_tier else roll_card_tier()
    matching_pool = [c for c in cards_pool_db if c.get("tier", "").lower() == tier.lower()]

    if not matching_pool:
        base_card = {"id": f"DEF_{int(time.time())}", "code": ''.join(random.choices(string.digits, k=4)), "name": f"{tier} Cultivator", "tier": tier, "photo": ""}
    else:
        base_card = random.choice(matching_pool)

    code = base_card.get("code")
    if not code:
        code = ''.join(random.choices(string.digits, k=4))
        base_card["code"] = code

    now = time.time()
    worth = TIER_WORTH.get(tier, 200)
    card_data = {
        "code": code,
        "owner_id": owner_id,
        "name": base_card.get("name", "Unknown Cultivator"),
        "tier": tier,
        "worth": worth,
        "photo": base_card.get("photo", ""),
        "obtained_at": now,
        "expire_at": now + (72 * 3600),  # 72 hours default stay limit
        "is_permanent": False
    }

    instance_key = f"{owner_id}_{code}"
    user_cards_db[instance_key] = card_data
    save_user_cards(user_cards_db)
    logger.info(f"✨ Created Card '{card_data['name']}' [{tier}] Code={code} for User {owner_id}")
    return card_data

PILL_CATALOG = {
    "1": {"name": "Qi Gathering Pill", "seconds": 24 * 3600, "price": 200, "desc": "+1 Day Stay"},
    "2": {"name": "Jade Spirit Pill", "seconds": 3 * 24 * 3600, "price": 500, "desc": "+3 Days Stay"},
    "3": {"name": "Heavenly Life Pill", "seconds": 9 * 24 * 3600, "price": 1200, "desc": "+9 Days Stay"},
    "4": {"name": "Emperor's Longevity Pill", "seconds": 18 * 24 * 3600, "price": 2000, "desc": "+18 Days Stay"},
    "5": {"name": "Eternal Life Pill", "seconds": -1, "price": 18000, "desc": "Permanent Stay ♾️"}
}

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
    send_telegram_log(f"👤 {joined_count} new member(s) joined group <code>{chat_id}</code>.")

    for member in update.message.new_chat_members:
        track_user_data(member)
        name = member.first_name or "New Member"
        logger.info(f"👤 New member joined in group {chat_id}: {name}")

# ==================== CULTIVATION CARD COMMANDS ====================

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays bot operational status, system health, owner info, and game statistics."""
    if not update.message:
        return

    user = update.message.from_user
    if user:
        track_user_data(user)

    # Calculate uptime
    uptime_seconds = int(time.time() - BOT_START_TIME)
    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    mins = (uptime_seconds % 3600) // 60
    secs = uptime_seconds % 60

    uptime_str = f"{secs}s"
    if days > 0:
        uptime_str = f"{days}d {hours}h {mins}m {secs}s"
    elif hours > 0:
        uptime_str = f"{hours}h {mins}m {secs}s"
    elif mins > 0:
        uptime_str = f"{mins}m {secs}s"

    # Database status check
    db = get_mongo_db()
    db_status = "🟢 Connected (MongoDB Atlas)" if db is not None else "🟡 Local JSON Storage (Fallback)"

    # Owner info
    owner_str = f"<code>{OWNER_ID}</code>" if OWNER_ID != 0 else "Not Set ⚠️"

    # Stats
    total_users = len(users_db)
    pool_cards = len(cards_pool_db)
    claimed_cards = len(user_cards_db)
    active_offers_count = len(active_card_offers)
    active_drops_count = len(active_drops)
    keys_count = len(GROQ_KEYS)

    text = (
        f"🤖 <b>SYLVI BOT STATUS & SYSTEM HEALTH</b> 🟢\n\n"
        f"⏱️ <b>Uptime:</b> <code>{uptime_str}</code>\n"
        f"👑 <b>Owner ID:</b> {owner_str}\n"
        f"💾 <b>Database:</b> {db_status}\n"
        f"🔑 <b>Groq API Keys Loaded:</b> <code>{keys_count} keys</code>\n\n"
        f"📊 <b>Game & Economy Statistics:</b>\n"
        f"• Total Tracked Users: <code>{total_users}</code>\n"
        f"• Global Card Pool: <code>{pool_cards} cards</code>\n"
        f"• Claimed Cards in Roster: <code>{claimed_cards} cards</code>\n"
        f"• Active Market Offers: <code>{active_offers_count}</code>\n"
        f"• Active World Drops: <code>{active_drops_count} chats</code>\n\n"
        f"✨ <i>All systems operational! Use <code>/help</code> for available commands.</i>"
    )

    await update.message.reply_text(text, parse_mode='HTML')

async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends an inline button with the Cultivation Mini App link."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    web_app_url = os.getenv("RENDER_EXTERNAL_URL") or "https://t-xcem.onrender.com"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("OPEN MINI APP", web_app=WebAppInfo(url=web_app_url))]
    ])

    await message.reply_text(
        "🌟 <b>CULTIVATION CULT TELEGRAM MINI APP</b> ⛩️\n\n"
        "Tap the button below to open your cultivation dashboard!",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

# ==================== CONNECT 4 GAME (/JOIN4) ====================
active_connect4_games: Dict[int, Dict] = {}  # chat_id -> game_state

def check_connect4_win(board, token) -> bool:
    rows = 6
    cols = 7
    # Horizontal
    for r in range(rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r][c+1] == token and board[r][c+2] == token and board[r][c+3] == token:
                return True
    # Vertical
    for r in range(rows - 3):
        for c in range(cols):
            if board[r][c] == token and board[r+1][c] == token and board[r+2][c] == token and board[r+3][c] == token:
                return True
    # Diagonal positive slope
    for r in range(rows - 3):
        for c in range(cols - 3):
            if board[r][c] == token and board[r+1][c+1] == token and board[r+2][c+2] == token and board[r+3][c+3] == token:
                return True
    # Diagonal negative slope
    for r in range(3, rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r-1][c+1] == token and board[r-2][c+2] == token and board[r-3][c+3] == token:
                return True
    return False

def is_board_full(board) -> bool:
    return all(board[0][c] != "⚪️" for c in range(7))

def drop_token(board, col, token) -> int:
    for r in range(5, -1, -1):
        if board[r][col] == "⚪️":
            board[r][col] = token
            return r
    return -1

def build_connect4_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    row_buttons = []
    for c in range(7):
        row_buttons.append(InlineKeyboardButton(str(c+1), callback_data=f"c4_col_{c}"))
    return InlineKeyboardMarkup([row_buttons])

def format_connect4_board(game: Dict) -> str:
    board = game["board"]
    board_str = "\n".join([" ".join(row) for row in board])
    p1 = escape_html(game["player1_name"])
    p2 = escape_html(game["player2_name"])
    turn_name = p1 if game["current_turn"] == game["player1_id"] else p2
    turn_token = "🔴" if game["current_turn"] == game["player1_id"] else "🟡"

    if game["player2_id"] == 0:
        pot_info = "🎁 <b>Reward on Win:</b> <code>+100 SS</code> (Free Play vs Bot)"
    else:
        pot_info = f"💰 <b>Pot / Bet:</b> <code>{game['bet'] * 2} SS</code> (<code>{game['bet']} SS</code> each)"

    return (
        f"🔴 <b>CONNECT 4 BATTLE</b> 🟡\n\n"
        f"👤 <b>Player 1 (🔴):</b> {p1}\n"
        f"👤 <b>Player 2 (🟡):</b> {p2}\n"
        f"{pot_info}\n\n"
        f"{board_str}\n\n"
        f"👉 <b>Turn:</b> {turn_token} <b>{escape_html(turn_name)}</b>\n"
        f"<i>Tap column numbers (1-7) below to drop your disk!</i>"
    )

async def join4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a Connect 4 game vs Bot (Free, +100 SS on win) or PvP against another user with SS bet."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    chat_id = update.message.chat.id

    args = context.args or []
    opponent_id = 0
    opponent_name = "Sylvi Bot 🤖"
    bet = 0

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        opp_user = update.message.reply_to_message.from_user
        if opp_user.id == user.id:
            await update.message.reply_text("❌ You cannot play against yourself!", parse_mode='HTML')
            return
        if opp_user.is_bot:
            await update.message.reply_text("❌ You cannot challenge bots as a user opponent!", parse_mode='HTML')
            return

        if not args or not args[0].isdigit():
            await update.message.reply_text(
                "⚠️ <b>Usage for PvP:</b> <code>/join4 &lt;bet_ss&gt;</code> (replying to opponent's message)\n"
                "<i>Example:</i> <code>/join4 50</code>",
                parse_mode='HTML'
            )
            return

        bet = int(args[0])
        if bet < 10:
            await update.message.reply_text("❌ Minimum bet for PvP Connect 4 is <b>10 SS</b>!", parse_mode='HTML')
            return

        eco1 = get_user_economy(user.id)
        if eco1["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ You don't have enough Spirit Stones! Your balance: <code>{eco1['spirit_stones']} SS</code>", parse_mode='HTML')
            return

        eco2 = get_user_economy(opp_user.id)
        if eco2["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ Opponent <b>{escape_html(opp_user.first_name)}</b> does not have enough Spirit Stones ({bet} SS required)!", parse_mode='HTML')
            return

        opponent_id = opp_user.id
        opponent_name = opp_user.first_name

        eco1["spirit_stones"] -= bet
        eco2["spirit_stones"] -= bet
        save_user_economy(user_economy_db)
    else:
        bet = 0

    game_state = {
        "chat_id": chat_id,
        "player1_id": user.id,
        "player1_name": user.first_name,
        "player2_id": opponent_id,
        "player2_name": opponent_name,
        "bet": bet,
        "board": [["⚪️" for _ in range(7)] for _ in range(6)],
        "current_turn": user.id,
        "status": "active"
    }
    active_connect4_games[chat_id] = game_state

    text = format_connect4_board(game_state)
    keyboard = build_connect4_keyboard(chat_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

async def connect4_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user = query.from_user
    chat_id = query.message.chat.id
    data = query.data or ""

    game = active_connect4_games.get(chat_id)
    if not game or game["status"] != "active":
        await query.answer("⚠️ No active Connect 4 game in this chat! Start one with /join4.", show_alert=True)
        return

    if user.id != game["current_turn"]:
        await query.answer("⏳ It's not your turn!", show_alert=True)
        return

    col = int(data.replace("c4_col_", ""))
    token = "🔴" if user.id == game["player1_id"] else "🟡"

    row = drop_token(game["board"], col, token)
    if row == -1:
        await query.answer("⚠️ Column is full! Choose another column.", show_alert=True)
        return

    await query.answer()

    if check_connect4_win(game["board"], token):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        winner_id = user.id
        winner_name = user.first_name

        if game["player2_id"] == 0:
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += 100
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - YOU BEAT THE BOT!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Reward Received:</b> <code>+100 Spirit Stones (SS)</code> 💎\n\n"
                f"<i>Type /join4 to play again!</i>"
            )
        else:
            pot = game["bet"] * 2
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += pot
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - WINNER!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Prize Won:</b> <code>+{pot} SS</code> 💎\n\n"
                f"<i>Type /join4 &lt;bet&gt; to play again!</i>"
            )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    if is_board_full(game["board"]):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        if game["player2_id"] != 0:
            eco1 = get_user_economy(game["player1_id"])
            eco1["spirit_stones"] += game["bet"]
            eco2 = get_user_economy(game["player2_id"])
            eco2["spirit_stones"] += game["bet"]
            save_user_economy(user_economy_db)

        board_str = "\n".join([" ".join(r) for r in game["board"]])
        refund_str = f" Both players' bets (<code>{game['bet']} SS</code> each) have been refunded!" if game["player2_id"] != 0 else ""
        text = (
            f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
            f"{board_str}\n\n"
            f"<i>Board is full.{refund_str}</i>"
        )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    next_turn = game["player2_id"] if game["current_turn"] == game["player1_id"] else game["player1_id"]
    game["current_turn"] = next_turn

    if game["status"] == "active" and game["current_turn"] == 0:
        bot_token = "🟡"
        valid_cols = [c for c in range(7) if game["board"][0][c] == "⚪️"]
        if valid_cols:
            # Loophole: Bot picks a random valid column and never defends or blocks player!
            bot_col = random.choice(valid_cols)
            drop_token(game["board"], bot_col, bot_token)

            if check_connect4_win(game["board"], bot_token):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤖 <b>CONNECT 4 - SYLVI BOT WINS!</b> 💀\n\n"
                    f"{board_str}\n\n"
                    f"👑 <b>Winner:</b> <b>Sylvi Bot 🤖</b>\n\n"
                    f"<i>Type /join4 to try again!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            if is_board_full(game["board"]):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
                    f"{board_str}\n\n"
                    f"<i>Game ended in a draw!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            game["current_turn"] = game["player1_id"]

    text = format_connect4_board(game)
    keyboard = build_connect4_keyboard(chat_id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        pass

async def logsgc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to set the target group chat ID for real-time activity logs."""
    global LOGS_GC_ID
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_owner(user.id):
        await message.reply_text("🚫 Only the bot owner can use this command!")
        return

    args = context.args or []
    if not args:
        chat_id = message.chat.id
        LOGS_GC_ID = chat_id
        save_logs_gc_id(chat_id)
        send_telegram_log(f"✅ Activity logs group set to this chat ID: <code>{chat_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"This chat (ID: <code>{chat_id}</code>) will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
        return

    try:
        target_id = int(args[0].strip())
        LOGS_GC_ID = target_id
        save_logs_gc_id(target_id)
        send_telegram_log(f"✅ Activity logs group set to chat ID: <code>{target_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"Target Chat ID: <code>{target_id}</code> will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
    except ValueError:
        await message.reply_text("⚠️ Invalid Chat ID! Must be a valid integer.", parse_mode='HTML')

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the Spirit Stones economy leaderboard (top players)."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    if not user_economy_db:
        await message.reply_text("🏆 <b>Leaderboard is currently empty!</b>", parse_mode='HTML')
        return

    sorted_econ = sorted(
        user_economy_db.values(),
        key=lambda x: x.get("spirit_stones", 0) if isinstance(x, dict) else 0,
        reverse=True
    )

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = []

    for idx, eco in enumerate(sorted_econ[:10]):
        if not isinstance(eco, dict):
            continue
        uid = eco.get("user_id")
        ss = eco.get("spirit_stones", 0)
        u_info = users_db.get(str(uid), {})
        uname = u_info.get("username")
        fname = u_info.get("first_name") or f"Cultivator {uid}"

        display_name = f"@{escape_html(str(uname))}" if uname else escape_html(str(fname))
        medal = medals[idx] if idx < len(medals) else f"•"

        lines.append(f"{medal} <b>{display_name}</b> — <code>{ss} SS</code> 💎")

    lb_str = "\n".join(lines)

    text = (
        f"🏆 <b>CULTIVATION SPIRIT STONES LEADERBOARD</b> 💎\n\n"
        f"{lb_str}\n\n"
        f"<i>💡 Earn more Spirit Stones via /daily, /summon, or betting in /mine!</i>"
    )

    await message.reply_text(text, parse_mode='HTML')

async def summon_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Summons a random card drop in group chat with tier probabilities."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()

    # RESTRICTION: Group chat only
    if update.message.chat.type not in ['group', 'supergroup']:
        await update.message.reply_text("⚠️ <code>/summon</code> command only works in group chats!", parse_mode='HTML')
        return

    now = time.time()
    user_id = user.id
    chat_id = update.message.chat.id

    # 1. Check per-user 60-min cooldown (3600 seconds)
    last_user_summon = user_summon_cooldowns.get(user_id, 0.0)
    user_diff = now - last_user_summon
    if user_diff < 3600:
        rem_sec = int(3600 - user_diff)
        rem_mins = rem_sec // 60
        rem_secs_left = rem_sec % 60
        await update.message.reply_text(
            f"⏳ <b>Summon Cooldown Active!</b>\n\nYou must wait <code>{rem_mins}m {rem_secs_left}s</code> before you can summon again.",
            parse_mode='HTML'
        )
        return

    # 2. Check group global 1-min cooldown (60 seconds)
    last_chat_summon = chat_summon_cooldowns.get(chat_id, 0.0)
    chat_diff = now - last_chat_summon
    if chat_diff < 60:
        rem_sec = int(60 - chat_diff)
        await update.message.reply_text(
            f"⏳ <b>Group Summon Cooldown Active!</b>\n\nA card was recently summoned in this group. Please wait <code>{rem_sec}s</code>.",
            parse_mode='HTML'
        )
        return

    # Set cooldowns
    user_summon_cooldowns[user_id] = now
    chat_summon_cooldowns[chat_id] = now

    if not cards_pool_db:
        await update.message.reply_text(
            "⚠️ <b>Global card pool is empty!</b> No cards available to summon. Owner needs to add cards using <code>/addcard</code>.",
            parse_mode='HTML'
        )
        return

    dropped_tier = roll_card_tier()
    matching_pool = [c for c in cards_pool_db if c.get("tier", "").lower() == dropped_tier.lower()]

    if matching_pool:
        base_card = random.choice(matching_pool)
    else:
        base_card = random.choice(cards_pool_db)

    c_name = base_card.get("name", "Cultivator")
    dropped_tier = base_card.get("tier", "Mortal")
    c_photo = base_card.get("photo", "")

    send_telegram_log(f"🎴 Card Summoned in chat <code>{chat_id}</code> by {user.first_name} (@{user.username or 'none'}): <b>{c_name}</b> [{dropped_tier}]")

    active_drops[chat_id] = {
        "name": c_name,
        "tier": dropped_tier,
        "photo": c_photo,
        "worth": TIER_WORTH.get(dropped_tier, 200)
    }

    symbol = TIER_SYMBOLS.get(dropped_tier, "⚪️")
    c_name_esc = escape_html(c_name)

    text = (
        f"A {dropped_tier} {symbol} recruit {c_name_esc} has appeared...\n\n"
        f"Use /recruit to bring them into your crew"
    )

    if c_photo:
        try:
            await update.message.reply_photo(photo=c_photo, caption=text)
            return
        except Exception as e:
            logger.warning(f"Could not send card photo: {e}")

    await update.message.reply_text(text)

def get_tier_menu_view(page: int = 0, time_filter: str = "all") -> Tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🎴 <b>SORT ROSTER BY TIER</b>\n\n"
        f"Select a rarity tier below to filter your cards:"
    )

    keyboard_rows = [
        [
            InlineKeyboardButton("🌐 All Tiers", callback_data=f"mycards_p_0_All_{time_filter}"),
            InlineKeyboardButton("⚪ Mortal", callback_data=f"mycards_p_0_Mortal_{time_filter}")
        ],
        [
            InlineKeyboardButton("🟢 Awakened", callback_data=f"mycards_p_0_Awakened_{time_filter}"),
            InlineKeyboardButton("🔵 Transcendent", callback_data=f"mycards_p_0_Transcendent_{time_filter}")
        ],
        [
            InlineKeyboardButton("🟣 Celestial", callback_data=f"mycards_p_0_Celestial_{time_filter}"),
            InlineKeyboardButton("🔥 Primordial", callback_data=f"mycards_p_0_Primordial_{time_filter}")
        ],
        [
            InlineKeyboardButton("🔙 Back to Roster", callback_data=f"mycards_p_{page}_All_{time_filter}")
        ]
    ]
    return text, InlineKeyboardMarkup(keyboard_rows)

def get_time_menu_view(page: int = 0, tier_filter: str = "All") -> Tuple[str, InlineKeyboardMarkup]:
    text = (
        f"⏱️ <b>FILTER ROSTER BY STAY TIME</b>\n\n"
        f"Select card expiration time filter below:"
    )
    keyboard_rows = [
        [
            InlineKeyboardButton("⏱️ Less than 24 Hours (< 24h)", callback_data=f"mycards_p_0_{tier_filter}_lt24h")
        ],
        [
            InlineKeyboardButton("⏱️ More than 24 Hours (> 24h)", callback_data=f"mycards_p_0_{tier_filter}_gt24h")
        ],
        [
            InlineKeyboardButton("🌐 Show All Time", callback_data=f"mycards_p_0_{tier_filter}_all")
        ],
        [
            InlineKeyboardButton("🔙 Back to Roster", callback_data=f"mycards_p_{page}_{tier_filter}_all")
        ]
    ]
    return text, InlineKeyboardMarkup(keyboard_rows)

def get_user_roster_view(user_id: int, page: int = 0, tier_filter: str = "All", time_filter: str = "all") -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    clean_expired_cards()
    user_cards = [c for c in user_cards_db.values() if c.get("owner_id") == user_id]

    u_info = users_db.get(str(user_id), {})
    uname = escape_html(u_info.get("first_name", "User"))

    if not user_cards:
        return f"📜 <b>{uname}'s crew is empty!</b>\n\nType <code>/summon</code> in a group chat or <code>/daily</code> to recruit cards!", None

    filtered_cards = []
    now = time.time()
    for c in user_cards:
        t = c.get("tier", "Mortal").capitalize()
        if tier_filter != "All" and t.lower() != tier_filter.lower():
            continue

        if time_filter == "lt24h":
            if c.get("is_permanent", False):
                continue
            diff = c.get("expire_at", 0) - now
            if diff <= 0 or diff > 24 * 3600:
                continue
        elif time_filter == "gt24h":
            if c.get("is_permanent", False):
                pass
            else:
                diff = c.get("expire_at", 0) - now
                if diff <= 24 * 3600:
                    continue
        filtered_cards.append(c)

    tier_order = {"Primordial": 5, "Celestial": 4, "Transcendent": 3, "Awakened": 2, "Mortal": 1}
    filtered_cards.sort(key=lambda x: (tier_order.get(x.get("tier", "Mortal"), 0), x.get("expire_at", 0)), reverse=True)

    ITEMS_PER_PAGE = 8
    total_cards = len(filtered_cards)
    total_pages = max(1, (total_cards + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    page_cards = filtered_cards[page * ITEMS_PER_PAGE : (page + 1) * ITEMS_PER_PAGE]

    card_counts = {}
    for c in user_cards:
        cname_key = c.get("name", "").strip().lower()
        card_counts[cname_key] = card_counts.get(cname_key, 0) + 1

    tiered_page_cards: Dict[str, List[Dict]] = {}
    for c in page_cards:
        t_name = c.get("tier", "Mortal").capitalize()
        if t_name not in tiered_page_cards:
            tiered_page_cards[t_name] = []
        tiered_page_cards[t_name].append(c)

    tier_emblems = {
        "Primordial": "💠",
        "Celestial": "🔮",
        "Transcendent": "🔵",
        "Awakened": "🟢",
        "Mortal": "⚪"
    }

    tier_sections = []
    for t_name in ["Primordial", "Celestial", "Transcendent", "Awakened", "Mortal"]:
        c_list = tiered_page_cards.get(t_name, [])
        if not c_list:
            continue
        emblem = tier_emblems.get(t_name, "⚪")
        lines = [f"➥ <b>{t_name}</b> {emblem}"]
        for c in c_list:
            code = c.get("code", "0000")
            c_name = escape_html(c.get("name", "Cultivator"))
            cnt = card_counts.get(c.get("name", "").strip().lower(), 1)
            is_perm = c.get("is_permanent", False)
            perm_str = " 🏋️" if is_perm else ""
            lines.append(f"|{code}| <b>{c_name}</b> {cnt}X{perm_str}")
        tier_sections.append("\n".join(lines))

    cards_formatted_str = "\n\n──────────────────────\n\n".join(tier_sections) if tier_sections else "<i>No cards match current filter.</i>"

    filter_info = ""
    if tier_filter != "All":
        filter_info += f" | {tier_filter}"
    if time_filter == "lt24h":
        filter_info += " | <24h"
    elif time_filter == "gt24h":
        filter_info += " | >24h"

    header_line = f"<b>{uname}'s crew</b> ➥ <b>Page {page+1}/{total_pages}</b>{filter_info}"
    divider = "══════════════════════"

    text = (
        f"{divider}\n"
        f"{header_line}\n"
        f"{divider}\n\n"
        f"{cards_formatted_str}"
    )

    keyboard_rows = []

    sort_tier_label = f"Sort By Tier ({tier_filter})" if tier_filter != "All" else "Sort By Tier"
    time_label = "Sort By Time (<24h)" if time_filter == "lt24h" else ("Sort By Time (>24h)" if time_filter == "gt24h" else "Sort By Time")

    keyboard_rows.append([
        InlineKeyboardButton(sort_tier_label, callback_data=f"mycards_menu_tiers_{page}_{time_filter}"),
        InlineKeyboardButton(time_label, callback_data=f"mycards_menu_time_{page}_{tier_filter}")
    ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"mycards_p_{page-1}_{tier_filter}_{time_filter}"))
    nav_buttons.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="mycards_noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"mycards_p_{page+1}_{tier_filter}_{time_filter}"))

    keyboard_rows.append(nav_buttons)

    return text, InlineKeyboardMarkup(keyboard_rows)

def get_user_fav_card(user_id: int) -> Optional[Dict]:
    u_info = users_db.get(str(user_id), {})
    fav_code = u_info.get("fav_card_code")
    if fav_code:
        card = find_user_card_by_code(user_id, str(fav_code))
        if card and card.get("photo"):
            return card

    user_cards = [c for c in user_cards_db.values() if c.get("owner_id") == user_id and c.get("photo")]
    if not user_cards:
        return None

    tier_order = {"Primordial": 5, "Celestial": 4, "Transcendent": 3, "Awakened": 2, "Mortal": 1}
    user_cards.sort(key=lambda x: tier_order.get(x.get("tier", "Mortal"), 0), reverse=True)
    return user_cards[0]

async def favcard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sets a card from user's roster as favorite to display its photo in /mycards."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()

    args = context.args or []
    if not args:
        fav_card = get_user_fav_card(user.id)
        if fav_card:
            c_name = escape_html(fav_card.get("name"))
            t_emoji = TIER_EMOJIS.get(fav_card.get("tier"), fav_card.get("tier"))
            code = fav_card.get("code")
            await update.message.reply_text(
                f"⭐ <b>Your Current Favorite Card:</b> {t_emoji} - <b>{c_name}</b> (Code: <code>{code}</code>)\n\n"
                f"💡 To change your favorite card, type: <code>/favcard &lt;card_code_or_name&gt;</code>",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                "⚠️ <b>Usage:</b> <code>/favcard &lt;card_code_or_name&gt;</code> (e.g. <code>/favcard 4444</code>)\n\n"
                "<i>Setting a favorite card will display its photo whenever you check /mycards!</i>",
                parse_mode='HTML'
            )
        return

    target_query = " ".join(args).strip().lower()
    matched_card = find_user_card_by_code(user.id, target_query)

    if not matched_card:
        for c in user_cards_db.values():
            if c.get("owner_id") == user.id and target_query in c.get("name", "").strip().lower():
                matched_card = c
                break

    if not matched_card:
        await update.message.reply_text(f"❌ Card '<b>{escape_html(target_query)}</b>' not found in your active roster!", parse_mode='HTML')
        return

    u_info = users_db.get(str(user.id), {})
    u_info["fav_card_code"] = matched_card.get("code")
    save_users_data(users_db)

    c_name = escape_html(matched_card.get("name"))
    t_emoji = TIER_EMOJIS.get(matched_card.get("tier"), matched_card.get("tier"))
    code = matched_card.get("code")

    msg_text = (
        f"⭐ <b>FAVORITE CARD SET SUCCESSFULLY!</b> 🎉\n\n"
        f"🎴 <b>Favorite Card:</b> {t_emoji} - <b>{c_name}</b> (Code: <code>{code}</code>)\n\n"
        f"💡 This card's image will now appear at the top whenever you run <code>/mycards</code>!"
    )

    if matched_card.get("photo"):
        try:
            await update.message.reply_photo(photo=matched_card["photo"], caption=msg_text, parse_mode='HTML')
            return
        except Exception:
            pass

    await update.message.reply_text(msg_text, parse_mode='HTML')

async def mycards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lists user's active cards with favorite card image, descending tier order, pagination and filter buttons."""
    if not update.message or not update.message.from_user:
        return
    user = update.message.from_user
    track_user_data(user)

    text, keyboard = get_user_roster_view(user.id, page=0, tier_filter="All", time_filter="all")
    fav_card = get_user_fav_card(user.id)

    if fav_card and fav_card.get("photo"):
        try:
            if len(text) <= 1020:
                await update.message.reply_photo(photo=fav_card["photo"], caption=text, reply_markup=keyboard, parse_mode='HTML')
                return
            else:
                short_cap = f"⭐ <b>Favorite Card:</b> <b>{escape_html(fav_card.get('name'))}</b> [{fav_card.get('tier')}]"
                await update.message.reply_photo(photo=fav_card["photo"], caption=short_cap, parse_mode='HTML')
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
                return
        except Exception as e:
            logger.warning(f"Could not send favorite card photo in mycards: {e}")

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

async def mycards_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user = query.from_user
    user_id = user.id
    data = query.data or ""

    if data == "mycards_noop":
        await query.answer("Current page", show_alert=False)
        return

    if data.startswith("mycards_menu_tiers_"):
        parts = data.split("_")
        if len(parts) >= 5:
            try:
                page = int(parts[3])
                time_filter = parts[4]
            except ValueError:
                page = 0
                time_filter = "all"
            text, keyboard = get_tier_menu_view(page, time_filter)
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            except Exception as e:
                logger.warning(f"Could not update tier menu: {e}")
            await query.answer()
        return

    if data.startswith("mycards_menu_time_"):
        parts = data.split("_")
        if len(parts) >= 5:
            try:
                page = int(parts[3])
                tier_filter = parts[4]
            except ValueError:
                page = 0
                tier_filter = "All"
            text, keyboard = get_time_menu_view(page, tier_filter)
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            except Exception as e:
                logger.warning(f"Could not update time menu: {e}")
            await query.answer()
        return

    if data.startswith("mycards_p_"):
        parts = data.split("_")
        if len(parts) >= 5:
            try:
                page = int(parts[2])
                tier_filter = parts[3]
                time_filter = parts[4]
            except ValueError:
                page = 0
                tier_filter = "All"
                time_filter = "all"

            text, keyboard = get_user_roster_view(user_id, page, tier_filter, time_filter)
            try:
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            except Exception as e:
                logger.warning(f"Could not update mycards message: {e}")
            await query.answer()

async def showcard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays details of a specific card by 4-digit code (Name, Tier, Worth, Stay Time)."""
    if not update.message:
        return

    clean_expired_cards()
    args = context.args or []
    if not args:
        await update.message.reply_text("⚠️ Usage: <code>/showcard &lt;4-digit_code&gt;</code> (e.g. <code>/showcard 4444</code>)", parse_mode='HTML')
        return

    code = args[0].strip().upper()
    user = update.message.from_user
    card = None
    if user:
        card = find_user_card_by_code(user.id, code)

    if not card:
        for pool_c in cards_pool_db:
            if str(pool_c.get("code", "")).upper() == code:
                card = {
                    "name": pool_c.get("name"),
                    "tier": pool_c.get("tier"),
                    "worth": TIER_WORTH.get(pool_c.get("tier", "Mortal").capitalize(), 200),
                    "photo": pool_c.get("photo", ""),
                    "is_permanent": False
                }
                break

    if not card:
        await update.message.reply_text(f"❌ Card code <code>{code}</code> not found in database!", parse_mode='HTML')
        return

    c_name = escape_html(card.get("name", "Cultivator"))
    tier_name = card.get("tier", "Mortal")
    tier_emoji = TIER_EMOJIS.get(tier_name, tier_name)
    worth = get_card_worth(card)
    stay_time = format_time_left(card) if "expire_at" in card else "Global Card Pool"

    text = (
        f"⚔️ <b>Name:</b> {c_name}\n"
        f"✨ <b>Tier:</b> {tier_emoji}\n"
        f"💎 <b>Worth:</b> <code>{worth} SS</code>\n"
        f"⏳ <b>Stay Time in Crew:</b> <code>{stay_time}</code>"
    )

    if card.get("photo"):
        try:
            await update.message.reply_photo(photo=card["photo"], caption=text, parse_mode='HTML')
            return
        except Exception as e:
            logger.warning(f"Could not send card photo: {e}")

    await update.message.reply_text(text, parse_mode='HTML')

async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends an inline button with the Cultivation Mini App link."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    web_app_url = os.getenv("RENDER_EXTERNAL_URL") or "https://t-xcem.onrender.com"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("OPEN MINI APP", web_app=WebAppInfo(url=web_app_url))]
    ])

    await message.reply_text(
        "🌟 <b>CULTIVATION CULT TELEGRAM MINI APP</b> ⛩️\n\n"
        "Tap the button below to open your cultivation dashboard!",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

# ==================== CONNECT 4 GAME (/JOIN4) ====================
active_connect4_games: Dict[int, Dict] = {}  # chat_id -> game_state

def check_connect4_win(board, token) -> bool:
    rows = 6
    cols = 7
    # Horizontal
    for r in range(rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r][c+1] == token and board[r][c+2] == token and board[r][c+3] == token:
                return True
    # Vertical
    for r in range(rows - 3):
        for c in range(cols):
            if board[r][c] == token and board[r+1][c] == token and board[r+2][c] == token and board[r+3][c] == token:
                return True
    # Diagonal positive slope
    for r in range(rows - 3):
        for c in range(cols - 3):
            if board[r][c] == token and board[r+1][c+1] == token and board[r+2][c+2] == token and board[r+3][c+3] == token:
                return True
    # Diagonal negative slope
    for r in range(3, rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r-1][c+1] == token and board[r-2][c+2] == token and board[r-3][c+3] == token:
                return True
    return False

def is_board_full(board) -> bool:
    return all(board[0][c] != "⚪️" for c in range(7))

def drop_token(board, col, token) -> int:
    for r in range(5, -1, -1):
        if board[r][col] == "⚪️":
            board[r][col] = token
            return r
    return -1

def build_connect4_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    row_buttons = []
    for c in range(7):
        row_buttons.append(InlineKeyboardButton(str(c+1), callback_data=f"c4_col_{c}"))
    return InlineKeyboardMarkup([row_buttons])

def format_connect4_board(game: Dict) -> str:
    board = game["board"]
    board_str = "\n".join([" ".join(row) for row in board])
    p1 = escape_html(game["player1_name"])
    p2 = escape_html(game["player2_name"])
    turn_name = p1 if game["current_turn"] == game["player1_id"] else p2
    turn_token = "🔴" if game["current_turn"] == game["player1_id"] else "🟡"

    if game["player2_id"] == 0:
        pot_info = "🎁 <b>Reward on Win:</b> <code>+100 SS</code> (Free Play vs Bot)"
    else:
        pot_info = f"💰 <b>Pot / Bet:</b> <code>{game['bet'] * 2} SS</code> (<code>{game['bet']} SS</code> each)"

    return (
        f"🔴 <b>CONNECT 4 BATTLE</b> 🟡\n\n"
        f"👤 <b>Player 1 (🔴):</b> {p1}\n"
        f"👤 <b>Player 2 (🟡):</b> {p2}\n"
        f"{pot_info}\n\n"
        f"{board_str}\n\n"
        f"👉 <b>Turn:</b> {turn_token} <b>{escape_html(turn_name)}</b>\n"
        f"<i>Tap column numbers (1-7) below to drop your disk!</i>"
    )

async def join4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a Connect 4 game vs Bot (Free, +100 SS on win) or PvP against another user with SS bet."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    chat_id = update.message.chat.id

    args = context.args or []
    opponent_id = 0
    opponent_name = "Sylvi Bot 🤖"
    bet = 0

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        opp_user = update.message.reply_to_message.from_user
        if opp_user.id == user.id:
            await update.message.reply_text("❌ You cannot play against yourself!", parse_mode='HTML')
            return
        if opp_user.is_bot:
            await update.message.reply_text("❌ You cannot challenge bots as a user opponent!", parse_mode='HTML')
            return

        if not args or not args[0].isdigit():
            await update.message.reply_text(
                "⚠️ <b>Usage for PvP:</b> <code>/join4 &lt;bet_ss&gt;</code> (replying to opponent's message)\n"
                "<i>Example:</i> <code>/join4 50</code>",
                parse_mode='HTML'
            )
            return

        bet = int(args[0])
        if bet < 10:
            await update.message.reply_text("❌ Minimum bet for PvP Connect 4 is <b>10 SS</b>!", parse_mode='HTML')
            return

        eco1 = get_user_economy(user.id)
        if eco1["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ You don't have enough Spirit Stones! Your balance: <code>{eco1['spirit_stones']} SS</code>", parse_mode='HTML')
            return

        eco2 = get_user_economy(opp_user.id)
        if eco2["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ Opponent <b>{escape_html(opp_user.first_name)}</b> does not have enough Spirit Stones ({bet} SS required)!", parse_mode='HTML')
            return

        opponent_id = opp_user.id
        opponent_name = opp_user.first_name

        eco1["spirit_stones"] -= bet
        eco2["spirit_stones"] -= bet
        save_user_economy(user_economy_db)
    else:
        bet = 0

    game_state = {
        "chat_id": chat_id,
        "player1_id": user.id,
        "player1_name": user.first_name,
        "player2_id": opponent_id,
        "player2_name": opponent_name,
        "bet": bet,
        "board": [["⚪️" for _ in range(7)] for _ in range(6)],
        "current_turn": user.id,
        "status": "active"
    }
    active_connect4_games[chat_id] = game_state

    text = format_connect4_board(game_state)
    keyboard = build_connect4_keyboard(chat_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

async def connect4_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user = query.from_user
    chat_id = query.message.chat.id
    data = query.data or ""

    game = active_connect4_games.get(chat_id)
    if not game or game["status"] != "active":
        await query.answer("⚠️ No active Connect 4 game in this chat! Start one with /join4.", show_alert=True)
        return

    if user.id != game["current_turn"]:
        await query.answer("⏳ It's not your turn!", show_alert=True)
        return

    col = int(data.replace("c4_col_", ""))
    token = "🔴" if user.id == game["player1_id"] else "🟡"

    row = drop_token(game["board"], col, token)
    if row == -1:
        await query.answer("⚠️ Column is full! Choose another column.", show_alert=True)
        return

    await query.answer()

    if check_connect4_win(game["board"], token):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        winner_id = user.id
        winner_name = user.first_name

        if game["player2_id"] == 0:
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += 100
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - YOU BEAT THE BOT!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Reward Received:</b> <code>+100 Spirit Stones (SS)</code> 💎\n\n"
                f"<i>Type /join4 to play again!</i>"
            )
        else:
            pot = game["bet"] * 2
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += pot
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - WINNER!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Prize Won:</b> <code>+{pot} SS</code> 💎\n\n"
                f"<i>Type /join4 &lt;bet&gt; to play again!</i>"
            )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    if is_board_full(game["board"]):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        if game["player2_id"] != 0:
            eco1 = get_user_economy(game["player1_id"])
            eco1["spirit_stones"] += game["bet"]
            eco2 = get_user_economy(game["player2_id"])
            eco2["spirit_stones"] += game["bet"]
            save_user_economy(user_economy_db)

        board_str = "\n".join([" ".join(r) for r in game["board"]])
        refund_str = f" Both players' bets (<code>{game['bet']} SS</code> each) have been refunded!" if game["player2_id"] != 0 else ""
        text = (
            f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
            f"{board_str}\n\n"
            f"<i>Board is full.{refund_str}</i>"
        )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    next_turn = game["player2_id"] if game["current_turn"] == game["player1_id"] else game["player1_id"]
    game["current_turn"] = next_turn

    if game["status"] == "active" and game["current_turn"] == 0:
        bot_token = "🟡"
        valid_cols = [c for c in range(7) if game["board"][0][c] == "⚪️"]
        if valid_cols:
            # Loophole: Bot picks a random valid column and never defends or blocks player!
            bot_col = random.choice(valid_cols)
            drop_token(game["board"], bot_col, bot_token)

            if check_connect4_win(game["board"], bot_token):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤖 <b>CONNECT 4 - SYLVI BOT WINS!</b> 💀\n\n"
                    f"{board_str}\n\n"
                    f"👑 <b>Winner:</b> <b>Sylvi Bot 🤖</b>\n\n"
                    f"<i>Type /join4 to try again!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            if is_board_full(game["board"]):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
                    f"{board_str}\n\n"
                    f"<i>Game ended in a draw!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            game["current_turn"] = game["player1_id"]

    text = format_connect4_board(game)
    keyboard = build_connect4_keyboard(chat_id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        pass

async def logsgc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to set the target group chat ID for real-time activity logs."""
    global LOGS_GC_ID
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_owner(user.id):
        await message.reply_text("🚫 Only the bot owner can use this command!")
        return

    args = context.args or []
    if not args:
        chat_id = message.chat.id
        LOGS_GC_ID = chat_id
        save_logs_gc_id(chat_id)
        send_telegram_log(f"✅ Activity logs group set to this chat ID: <code>{chat_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"This chat (ID: <code>{chat_id}</code>) will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
        return

    try:
        target_id = int(args[0].strip())
        LOGS_GC_ID = target_id
        save_logs_gc_id(target_id)
        send_telegram_log(f"✅ Activity logs group set to chat ID: <code>{target_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"Target Chat ID: <code>{target_id}</code> will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
    except ValueError:
        await message.reply_text("⚠️ Invalid Chat ID! Must be a valid integer.", parse_mode='HTML')

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the Spirit Stones economy leaderboard (top players)."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    if not user_economy_db:
        await message.reply_text("🏆 <b>Leaderboard is currently empty!</b>", parse_mode='HTML')
        return

    sorted_econ = sorted(
        user_economy_db.values(),
        key=lambda x: x.get("spirit_stones", 0) if isinstance(x, dict) else 0,
        reverse=True
    )

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = []

    for idx, eco in enumerate(sorted_econ[:10]):
        if not isinstance(eco, dict):
            continue
        uid = eco.get("user_id")
        ss = eco.get("spirit_stones", 0)
        u_info = users_db.get(str(uid), {})
        uname = u_info.get("username")
        fname = u_info.get("first_name") or f"Cultivator {uid}"

        display_name = f"@{escape_html(str(uname))}" if uname else escape_html(str(fname))
        medal = medals[idx] if idx < len(medals) else f"•"

        lines.append(f"{medal} <b>{display_name}</b> — <code>{ss} SS</code> 💎")

    lb_str = "\n".join(lines)

    text = (
        f"🏆 <b>CULTIVATION SPIRIT STONES LEADERBOARD</b> 💎\n\n"
        f"{lb_str}\n\n"
        f"<i>💡 Earn more Spirit Stones via /daily, /summon, or betting in /mine!</i>"
    )

    await message.reply_text(text, parse_mode='HTML')

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Searches for a card by name using /search <name>, shows selection if multiple match, and displays card image with details and owner count."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()

    args = context.args or []
    if not args:
        await update.message.reply_text("⚠️ Usage: <code>/search &lt;card_name&gt;</code> (e.g. <code>/search cheon</code>)", parse_mode='HTML')
        return

    query_text = " ".join(args).strip().lower()

    matched_names = set()
    for c in cards_pool_db:
        cname = c.get("name", "").strip()
        if query_text in cname.lower():
            matched_names.add(cname)

    for c in user_cards_db.values():
        cname = c.get("name", "").strip()
        if query_text in cname.lower():
            matched_names.add(cname)

    unique_matches = sorted(list(matched_names))

    if not unique_matches:
        await update.message.reply_text(f"❌ No cards matching '<b>{escape_html(query_text)}</b>' found in database!", parse_mode='HTML')
        return

    if len(unique_matches) > 1:
        keyboard_buttons = []
        for idx, mname in enumerate(unique_matches[:10], start=1):
            keyboard_buttons.append([InlineKeyboardButton(f"{idx}. {mname}", callback_data=f"search_card_{idx-1}_{query_text}")])

        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        match_list_str = "\n".join([f"<b>{idx}.</b> {mname}" for idx, mname in enumerate(unique_matches[:10], start=1)])

        await update.message.reply_text(
            f"🔍 <b>Multiple cards found matching '{escape_html(query_text)}':</b>\n\n"
            f"{match_list_str}\n\n"
            f"👇 <i>Please select by tapping 1st, 2nd, 3rd, etc. below:</i>",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return

    selected_name = unique_matches[0]
    await send_card_search_result(update.message, selected_name)

async def send_card_search_result(message, card_name: str) -> None:
    card_obj = None
    for c in cards_pool_db:
        if c.get("name", "").strip().lower() == card_name.lower():
            card_obj = c
            break
    if not card_obj:
        for c in user_cards_db.values():
            if c.get("name", "").strip().lower() == card_name.lower():
                card_obj = c
                break

    if not card_obj:
        await message.reply_text(f"❌ Card '<b>{escape_html(card_name)}</b>' not found!", parse_mode='HTML')
        return

    c_name = escape_html(card_obj.get("name", "Cultivator"))
    tier = card_obj.get("tier", "Mortal")
    tier_emoji = TIER_EMOJIS.get(tier, tier)
    worth = get_card_worth(card_obj)
    photo = card_obj.get("photo", "")

    owners = [c for c in user_cards_db.values() if c.get("name", "").strip().lower() == card_name.lower()]
    owner_count = len(owners)
    owner_handles = []
    for o in owners:
        uid = o.get("owner_id")
        u_info = users_db.get(str(uid), {})
        handle = f"@{u_info.get('username')}" if u_info.get("username") else u_info.get("first_name", f"User {uid}")
        if handle not in owner_handles:
            owner_handles.append(handle)

    owners_str = ", ".join(owner_handles[:5]) if owner_handles else "None"
    if len(owner_handles) > 5:
        owners_str += f" and {len(owner_handles) - 5} others"

    caption = (
        f"⚔️ <b>Name:</b> {c_name}\n"
        f"✨ <b>Tier:</b> {tier_emoji}\n"
        f"💎 <b>Worth:</b> <code>{worth} SS</code>\n"
        f"👥 <b>Owned by:</b> <code>{owner_count} cultivators</code> ({owners_str})"
    )

    fallback_img = "https://images.unsplash.com/photo-1534447677768-be436bb09401?w=600&auto=format&fit=crop&q=80"
    photo_url = photo if (photo and (photo.startswith("http://") or photo.startswith("https://"))) else fallback_img

    if photo and not photo.startswith("http"):
        try:
            await message.reply_photo(photo=photo, caption=caption, parse_mode='HTML')
            return
        except Exception:
            pass

    try:
        await message.reply_photo(photo=photo_url, caption=caption, parse_mode='HTML')
    except Exception:
        await message.reply_text(caption, parse_mode='HTML')

async def search_card_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    data = query.data or ""
    await query.answer()

    if data.startswith("search_card_"):
        parts = data.split("_")
        if len(parts) >= 4:
            try:
                idx = int(parts[2])
                query_text = "_".join(parts[3:]).strip().lower()
            except ValueError:
                return

            matched_names = set()
            for c in cards_pool_db:
                cname = c.get("name", "").strip()
                if query_text in cname.lower():
                    matched_names.add(cname)
            for c in user_cards_db.values():
                cname = c.get("name", "").strip()
                if query_text in cname.lower():
                    matched_names.add(cname)

            unique_matches = sorted(list(matched_names))
            if 0 <= idx < len(unique_matches):
                selected_name = unique_matches[idx]
                await send_card_search_result(query.message, selected_name)
                try:
                    await query.message.delete()
                except Exception:
                    pass

async def daily_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Claims daily free 500 Spirit Stones (SS) + 1 Mortal Card."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    eco = get_user_economy(user.id)
    now = time.time()

    last_daily = eco.get("last_daily", 0.0)
    cooldown = 24 * 3600
    time_passed = now - last_daily

    if time_passed < cooldown:
        remaining_sec = cooldown - time_passed
        hours = int(remaining_sec // 3600)
        mins = int((remaining_sec % 3600) // 60)
        await update.message.reply_text(
            f"⏳ <b>Daily reward already claimed!</b>\n\nPlease wait <code>{hours}h {mins}m</code> before claiming again.",
            parse_mode='HTML'
        )
        return

    eco["spirit_stones"] += 500
    eco["last_daily"] = now
    save_user_economy(user_economy_db)

    mortal_card = create_card_instance(user.id, forced_tier="Mortal")
    send_telegram_log(f"💎 Daily claimed by {user.first_name} (ID: <code>{user.id}</code>): +500 SS & Mortal card ({mortal_card['name']})")
    uname = escape_html(user.first_name)
    c_name = escape_html(mortal_card["name"])
    code = mortal_card["code"]
    new_ss = eco["spirit_stones"]

    await update.message.reply_text(
        f"💎 <b>DAILY CULTIVATION REWARD CLAIMED!</b>\n\n"
        f"👤 <b>Cultivator:</b> {uname}\n"
        f"✨ <b>Reward Received:</b> <code>+500 Spirit Stones (SS)</code> 💎\n"
        f"🎴 <b>Bonus Card:</b> ⚪ Mortal - <b>{c_name}</b> (Code: <code>{code}</code>)\n\n"
        f"💰 <b>Total SS Balance:</b> <code>{new_ss} SS</code>",
        parse_mode='HTML'
    )

PILL_KEYWORD_MAP = {
    "1": "1", "qi": "1", "qigathering": "1",
    "2": "2", "jade": "2", "jadespirit": "2",
    "3": "3", "heavenly": "3", "heavenlylife": "3",
    "4": "4", "emperor": "4", "emperors": "4", "longevity": "4",
    "5": "5", "eternal": "5", "eternallife": "5"
}

async def pillshop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays Pill Shop catalog with inline buy buttons."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    eco = get_user_economy(user.id)
    ss_bal = eco["spirit_stones"]

    shop_lines = []
    keyboard_buttons = []

    for num, item in PILL_CATALOG.items():
        cnt = eco.get("pills", {}).get(item["name"], 0)
        shop_lines.append(f"<b>{num}. {item['name']}</b> (Owned: {cnt})\n   • Effect: <code>{item['desc']}</code>\n   • Price: <code>{item['price']} SS</code>")
        keyboard_buttons.append([InlineKeyboardButton(f"🛒 Buy {item['name']} ({item['price']} SS)", callback_data=f"buy_pill_{num}")])

    shop_text = "\n\n".join(shop_lines)
    keyboard = InlineKeyboardMarkup(keyboard_buttons)

    text = (
        f"💊 <b>CULTIVATION PILL SHOP</b>\n\n"
        f"💰 <b>Your Spirit Stones Balance:</b> <code>{ss_bal} SS</code>\n\n"
        f"{shop_text}\n\n"
        f"👇 <b>Tap an inline button below to buy pills instantly!</b>\n"
        f"<i>(Or use command: <code>/buy &lt;pill&gt; [qty]</code>)</i>"
    )

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

async def buypill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Purchases pills using Spirit Stones."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    eco = get_user_economy(user.id)

    args = context.args or []
    if not args:
        await update.message.reply_text("⚠️ Usage: <code>/buy &lt;pill_name_or_number&gt; [qty]</code> (e.g. <code>/buy qi 2</code>, <code>/buy eternal</code>)", parse_mode='HTML')
        return

    query_key = args[0].strip().lower()
    qty = 1
    if len(args) > 1 and args[1].isdigit():
        qty = max(1, int(args[1]))

    mapped_num = PILL_KEYWORD_MAP.get(query_key)
    target_item = None
    if mapped_num and mapped_num in PILL_CATALOG:
        target_item = PILL_CATALOG[mapped_num]
    else:
        for num, item in PILL_CATALOG.items():
            if query_key == num or query_key in item["name"].lower():
                target_item = item
                break

    if not target_item:
        await update.message.reply_text("❌ Invalid pill selection! Available keywords: <code>qi, jade, heavenly, emperor, longevity, eternal</code> or numbers <code>1-5</code>.", parse_mode='HTML')
        return

    pill_name = target_item["name"]
    unit_price = target_item["price"]
    total_cost = unit_price * qty

    if eco["spirit_stones"] < total_cost:
        await update.message.reply_text(
            f"❌ <b>Insufficient Spirit Stones!</b>\n\n"
            f"• Total Cost: <code>{total_cost} SS</code>\n"
            f"• Your Balance: <code>{eco['spirit_stones']} SS</code>",
            parse_mode='HTML'
        )
        return

    eco["spirit_stones"] -= total_cost
    eco["pills"][pill_name] = eco["pills"].get(pill_name, 0) + qty
    save_user_economy(user_economy_db)

    send_telegram_log(
        f"🛒 <b>PILL PURCHASED IN SHOP</b>\n"
        f"👤 <b>User:</b> {escape_html(user.first_name)} (@{user.username or 'none'}, ID: <code>{user.id}</code>)\n"
        f"💊 <b>Bought:</b> {qty}x <b>{pill_name}</b>\n"
        f"💰 <b>Total Paid:</b> <code>{total_cost} SS</code> | <b>SS Balance:</b> <code>{eco['spirit_stones']} SS</code>"
    )

    await update.message.reply_text(
        f"✅ <b>PURCHASE SUCCESSFUL!</b>\n\n"
        f"💊 <b>Bought:</b> {qty}x <b>{pill_name}</b>\n"
        f"💰 <b>Cost Paid:</b> <code>{total_cost} SS</code>\n"
        f"💎 <b>Remaining Balance:</b> <code>{eco['spirit_stones']} SS</code>\n\n"
        f"💡 Feed card using <code>/cultivate &lt;card_code&gt; {pill_name}</code>",
        parse_mode='HTML'
    )

async def inventory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows user Spirit Stones balance and owned pills stock."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    eco = get_user_economy(user.id)
    uname = escape_html(user.first_name)

    ss_bal = eco["spirit_stones"]
    pills = eco.get("pills", {})

    pill_lines = []
    for num, item in PILL_CATALOG.items():
        pname = item["name"]
        count = pills.get(pname, 0)
        pill_lines.append(f"• <b>{pname}:</b> <code>{count}</code> owned")

    pills_str = "\n".join(pill_lines)

    await update.message.reply_text(
        f"🎒 <b>{uname}'s Cultivation Inventory:</b>\n\n"
        f"💎 <b>Spirit Stones (SS):</b> <code>{ss_bal} SS</code>\n\n"
        f"💊 <b>Pills Stock:</b>\n{pills_str}\n\n"
        f"💡 Type <code>/pillshop</code> to buy more pills!",
        parse_mode='HTML'
    )

# ==================== MINES BETTING GAME ====================
active_mines_games: Dict[int, Dict] = {}  # user_id -> game_state

def calculate_mines_multiplier(total_tiles: int, mines: int, opened: int) -> float:
    if opened <= 0:
        return 1.00
    safe_tiles = total_tiles - mines
    if opened > safe_tiles:
        opened = safe_tiles
    prod = 1.0
    for i in range(opened):
        prod *= (total_tiles - i) / (safe_tiles - i)
    return round(prod * 0.97, 2)

async def mine_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a Mines betting game where user bets Spirit Stones and avoids hidden mines."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    eco = get_user_economy(user.id)

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ <b>Usage:</b> <code>/mine &lt;bet_ss&gt; &lt;mines_count&gt;</code>\n"
            "• <b>Min Bet:</b> <code>50 SS</code>\n"
            "• <b>Mines Range:</b> <code>1 to 24</code> (out of 25 tiles)\n"
            "<i>Example:</i> <code>/mine 50 3</code> (Bet 50 SS with 3 mines)",
            parse_mode='HTML'
        )
        return

    try:
        bet = int(args[0])
        mines_count = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Bet amount and mines count must be valid numbers!", parse_mode='HTML')
        return

    if bet < 50:
        await update.message.reply_text("❌ Minimum bet for Mines is <b>50 SS</b>!", parse_mode='HTML')
        return

    if eco["spirit_stones"] < bet:
        await update.message.reply_text(
            f"❌ <b>Insufficient Spirit Stones!</b>\n"
            f"• Your Balance: <code>{eco['spirit_stones']} SS</code>\n"
            f"• Attempted Bet: <code>{bet} SS</code>",
            parse_mode='HTML'
        )
        return

    if mines_count < 1 or mines_count > 24:
        await update.message.reply_text("❌ Mines count must be between <b>1 and 24</b>!", parse_mode='HTML')
        return

    # Deduct bet
    eco["spirit_stones"] -= bet
    save_user_economy(user_economy_db)

    # Initialize game state
    mine_positions = set(random.sample(range(25), mines_count))
    game_state = {
        "user_id": user.id,
        "bet": bet,
        "mines_count": mines_count,
        "mine_positions": mine_positions,
        "revealed": set(),
        "tiles_opened": 0,
        "game_over": False,
        "cashed_out": False
    }
    active_mines_games[user.id] = game_state

    # Build 5x5 keyboard
    keyboard = build_mines_keyboard(game_state)
    uname = escape_html(user.first_name)

    text = (
        f"💣 <b>MINES GAME STARTED: {uname}</b> 💎\n\n"
        f"💰 <b>Bet:</b> <code>{bet} SS</code>\n"
        f"⛏️ <b>Mines:</b> <code>{mines_count}</code> | <b>Safe Tiles:</b> <code>{25 - mines_count}</code>\n"
        f"📈 <b>Current Multiplier:</b> <code>1.00x</code>\n"
        f"💎 <b>Potential Winnings:</b> <code>{bet} SS</code>\n\n"
        f"👇 <i>Tap tiles below to uncover safe diamonds and increase your multiplier!</i>"
    )

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

def build_mines_keyboard(game_state: Dict) -> InlineKeyboardMarkup:
    revealed = game_state["revealed"]
    game_over = game_state["game_over"]
    mine_positions = game_state["mine_positions"]

    buttons = []
    for row in range(5):
        row_buttons = []
        for col in range(5):
            idx = row * 5 + col
            if idx in revealed:
                label = "💎"
                callback = "mines_noop"
            elif game_over:
                if idx in mine_positions:
                    label = "💣"
                else:
                    label = "🟩"
                callback = "mines_noop"
            else:
                label = "🟦"
                callback = f"mines_click_{idx}"
            row_buttons.append(InlineKeyboardButton(label, callback_data=callback))
        buttons.append(row_buttons)

    if not game_over:
        current_mult = calculate_mines_multiplier(25, game_state["mines_count"], game_state["tiles_opened"])
        potential = int(game_state["bet"] * current_mult)
        if game_state["tiles_opened"] > 0:
            buttons.append([
                InlineKeyboardButton(f"💰 Cash Out ({potential} SS @ {current_mult}x)", callback_data="mines_cashout"),
                InlineKeyboardButton("🛑 Surrender", callback_data="mines_surrender")
            ])
        else:
            buttons.append([
                InlineKeyboardButton("🛑 Surrender / Quit", callback_data="mines_surrender")
            ])
    else:
        buttons.append([
            InlineKeyboardButton("🎮 Play Again (/mine)", callback_data="mines_playagain")
        ])

    return InlineKeyboardMarkup(buttons)

async def mines_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user = query.from_user
    user_id = user.id
    data = query.data or ""
    game_state = active_mines_games.get(user_id)

    if not game_state:
        await query.answer("⚠️ No active Mines game found! Type /mine <bet> <mines> to start a new game.", show_alert=True)
        return

    uname = escape_html(user.first_name)

    if data == "mines_noop":
        await query.answer("Tile already opened or game ended!", show_alert=False)
        return

    if data == "mines_playagain":
        active_mines_games.pop(user_id, None)
        await query.edit_message_text(
            "🎮 <b>Mines Game Closed.</b>\nType <code>/mine &lt;bet_ss&gt; &lt;mines_count&gt;</code> to play again!",
            parse_mode='HTML'
        )
        return

    if game_state["game_over"]:
        await query.answer("Game is already over!", show_alert=True)
        return

    if data.startswith("mines_click_"):
        idx = int(data.replace("mines_click_", ""))
        if idx in game_state["revealed"]:
            await query.answer("Tile already revealed!", show_alert=True)
            return

        if idx in game_state["mine_positions"]:
            game_state["game_over"] = True
            bet = game_state["bet"]
            active_mines_games.pop(user_id, None)

            keyboard = build_mines_keyboard(game_state)
            text = (
                f"💣 <b>BOOM! YOU HIT A MINE!</b> 💀\n\n"
                f"👤 <b>Cultivator:</b> {uname}\n"
                f"💰 <b>Bet Lost:</b> <code>-{bet} SS</code>\n"
                f"⛏️ <b>Mines:</b> <code>{game_state['mines_count']}</code> | <b>Tiles Opened:</b> <code>{game_state['tiles_opened']}</code>\n\n"
                f"<i>Better luck next time! Type /mine to try again.</i>"
            )
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            await query.answer("BOOM! You hit a mine!", show_alert=True)
            return

        game_state["revealed"].add(idx)
        game_state["tiles_opened"] += 1
        tiles_opened = game_state["tiles_opened"]
        safe_total = 25 - game_state["mines_count"]

        current_mult = calculate_mines_multiplier(25, game_state["mines_count"], tiles_opened)
        potential = int(game_state["bet"] * current_mult)

        if tiles_opened == safe_total:
            game_state["game_over"] = True
            winnings = potential
            eco = get_user_economy(user_id)
            eco["spirit_stones"] += winnings
            save_user_economy(user_economy_db)
            active_mines_games.pop(user_id, None)

            keyboard = build_mines_keyboard(game_state)
            text = (
                f"🏆 <b>JACKPOT! ALL SAFE TILES CLEARED!</b> 🔥\n\n"
                f"👤 <b>Cultivator:</b> {uname}\n"
                f"💰 <b>Bet:</b> <code>{game_state['bet']} SS</code>\n"
                f"📈 <b>Multiplier:</b> <code>{current_mult}x</code>\n"
                f"💎 <b>Winnings Won:</b> <code>+{winnings} SS</code>\n"
                f"💰 <b>New Balance:</b> <code>{eco['spirit_stones']} SS</code>"
            )
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
            await query.answer(f"Jackpot! Won {winnings} SS!", show_alert=True)
            return

        keyboard = build_mines_keyboard(game_state)
        text = (
            f"💣 <b>MINES GAME IN PROGRESS: {uname}</b> 💎\n\n"
            f"💰 <b>Bet:</b> <code>{game_state['bet']} SS</code>\n"
            f"⛏️ <b>Mines:</b> <code>{game_state['mines_count']}</code> | <b>Opened:</b> <code>{tiles_opened}/{safe_total}</code>\n"
            f"📈 <b>Current Multiplier:</b> <code>{current_mult}x</code>\n"
            f"💎 <b>Potential Winnings:</b> <code>{potential} SS</code>\n\n"
            f"👇 <i>Keep tapping safe tiles or Cash Out!</i>"
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        await query.answer(f"Safe! Multiplier: {current_mult}x", show_alert=False)
        return

    if data == "mines_cashout":
        if game_state["tiles_opened"] <= 0:
            await query.answer("Open at least 1 safe tile before cashing out!", show_alert=True)
            return

        game_state["game_over"] = True
        current_mult = calculate_mines_multiplier(25, game_state["mines_count"], game_state["tiles_opened"])
        winnings = int(game_state["bet"] * current_mult)

        eco = get_user_economy(user_id)
        eco["spirit_stones"] += winnings
        save_user_economy(user_economy_db)
        active_mines_games.pop(user_id, None)

        keyboard = build_mines_keyboard(game_state)
        text = (
            f"💰 <b>CASH OUT SUCCESSFUL!</b> 🎉\n\n"
            f"👤 <b>Cultivator:</b> {uname}\n"
            f"📈 <b>Multiplier:</b> <code>{current_mult}x</code>\n"
            f"💎 <b>Winnings Claimed:</b> <code>+{winnings} SS</code>\n"
            f"💰 <b>New Balance:</b> <code>{eco['spirit_stones']} SS</code>\n\n"
            f"<i>Type /mine to play again!</i>"
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        await query.answer(f"Cashed out successfully: {winnings} SS!", show_alert=True)
        return

    if data == "mines_surrender":
        game_state["game_over"] = True
        bet = game_state["bet"]
        active_mines_games.pop(user_id, None)

        keyboard = build_mines_keyboard(game_state)
        text = (
            f"🛑 <b>GAME SURRENDERED!</b>\n\n"
            f"👤 <b>Cultivator:</b> {uname}\n"
            f"💰 <b>Bet Forfeited:</b> <code>-{bet} SS</code>\n\n"
            f"<i>Type /mine to start a new game.</i>"
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        await query.answer("Game surrendered.", show_alert=True)
        return

async def myprofile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays user's cultivation game profile, Spirit Stones, roster count, unique cards collected, and completion %."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()
    eco = get_user_economy(user.id)
    uname = escape_html(user.first_name)
    ss_bal = eco["spirit_stones"]

    user_cards = [c for c in user_cards_db.values() if c.get("owner_id") == user.id]
    total_roster_cards = len(user_cards)

    total_pool_count = len(cards_pool_db)
    owned_card_names = {c.get("name", "").strip().lower() for c in user_cards}
    unique_owned_count = 0
    if total_pool_count > 0:
        unique_owned_count = sum(1 for b in cards_pool_db if b.get("name", "").strip().lower() in owned_card_names)
    else:
        unique_owned_count = len(owned_card_names)

    completion_pct = (unique_owned_count / total_pool_count * 100) if total_pool_count > 0 else 0.0

    filled_blocks = int(completion_pct // 10)
    filled_blocks = max(0, min(10, filled_blocks))
    progress_bar = "█" * filled_blocks + "░" * (10 - filled_blocks)

    tiers = ["Mortal", "Awakened", "Transcendent", "Celestial", "Primordial"]
    tier_lines = []
    for tier in tiers:
        pool_tier_cards = [b for b in cards_pool_db if b.get("tier", "").lower() == tier.lower()]
        pool_tier_count = len(pool_tier_cards)

        owned_tier_names = {c.get("name", "").strip().lower() for c in user_cards if c.get("tier", "").lower() == tier.lower()}
        owned_tier_count = sum(1 for b in pool_tier_cards if b.get("name", "").strip().lower() in owned_tier_names) if pool_tier_count > 0 else len(owned_tier_names)

        symbol = TIER_SYMBOLS.get(tier, "⚪️")
        tier_lines.append(f"• {symbol} {tier}: <code>{owned_tier_count}/{pool_tier_count}</code>")

    tier_str = "\n".join(tier_lines)

    text = (
        f"📊 <b>CULTIVATOR GAME STATUS: {uname}</b>\n\n"
        f"💎 <b>Spirit Stones (SS):</b> <code>{ss_bal} SS</code>\n"
        f"🎴 <b>Roster Cards Owned:</b> <code>{total_roster_cards} cards</code>\n"
        f"🎯 <b>Unique Cards Collected:</b> <code>{unique_owned_count}/{total_pool_count} base cards</code>\n"
        f"🏆 <b>Game Completion:</b> <b>{completion_pct:.1f}%</b>\n"
        f"[{progress_bar}]\n\n"
        f"🏷️ <b>Collection Progress by Tier:</b>\n"
        f"{tier_str}\n\n"
        f"💡 <i>Tip: Recruited cards increase your completion %! When new cards are added to the pool, completion % adjusts automatically.</i>"
    )

    await update.message.reply_text(text, parse_mode='HTML')

async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends an inline button with the Cultivation Mini App link."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    web_app_url = os.getenv("RENDER_EXTERNAL_URL") or "https://t-xcem.onrender.com"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("OPEN MINI APP", web_app=WebAppInfo(url=web_app_url))]
    ])

    await message.reply_text(
        "🌟 <b>CULTIVATION CULT TELEGRAM MINI APP</b> ⛩️\n\n"
        "Tap the button below to open your cultivation dashboard!",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

# ==================== CONNECT 4 GAME (/JOIN4) ====================
active_connect4_games: Dict[int, Dict] = {}  # chat_id -> game_state

def check_connect4_win(board, token) -> bool:
    rows = 6
    cols = 7
    # Horizontal
    for r in range(rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r][c+1] == token and board[r][c+2] == token and board[r][c+3] == token:
                return True
    # Vertical
    for r in range(rows - 3):
        for c in range(cols):
            if board[r][c] == token and board[r+1][c] == token and board[r+2][c] == token and board[r+3][c] == token:
                return True
    # Diagonal positive slope
    for r in range(rows - 3):
        for c in range(cols - 3):
            if board[r][c] == token and board[r+1][c+1] == token and board[r+2][c+2] == token and board[r+3][c+3] == token:
                return True
    # Diagonal negative slope
    for r in range(3, rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r-1][c+1] == token and board[r-2][c+2] == token and board[r-3][c+3] == token:
                return True
    return False

def is_board_full(board) -> bool:
    return all(board[0][c] != "⚪️" for c in range(7))

def drop_token(board, col, token) -> int:
    for r in range(5, -1, -1):
        if board[r][col] == "⚪️":
            board[r][col] = token
            return r
    return -1

def build_connect4_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    row_buttons = []
    for c in range(7):
        row_buttons.append(InlineKeyboardButton(str(c+1), callback_data=f"c4_col_{c}"))
    return InlineKeyboardMarkup([row_buttons])

def format_connect4_board(game: Dict) -> str:
    board = game["board"]
    board_str = "\n".join([" ".join(row) for row in board])
    p1 = escape_html(game["player1_name"])
    p2 = escape_html(game["player2_name"])
    turn_name = p1 if game["current_turn"] == game["player1_id"] else p2
    turn_token = "🔴" if game["current_turn"] == game["player1_id"] else "🟡"

    if game["player2_id"] == 0:
        pot_info = "🎁 <b>Reward on Win:</b> <code>+100 SS</code> (Free Play vs Bot)"
    else:
        pot_info = f"💰 <b>Pot / Bet:</b> <code>{game['bet'] * 2} SS</code> (<code>{game['bet']} SS</code> each)"

    return (
        f"🔴 <b>CONNECT 4 BATTLE</b> 🟡\n\n"
        f"👤 <b>Player 1 (🔴):</b> {p1}\n"
        f"👤 <b>Player 2 (🟡):</b> {p2}\n"
        f"{pot_info}\n\n"
        f"{board_str}\n\n"
        f"👉 <b>Turn:</b> {turn_token} <b>{escape_html(turn_name)}</b>\n"
        f"<i>Tap column numbers (1-7) below to drop your disk!</i>"
    )

async def join4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a Connect 4 game vs Bot (Free, +100 SS on win) or PvP against another user with SS bet."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    chat_id = update.message.chat.id

    args = context.args or []
    opponent_id = 0
    opponent_name = "Sylvi Bot 🤖"
    bet = 0

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        opp_user = update.message.reply_to_message.from_user
        if opp_user.id == user.id:
            await update.message.reply_text("❌ You cannot play against yourself!", parse_mode='HTML')
            return
        if opp_user.is_bot:
            await update.message.reply_text("❌ You cannot challenge bots as a user opponent!", parse_mode='HTML')
            return

        if not args or not args[0].isdigit():
            await update.message.reply_text(
                "⚠️ <b>Usage for PvP:</b> <code>/join4 &lt;bet_ss&gt;</code> (replying to opponent's message)\n"
                "<i>Example:</i> <code>/join4 50</code>",
                parse_mode='HTML'
            )
            return

        bet = int(args[0])
        if bet < 10:
            await update.message.reply_text("❌ Minimum bet for PvP Connect 4 is <b>10 SS</b>!", parse_mode='HTML')
            return

        eco1 = get_user_economy(user.id)
        if eco1["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ You don't have enough Spirit Stones! Your balance: <code>{eco1['spirit_stones']} SS</code>", parse_mode='HTML')
            return

        eco2 = get_user_economy(opp_user.id)
        if eco2["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ Opponent <b>{escape_html(opp_user.first_name)}</b> does not have enough Spirit Stones ({bet} SS required)!", parse_mode='HTML')
            return

        opponent_id = opp_user.id
        opponent_name = opp_user.first_name

        eco1["spirit_stones"] -= bet
        eco2["spirit_stones"] -= bet
        save_user_economy(user_economy_db)
    else:
        bet = 0

    game_state = {
        "chat_id": chat_id,
        "player1_id": user.id,
        "player1_name": user.first_name,
        "player2_id": opponent_id,
        "player2_name": opponent_name,
        "bet": bet,
        "board": [["⚪️" for _ in range(7)] for _ in range(6)],
        "current_turn": user.id,
        "status": "active"
    }
    active_connect4_games[chat_id] = game_state

    text = format_connect4_board(game_state)
    keyboard = build_connect4_keyboard(chat_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

async def connect4_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user = query.from_user
    chat_id = query.message.chat.id
    data = query.data or ""

    game = active_connect4_games.get(chat_id)
    if not game or game["status"] != "active":
        await query.answer("⚠️ No active Connect 4 game in this chat! Start one with /join4.", show_alert=True)
        return

    if user.id != game["current_turn"]:
        await query.answer("⏳ It's not your turn!", show_alert=True)
        return

    col = int(data.replace("c4_col_", ""))
    token = "🔴" if user.id == game["player1_id"] else "🟡"

    row = drop_token(game["board"], col, token)
    if row == -1:
        await query.answer("⚠️ Column is full! Choose another column.", show_alert=True)
        return

    await query.answer()

    if check_connect4_win(game["board"], token):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        winner_id = user.id
        winner_name = user.first_name

        if game["player2_id"] == 0:
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += 100
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - YOU BEAT THE BOT!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Reward Received:</b> <code>+100 Spirit Stones (SS)</code> 💎\n\n"
                f"<i>Type /join4 to play again!</i>"
            )
        else:
            pot = game["bet"] * 2
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += pot
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - WINNER!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Prize Won:</b> <code>+{pot} SS</code> 💎\n\n"
                f"<i>Type /join4 &lt;bet&gt; to play again!</i>"
            )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    if is_board_full(game["board"]):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        if game["player2_id"] != 0:
            eco1 = get_user_economy(game["player1_id"])
            eco1["spirit_stones"] += game["bet"]
            eco2 = get_user_economy(game["player2_id"])
            eco2["spirit_stones"] += game["bet"]
            save_user_economy(user_economy_db)

        board_str = "\n".join([" ".join(r) for r in game["board"]])
        refund_str = f" Both players' bets (<code>{game['bet']} SS</code> each) have been refunded!" if game["player2_id"] != 0 else ""
        text = (
            f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
            f"{board_str}\n\n"
            f"<i>Board is full.{refund_str}</i>"
        )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    next_turn = game["player2_id"] if game["current_turn"] == game["player1_id"] else game["player1_id"]
    game["current_turn"] = next_turn

    if game["status"] == "active" and game["current_turn"] == 0:
        bot_token = "🟡"
        valid_cols = [c for c in range(7) if game["board"][0][c] == "⚪️"]
        if valid_cols:
            # Loophole: Bot picks a random valid column and never defends or blocks player!
            bot_col = random.choice(valid_cols)
            drop_token(game["board"], bot_col, bot_token)

            if check_connect4_win(game["board"], bot_token):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤖 <b>CONNECT 4 - SYLVI BOT WINS!</b> 💀\n\n"
                    f"{board_str}\n\n"
                    f"👑 <b>Winner:</b> <b>Sylvi Bot 🤖</b>\n\n"
                    f"<i>Type /join4 to try again!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            if is_board_full(game["board"]):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
                    f"{board_str}\n\n"
                    f"<i>Game ended in a draw!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            game["current_turn"] = game["player1_id"]

    text = format_connect4_board(game)
    keyboard = build_connect4_keyboard(chat_id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        pass

async def logsgc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to set the target group chat ID for real-time activity logs."""
    global LOGS_GC_ID
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_owner(user.id):
        await message.reply_text("🚫 Only the bot owner can use this command!")
        return

    args = context.args or []
    if not args:
        chat_id = message.chat.id
        LOGS_GC_ID = chat_id
        save_logs_gc_id(chat_id)
        send_telegram_log(f"✅ Activity logs group set to this chat ID: <code>{chat_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"This chat (ID: <code>{chat_id}</code>) will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
        return

    try:
        target_id = int(args[0].strip())
        LOGS_GC_ID = target_id
        save_logs_gc_id(target_id)
        send_telegram_log(f"✅ Activity logs group set to chat ID: <code>{target_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"Target Chat ID: <code>{target_id}</code> will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
    except ValueError:
        await message.reply_text("⚠️ Invalid Chat ID! Must be a valid integer.", parse_mode='HTML')

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the Spirit Stones economy leaderboard (top players)."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    if not user_economy_db:
        await message.reply_text("🏆 <b>Leaderboard is currently empty!</b>", parse_mode='HTML')
        return

    sorted_econ = sorted(
        user_economy_db.values(),
        key=lambda x: x.get("spirit_stones", 0) if isinstance(x, dict) else 0,
        reverse=True
    )

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = []

    for idx, eco in enumerate(sorted_econ[:10]):
        if not isinstance(eco, dict):
            continue
        uid = eco.get("user_id")
        ss = eco.get("spirit_stones", 0)
        u_info = users_db.get(str(uid), {})
        uname = u_info.get("username")
        fname = u_info.get("first_name") or f"Cultivator {uid}"

        display_name = f"@{escape_html(str(uname))}" if uname else escape_html(str(fname))
        medal = medals[idx] if idx < len(medals) else f"•"

        lines.append(f"{medal} <b>{display_name}</b> — <code>{ss} SS</code> 💎")

    lb_str = "\n".join(lines)

    text = (
        f"🏆 <b>CULTIVATION SPIRIT STONES LEADERBOARD</b> 💎\n\n"
        f"{lb_str}\n\n"
        f"<i>💡 Earn more Spirit Stones via /daily, /summon, or betting in /mine!</i>"
    )

    await message.reply_text(text, parse_mode='HTML')

async def cultivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Feeds pill to card to extend stay duration or make permanent, with inline selection."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()
    eco = get_user_economy(user.id)

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/cultivate &lt;card_code&gt; [pill_name_or_num]</code>\n"
            "Example: <code>/cultivate 1234</code> (opens pill selection buttons!)",
            parse_mode='HTML'
        )
        return

    code = args[0].strip().upper()
    card = user_cards_db.get(code)

    if not card or card.get("owner_id") != user.id:
        await update.message.reply_text(f"❌ Card code <code>{code}</code> not found in your roster!", parse_mode='HTML')
        return

    pills = eco.get("pills", {})

    # IF NO SECOND ARGUMENT GIVEN, SHOW INLINE SELECTION BUTTONS FOR ALL OWNED PILLS!
    if len(args) == 1:
        owned_pills = []
        keyboard_buttons = []

        for num, item in PILL_CATALOG.items():
            pname = item["name"]
            cnt = pills.get(pname, 0)
            if cnt > 0:
                owned_pills.append((num, item, cnt))
                keyboard_buttons.append([InlineKeyboardButton(f"💊 {pname} ({cnt} in stock)", callback_data=f"cult_{code}_{num}")])

        if not owned_pills:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Open Pill Shop", callback_data="open_pillshop")]])
            await update.message.reply_text(
                f"❌ <b>You don't have any pills in your inventory!</b>\n\n"
                f"Tap the button below to buy pills from the shop:",
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            return

        keyboard_buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cult_cancel")])
        keyboard = InlineKeyboardMarkup(keyboard_buttons)

        c_name = escape_html(card.get("name"))
        tier_str = TIER_EMOJIS.get(card.get("tier"), card.get("tier"))
        current_stay = format_time_left(card)

        await update.message.reply_text(
            f"✨ <b>SELECT A PILL TO CULTIVATE CARD</b>\n\n"
            f"🎴 <b>Card:</b> {tier_str} - <b>{c_name}</b> (Code: <code>{code}</code>)\n"
            f"⏳ <b>Current Stay Limit:</b> <code>{current_stay}</code>\n\n"
            f"👇 <b>Select a pill below to feed to this card:</b>",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return

    # Direct pill provided via text
    pill_query = " ".join(args[1:]).strip().lower()
    target_item = None
    for num, item in PILL_CATALOG.items():
        if pill_query == num or pill_query in item["name"].lower():
            target_item = item
            break

    if not target_item:
        await update.message.reply_text("❌ Invalid pill specified! Check <code>/inventory</code> for your owned pills.", parse_mode='HTML')
        return

    pill_name = target_item["name"]
    owned_count = pills.get(pill_name, 0)

    if owned_count <= 0:
        await update.message.reply_text(
            f"❌ You don't have any <b>{pill_name}</b> in your inventory!\nType <code>/pillshop</code> to buy pills.",
            parse_mode='HTML'
        )
        return

    pills[pill_name] -= 1
    save_user_economy(user_economy_db)

    now = time.time()
    if target_item["seconds"] == -1:
        card["is_permanent"] = True
    else:
        current_expire = max(now, card.get("expire_at", now))
        card["expire_at"] = current_expire + target_item["seconds"]

    save_user_cards(user_cards_db)

    c_name = escape_html(card.get("name"))
    tier_str = TIER_EMOJIS.get(card.get("tier"), card.get("tier"))
    new_time_left = format_time_left(card)

    await update.message.reply_text(
        f"✨ <b>CULTIVATION SUCCESSFUL!</b> 🧘‍♂️\n\n"
        f"🎴 <b>Card:</b> {tier_str} - <b>{c_name}</b> (Code: <code>{code}</code>)\n"
        f"💊 <b>Fed Pill:</b> {pill_name}\n"
        f"⏳ <b>New Stay Limit:</b> <code>{new_time_left}</code>",
        parse_mode='HTML'
    )

owner_addcard_sessions: Dict[int, Dict] = {}  # user_id -> {"step": str, "photo": str, "tier": str}
owner_rarity_sessions: Dict[int, Dict] = {}   # user_id -> {"action": str, "step": str, "target_card": Dict}

async def rarity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to change card details or adjust tier spawn rates / rarity percentages."""
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if not is_owner(user_id):
        await update.message.reply_text("🚫 Only the bot owner can use this command!")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼️ Change Card Image", callback_data="rarity_act_image"),
            InlineKeyboardButton("✏️ Change Card Name", callback_data="rarity_act_name")
        ],
        [
            InlineKeyboardButton("⚖️ Adjust Tier Spawn Rates / Rarity", callback_data="rarity_act_rates")
        ],
        [
            InlineKeyboardButton("❌ Cancel Wizard", callback_data="rarity_act_cancel")
        ]
    ])

    p_p = TIER_PROBABILITIES.get("Primordial", 0.01)
    p_c = TIER_PROBABILITIES.get("Celestial", 0.20)
    p_t = TIER_PROBABILITIES.get("Transcendent", 1.20)
    p_a = TIER_PROBABILITIES.get("Awakened", 3.50)
    p_m = max(0.0, round(100.0 - (p_p + p_c + p_t + p_a), 4))

    await update.message.reply_text(
        "✨ <b>GLOBAL CARD EDIT & RARITY WIZARD (/rarity)</b>\n\n"
        "📊 <b>Current Active Tier Spawn Rates:</b>\n"
        f"• 🔥 Primordial: <code>{p_p}%</code>\n"
        f"• 🟣 Celestial: <code>{p_c}%</code>\n"
        f"• 🔵 Transcendent: <code>{p_t}%</code>\n"
        f"• 🟢 Awakened: <code>{p_a}%</code>\n"
        f"• ⚪ Mortal: <code>{p_m}%</code> (remainder)\n\n"
        "Select what you want to edit:",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

async def rarity_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user_id = query.from_user.id
    if not is_owner(user_id):
        await query.answer("🚫 Only the bot owner can use this feature!", show_alert=True)
        return

    data = query.data or ""
    await query.answer()

    if data == "rarity_act_cancel":
        owner_rarity_sessions.pop(user_id, None)
        await query.edit_message_text("❌ <b>Global Card Edit Wizard Canceled!</b>", parse_mode='HTML')
        return

    if data == "rarity_act_image":
        owner_rarity_sessions[user_id] = {
            "action": "change_image",
            "step": "awaiting_card_code"
        }
        await query.edit_message_text(
            "🖼️ <b>CARD IMAGE UPDATE (Step 1/2)</b>\n\n"
            "Please send/type the <b>4-digit Card Code</b> (or Card Pool ID) of the card whose image you want to change:",
            parse_mode='HTML'
        )
        return

    if data == "rarity_act_name":
        owner_rarity_sessions[user_id] = {
            "action": "change_name",
            "step": "awaiting_card_code"
        }
        await query.edit_message_text(
            "✏️ <b>CARD NAME UPDATE (Step 1/2)</b>\n\n"
            "Please send/type the <b>4-digit Card Code</b> (or Card Pool ID) of the card whose name you want to change:",
            parse_mode='HTML'
        )
        return

    if data == "rarity_act_rates":
        p_p = TIER_PROBABILITIES.get("Primordial", 0.01)
        p_c = TIER_PROBABILITIES.get("Celestial", 0.20)
        p_t = TIER_PROBABILITIES.get("Transcendent", 1.20)
        p_a = TIER_PROBABILITIES.get("Awakened", 3.50)

        rate_buttons = [
            [InlineKeyboardButton(f"🔥 Primordial ({p_p}%)", callback_data="rarity_rate_Primordial")],
            [InlineKeyboardButton(f"🟣 Celestial ({p_c}%)", callback_data="rarity_rate_Celestial")],
            [InlineKeyboardButton(f"🔵 Transcendent ({p_t}%)", callback_data="rarity_rate_Transcendent")],
            [InlineKeyboardButton(f"🟢 Awakened ({p_a}%)", callback_data="rarity_rate_Awakened")],
            [InlineKeyboardButton("🔄 Reset to Default Rates", callback_data="rarity_rate_reset")],
            [InlineKeyboardButton("❌ Cancel", callback_data="rarity_act_cancel")]
        ]
        keyboard = InlineKeyboardMarkup(rate_buttons)
        await query.edit_message_text(
            "⚖️ <b>ADJUST TIER SPAWN RATES & RARITY</b>\n\n"
            "Select a tier below to change its spawn probability (e.g. make Primordial/Celestial rarer or common):",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return

    if data == "rarity_rate_reset":
        TIER_PROBABILITIES.clear()
        TIER_PROBABILITIES.update(DEFAULT_TIER_PROBABILITIES)
        save_tier_probabilities(TIER_PROBABILITIES)
        owner_rarity_sessions.pop(user_id, None)

        send_telegram_log("🔄 <b>Tier Spawn Rates Reset to Default</b> by owner.")
        await query.edit_message_text("✅ <b>Tier Spawn Rates Reset to Default Values!</b>", parse_mode='HTML')
        return

    if data.startswith("rarity_rate_"):
        target_tier = data.replace("rarity_rate_", "")
        current_val = TIER_PROBABILITIES.get(target_tier, 0.01)

        owner_rarity_sessions[user_id] = {
            "action": "change_rate",
            "step": "awaiting_rate_val",
            "target_tier": target_tier
        }

        symbol = TIER_SYMBOLS.get(target_tier, target_tier)
        await query.edit_message_text(
            f"⚖️ <b>CHANGE SPAWN RATE FOR {symbol} {target_tier.upper()}</b>\n\n"
            f"• <b>Current Rate:</b> <code>{current_val}%</code>\n\n"
            f"✏️ Please type and send the <b>New Percentage Value</b> in chat:\n"
            f"<i>Examples:</i>\n"
            f"• <code>0.001</code> (Ultra rare - 1 in 100,000)\n"
            f"• <code>0.005</code> (Extremely rare - 1 in 20,000)\n"
            f"• <code>0.01</code> (Standard Primordial rate)",
            parse_mode='HTML'
        )
        return

async def finalize_rarity_image_update(update: Update, user_id: int, base_card: Dict, new_photo: str) -> None:
    if not base_card:
        return
    target_code = base_card.get("code")
    target_name_lower = base_card.get("name", "").strip().lower()

    # 1. Update in cards_pool_db
    base_card["photo"] = new_photo
    save_cards_pool(cards_pool_db)

    # 2. Update ALL matching user card instances in user_cards_db
    updated_count = 0
    for key, u_card in user_cards_db.items():
        if str(u_card.get("code", "")).upper() == str(target_code).upper() or u_card.get("name", "").strip().lower() == target_name_lower:
            u_card["photo"] = new_photo
            updated_count += 1

    save_user_cards(user_cards_db)
    owner_rarity_sessions.pop(user_id, None)

    c_name = escape_html(base_card.get("name"))
    t_emoji = TIER_EMOJIS.get(base_card.get("tier"), base_card.get("tier"))

    send_telegram_log(
        f"🖼️ <b>GLOBAL CARD IMAGE UPDATED</b>\n"
        f"🎴 <b>Card:</b> {t_emoji} - <b>{c_name}</b> (Code: <code>{target_code}</code>)\n"
        f"👥 <b>Updated in Rosters:</b> <code>{updated_count} user card instances</code>"
    )

    msg = (
        f"✅ <b>CARD IMAGE UPDATED GLOBALLY!</b> 🎉\n\n"
        f"⚔️ <b>Card:</b> {t_emoji} - <b>{c_name}</b>\n"
        f"🔑 <b>Code:</b> <code>{target_code}</code>\n"
        f"👥 <b>Updated across:</b> <code>{updated_count} user rosters</code>\n\n"
        f"<i>This new image is now live everywhere across the entire bot!</i>"
    )
    try:
        await update.message.reply_photo(photo=new_photo, caption=msg, parse_mode='HTML')
    except Exception:
        await update.message.reply_text(msg, parse_mode='HTML')

async def finalize_rarity_name_update(update: Update, user_id: int, base_card: Dict, new_name: str) -> None:
    if not base_card:
        return
    target_code = base_card.get("code")
    old_name_lower = base_card.get("name", "").strip().lower()

    # 1. Update in cards_pool_db
    base_card["name"] = new_name
    save_cards_pool(cards_pool_db)

    # 2. Update ALL matching user card instances in user_cards_db
    updated_count = 0
    for key, u_card in user_cards_db.items():
        if str(u_card.get("code", "")).upper() == str(target_code).upper() or u_card.get("name", "").strip().lower() == old_name_lower:
            u_card["name"] = new_name
            updated_count += 1

    save_user_cards(user_cards_db)
    owner_rarity_sessions.pop(user_id, None)

    c_name_esc = escape_html(new_name)
    t_emoji = TIER_EMOJIS.get(base_card.get("tier"), base_card.get("tier"))

    send_telegram_log(
        f"✏️ <b>GLOBAL CARD NAME UPDATED</b>\n"
        f"🎴 <b>New Name:</b> {t_emoji} - <b>{c_name_esc}</b> (Code: <code>{target_code}</code>)\n"
        f"👥 <b>Updated in Rosters:</b> <code>{updated_count} user card instances</code>"
    )

    msg = (
        f"✅ <b>CARD NAME UPDATED GLOBALLY!</b> 🎉\n\n"
        f"⚔️ <b>New Name:</b> {t_emoji} - <b>{c_name_esc}</b>\n"
        f"🔑 <b>Code:</b> <code>{target_code}</code>\n"
        f"👥 <b>Updated across:</b> <code>{updated_count} user rosters</code>\n\n"
        f"<i>This new name is now live everywhere across the entire bot!</i>"
    )
    await update.message.reply_text(msg, parse_mode='HTML')

async def finalize_add_card(update: Update, user_id: int, tier: str, char_name: str, photo: str) -> None:
    used_codes = {str(c.get("code")) for c in cards_pool_db if c.get("code")}
    while True:
        card_code = ''.join(random.choices(string.digits, k=4))
        if card_code not in used_codes:
            break

    card_id = f"CARD_{int(time.time())}"
    new_base_card = {
        "id": card_id,
        "code": card_code,
        "name": char_name,
        "tier": tier,
        "photo": photo
    }

    cards_pool_db.append(new_base_card)
    save_cards_pool(cards_pool_db)
    send_telegram_log(f"➕ New Card Added to Pool by owner: <b>{char_name}</b> [{tier}] (ID: <code>{card_id}</code>, Code: <code>{card_code}</code>)")

    tier_emoji = TIER_EMOJIS.get(tier, tier)

    text = (
        f"✅ <b>NEW CARD ADDED TO GLOBAL POOL!</b> 🎉\n\n"
        f"⚔️ <b>Character Name:</b> <b>{escape_html(char_name)}</b>\n"
        f"✨ <b>Tier:</b> {tier_emoji}\n"
        f"🆔 <b>Card Pool ID:</b> <code>{card_id}</code>\n"
        f"🔑 <b>Assigned Code:</b> <code>{card_code}</code>\n\n"
        f"Total Base Pool Size: <code>{len(cards_pool_db)} cards</code>\n"
        f"💡 <i>This card is now in the global pool and can be recruited by players when summoned/dropped!</i>"
    )

    if photo and update.message:
        try:
            await update.message.reply_photo(photo=photo, caption=text, parse_mode='HTML')
            return
        except Exception as e:
            logger.warning(f"Could not send added card photo: {e}")

    if update.message:
        await update.message.reply_text(text, parse_mode='HTML')

async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends an inline button with the Cultivation Mini App link."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    web_app_url = os.getenv("RENDER_EXTERNAL_URL") or "https://t-xcem.onrender.com"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("OPEN MINI APP", web_app=WebAppInfo(url=web_app_url))]
    ])

    await message.reply_text(
        "🌟 <b>CULTIVATION CULT TELEGRAM MINI APP</b> ⛩️\n\n"
        "Tap the button below to open your cultivation dashboard!",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

# ==================== CONNECT 4 GAME (/JOIN4) ====================
active_connect4_games: Dict[int, Dict] = {}  # chat_id -> game_state

def check_connect4_win(board, token) -> bool:
    rows = 6
    cols = 7
    # Horizontal
    for r in range(rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r][c+1] == token and board[r][c+2] == token and board[r][c+3] == token:
                return True
    # Vertical
    for r in range(rows - 3):
        for c in range(cols):
            if board[r][c] == token and board[r+1][c] == token and board[r+2][c] == token and board[r+3][c] == token:
                return True
    # Diagonal positive slope
    for r in range(rows - 3):
        for c in range(cols - 3):
            if board[r][c] == token and board[r+1][c+1] == token and board[r+2][c+2] == token and board[r+3][c+3] == token:
                return True
    # Diagonal negative slope
    for r in range(3, rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r-1][c+1] == token and board[r-2][c+2] == token and board[r-3][c+3] == token:
                return True
    return False

def is_board_full(board) -> bool:
    return all(board[0][c] != "⚪️" for c in range(7))

def drop_token(board, col, token) -> int:
    for r in range(5, -1, -1):
        if board[r][col] == "⚪️":
            board[r][col] = token
            return r
    return -1

def build_connect4_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    row_buttons = []
    for c in range(7):
        row_buttons.append(InlineKeyboardButton(str(c+1), callback_data=f"c4_col_{c}"))
    return InlineKeyboardMarkup([row_buttons])

def format_connect4_board(game: Dict) -> str:
    board = game["board"]
    board_str = "\n".join([" ".join(row) for row in board])
    p1 = escape_html(game["player1_name"])
    p2 = escape_html(game["player2_name"])
    turn_name = p1 if game["current_turn"] == game["player1_id"] else p2
    turn_token = "🔴" if game["current_turn"] == game["player1_id"] else "🟡"

    if game["player2_id"] == 0:
        pot_info = "🎁 <b>Reward on Win:</b> <code>+100 SS</code> (Free Play vs Bot)"
    else:
        pot_info = f"💰 <b>Pot / Bet:</b> <code>{game['bet'] * 2} SS</code> (<code>{game['bet']} SS</code> each)"

    return (
        f"🔴 <b>CONNECT 4 BATTLE</b> 🟡\n\n"
        f"👤 <b>Player 1 (🔴):</b> {p1}\n"
        f"👤 <b>Player 2 (🟡):</b> {p2}\n"
        f"{pot_info}\n\n"
        f"{board_str}\n\n"
        f"👉 <b>Turn:</b> {turn_token} <b>{escape_html(turn_name)}</b>\n"
        f"<i>Tap column numbers (1-7) below to drop your disk!</i>"
    )

async def join4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a Connect 4 game vs Bot (Free, +100 SS on win) or PvP against another user with SS bet."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    chat_id = update.message.chat.id

    args = context.args or []
    opponent_id = 0
    opponent_name = "Sylvi Bot 🤖"
    bet = 0

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        opp_user = update.message.reply_to_message.from_user
        if opp_user.id == user.id:
            await update.message.reply_text("❌ You cannot play against yourself!", parse_mode='HTML')
            return
        if opp_user.is_bot:
            await update.message.reply_text("❌ You cannot challenge bots as a user opponent!", parse_mode='HTML')
            return

        if not args or not args[0].isdigit():
            await update.message.reply_text(
                "⚠️ <b>Usage for PvP:</b> <code>/join4 &lt;bet_ss&gt;</code> (replying to opponent's message)\n"
                "<i>Example:</i> <code>/join4 50</code>",
                parse_mode='HTML'
            )
            return

        bet = int(args[0])
        if bet < 10:
            await update.message.reply_text("❌ Minimum bet for PvP Connect 4 is <b>10 SS</b>!", parse_mode='HTML')
            return

        eco1 = get_user_economy(user.id)
        if eco1["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ You don't have enough Spirit Stones! Your balance: <code>{eco1['spirit_stones']} SS</code>", parse_mode='HTML')
            return

        eco2 = get_user_economy(opp_user.id)
        if eco2["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ Opponent <b>{escape_html(opp_user.first_name)}</b> does not have enough Spirit Stones ({bet} SS required)!", parse_mode='HTML')
            return

        opponent_id = opp_user.id
        opponent_name = opp_user.first_name

        eco1["spirit_stones"] -= bet
        eco2["spirit_stones"] -= bet
        save_user_economy(user_economy_db)
    else:
        bet = 0

    game_state = {
        "chat_id": chat_id,
        "player1_id": user.id,
        "player1_name": user.first_name,
        "player2_id": opponent_id,
        "player2_name": opponent_name,
        "bet": bet,
        "board": [["⚪️" for _ in range(7)] for _ in range(6)],
        "current_turn": user.id,
        "status": "active"
    }
    active_connect4_games[chat_id] = game_state

    text = format_connect4_board(game_state)
    keyboard = build_connect4_keyboard(chat_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

async def connect4_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user = query.from_user
    chat_id = query.message.chat.id
    data = query.data or ""

    game = active_connect4_games.get(chat_id)
    if not game or game["status"] != "active":
        await query.answer("⚠️ No active Connect 4 game in this chat! Start one with /join4.", show_alert=True)
        return

    if user.id != game["current_turn"]:
        await query.answer("⏳ It's not your turn!", show_alert=True)
        return

    col = int(data.replace("c4_col_", ""))
    token = "🔴" if user.id == game["player1_id"] else "🟡"

    row = drop_token(game["board"], col, token)
    if row == -1:
        await query.answer("⚠️ Column is full! Choose another column.", show_alert=True)
        return

    await query.answer()

    if check_connect4_win(game["board"], token):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        winner_id = user.id
        winner_name = user.first_name

        if game["player2_id"] == 0:
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += 100
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - YOU BEAT THE BOT!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Reward Received:</b> <code>+100 Spirit Stones (SS)</code> 💎\n\n"
                f"<i>Type /join4 to play again!</i>"
            )
        else:
            pot = game["bet"] * 2
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += pot
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - WINNER!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Prize Won:</b> <code>+{pot} SS</code> 💎\n\n"
                f"<i>Type /join4 &lt;bet&gt; to play again!</i>"
            )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    if is_board_full(game["board"]):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        if game["player2_id"] != 0:
            eco1 = get_user_economy(game["player1_id"])
            eco1["spirit_stones"] += game["bet"]
            eco2 = get_user_economy(game["player2_id"])
            eco2["spirit_stones"] += game["bet"]
            save_user_economy(user_economy_db)

        board_str = "\n".join([" ".join(r) for r in game["board"]])
        refund_str = f" Both players' bets (<code>{game['bet']} SS</code> each) have been refunded!" if game["player2_id"] != 0 else ""
        text = (
            f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
            f"{board_str}\n\n"
            f"<i>Board is full.{refund_str}</i>"
        )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    next_turn = game["player2_id"] if game["current_turn"] == game["player1_id"] else game["player1_id"]
    game["current_turn"] = next_turn

    if game["status"] == "active" and game["current_turn"] == 0:
        bot_token = "🟡"
        valid_cols = [c for c in range(7) if game["board"][0][c] == "⚪️"]
        if valid_cols:
            # Loophole: Bot picks a random valid column and never defends or blocks player!
            bot_col = random.choice(valid_cols)
            drop_token(game["board"], bot_col, bot_token)

            if check_connect4_win(game["board"], bot_token):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤖 <b>CONNECT 4 - SYLVI BOT WINS!</b> 💀\n\n"
                    f"{board_str}\n\n"
                    f"👑 <b>Winner:</b> <b>Sylvi Bot 🤖</b>\n\n"
                    f"<i>Type /join4 to try again!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            if is_board_full(game["board"]):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
                    f"{board_str}\n\n"
                    f"<i>Game ended in a draw!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            game["current_turn"] = game["player1_id"]

    text = format_connect4_board(game)
    keyboard = build_connect4_keyboard(chat_id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        pass

async def logsgc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to set the target group chat ID for real-time activity logs."""
    global LOGS_GC_ID
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_owner(user.id):
        await message.reply_text("🚫 Only the bot owner can use this command!")
        return

    args = context.args or []
    if not args:
        chat_id = message.chat.id
        LOGS_GC_ID = chat_id
        save_logs_gc_id(chat_id)
        send_telegram_log(f"✅ Activity logs group set to this chat ID: <code>{chat_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"This chat (ID: <code>{chat_id}</code>) will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
        return

    try:
        target_id = int(args[0].strip())
        LOGS_GC_ID = target_id
        save_logs_gc_id(target_id)
        send_telegram_log(f"✅ Activity logs group set to chat ID: <code>{target_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"Target Chat ID: <code>{target_id}</code> will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
    except ValueError:
        await message.reply_text("⚠️ Invalid Chat ID! Must be a valid integer.", parse_mode='HTML')

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the Spirit Stones economy leaderboard (top players)."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    if not user_economy_db:
        await message.reply_text("🏆 <b>Leaderboard is currently empty!</b>", parse_mode='HTML')
        return

    sorted_econ = sorted(
        user_economy_db.values(),
        key=lambda x: x.get("spirit_stones", 0) if isinstance(x, dict) else 0,
        reverse=True
    )

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = []

    for idx, eco in enumerate(sorted_econ[:10]):
        if not isinstance(eco, dict):
            continue
        uid = eco.get("user_id")
        ss = eco.get("spirit_stones", 0)
        u_info = users_db.get(str(uid), {})
        uname = u_info.get("username")
        fname = u_info.get("first_name") or f"Cultivator {uid}"

        display_name = f"@{escape_html(str(uname))}" if uname else escape_html(str(fname))
        medal = medals[idx] if idx < len(medals) else f"•"

        lines.append(f"{medal} <b>{display_name}</b> — <code>{ss} SS</code> 💎")

    lb_str = "\n".join(lines)

    text = (
        f"🏆 <b>CULTIVATION SPIRIT STONES LEADERBOARD</b> 💎\n\n"
        f"{lb_str}\n\n"
        f"<i>💡 Earn more Spirit Stones via /daily, /summon, or betting in /mine!</i>"
    )

    await message.reply_text(text, parse_mode='HTML')

async def addcard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to add a new character card using interactive wizard or direct args."""
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if not is_owner(user_id):
        await update.message.reply_text("🚫 Only the bot owner can use this command!")
        return

    args = context.args or []

    # If direct arguments supplied (e.g. /addcard Mortal Xiao Yan), process immediately!
    if len(args) >= 2:
        raw_tier = args[0].strip().capitalize()
        valid_tiers = ["Mortal", "Awakened", "Transcendent", "Celestial", "Primordial"]

        matched_tier = None
        for vt in valid_tiers:
            if raw_tier.lower() == vt.lower():
                matched_tier = vt
                break

        if not matched_tier:
            await update.message.reply_text(
                f"⚠️ Invalid Tier <code>{raw_tier}</code>!\nValid tiers: <code>Mortal, Awakened, Transcendent, Celestial, Primordial</code>",
                parse_mode='HTML'
            )
            return

        char_name = " ".join(args[1:]).strip()
        photo_file_id = ""

        if update.message.photo:
            photo_file_id = update.message.photo[-1].file_id
        elif update.message.reply_to_message and update.message.reply_to_message.photo:
            photo_file_id = update.message.reply_to_message.photo[-1].file_id

        await finalize_add_card(update, user_id, matched_tier, char_name, photo_file_id)
        return

    # NO ARGUMENTS: Start Interactive Step-by-Step Wizard!
    owner_addcard_sessions[user_id] = {
        "step": "awaiting_photo",
        "photo": "",
        "tier": ""
    }

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏩ Skip Photo (No Image)", callback_data="addcard_skipphoto")],
        [InlineKeyboardButton("❌ Cancel Wizard", callback_data="addcard_cancel")]
    ])

    await update.message.reply_text(
        "✨ <b>CARD CREATION WIZARD (Step 1/3)</b>\n\n"
        "📷 <b>1st: Please Send/Upload the Card Photo/Image</b> now.\n"
        "<i>Or tap 'Skip Photo' below to create a text-only card.</i>",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

async def addcard_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user_id = query.from_user.id
    if not is_owner(user_id):
        await query.answer("🚫 Only the bot owner can use this feature!", show_alert=True)
        return

    data = query.data or ""
    await query.answer()

    if data == "addcard_cancel":
        owner_addcard_sessions.pop(user_id, None)
        await query.edit_message_text("❌ <b>Card Creation Wizard Canceled!</b>", parse_mode='HTML')
        return

    session = owner_addcard_sessions.get(user_id)
    if not session:
        await query.edit_message_text("⚠️ <b>Session expired or not found. Type <code>/addcard</code> to start again!</b>", parse_mode='HTML')
        return

    # Handle Skip Photo button
    if data == "addcard_skipphoto":
        session["photo"] = ""
        session["step"] = "awaiting_tier"

        tier_buttons = [
            [
                InlineKeyboardButton("⚪ Mortal", callback_data="addcard_tier_Mortal"),
                InlineKeyboardButton("🟢 Awakened", callback_data="addcard_tier_Awakened")
            ],
            [
                InlineKeyboardButton("🔵 Transcendent", callback_data="addcard_tier_Transcendent"),
                InlineKeyboardButton("🟣 Celestial", callback_data="addcard_tier_Celestial")
            ],
            [
                InlineKeyboardButton("🔥 Primordial", callback_data="addcard_tier_Primordial")
            ],
            [
                InlineKeyboardButton("❌ Cancel Wizard", callback_data="addcard_cancel")
            ]
        ]
        keyboard = InlineKeyboardMarkup(tier_buttons)

        await query.edit_message_text(
            "✨ <b>CARD CREATION WIZARD (Step 2/3)</b>\n\n"
            "📷 <b>Photo:</b> Skipped\n\n"
            "🏷️ <b>2nd: Select Card Tier below:</b>",
            reply_markup=keyboard,
            parse_mode='HTML'
        )
        return

    # Handle Tier Selection buttons
    if data.startswith("addcard_tier_"):
        selected_tier = data.replace("addcard_tier_", "")
        session["tier"] = selected_tier
        session["step"] = "awaiting_name"

        symbol = TIER_SYMBOLS.get(selected_tier, "⚪️")

        await query.edit_message_text(
            f"✨ <b>CARD CREATION WIZARD (Step 3/3)</b>\n\n"
            f"📷 <b>Photo:</b> {'Attached ✅' if session['photo'] else 'None'}\n"
            f"🏷️ <b>Selected Tier:</b> {symbol} {selected_tier}\n\n"
            f"✏️ <b>3rd: Now type and send the Character Name in chat:</b>\n"
            f"<i>(e.g. Xiao Yan, Tanjiro, Gojo Satoru)</i>",
            parse_mode='HTML'
        )
        return

async def pill_cultivate_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user = query.from_user
    user_id = user.id
    track_user_data(user)
    data = query.data or ""
    await query.answer()

    if data == "cult_cancel":
        await query.edit_message_text("❌ <b>Cultivation Canceled!</b>", parse_mode='HTML')
        return

    if data == "open_pillshop":
        eco = get_user_economy(user_id)
        ss_bal = eco["spirit_stones"]

        shop_lines = []
        keyboard_buttons = []

        for num, item in PILL_CATALOG.items():
            cnt = eco.get("pills", {}).get(item["name"], 0)
            shop_lines.append(f"<b>{num}. {item['name']}</b> (Owned: {cnt})\n   • Effect: <code>{item['desc']}</code>\n   • Price: <code>{item['price']} SS</code>")
            keyboard_buttons.append([InlineKeyboardButton(f"🛒 Buy {item['name']} ({item['price']} SS)", callback_data=f"buy_pill_{num}")])

        shop_text = "\n\n".join(shop_lines)
        keyboard = InlineKeyboardMarkup(keyboard_buttons)

        text = (
            f"💊 <b>CULTIVATION PILL SHOP</b>\n\n"
            f"💰 <b>Your Spirit Stones Balance:</b> <code>{ss_bal} SS</code>\n\n"
            f"{shop_text}\n\n"
            f"👇 <b>Tap an inline button below to buy pills instantly!</b>"
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        return

    # Handle Pill Shop inline buy buttons
    if data.startswith("buy_pill_"):
        num = data.replace("buy_pill_", "")
        item = PILL_CATALOG.get(num)
        if not item:
            await query.answer("❌ Invalid pill!", show_alert=True)
            return

        eco = get_user_economy(user_id)
        price = item["price"]
        pill_name = item["name"]

        if eco["spirit_stones"] < price:
            await query.answer(f"❌ Insufficient Spirit Stones! Required: {price} SS | Balance: {eco['spirit_stones']} SS", show_alert=True)
            return

        eco["spirit_stones"] -= price
        eco["pills"][pill_name] = eco["pills"].get(pill_name, 0) + 1
        save_user_economy(user_economy_db)

        # Refresh shop message with updated SS balance
        ss_bal = eco["spirit_stones"]
        shop_lines = []
        keyboard_buttons = []

        for p_num, p_item in PILL_CATALOG.items():
            cnt = eco["pills"].get(p_item["name"], 0)
            shop_lines.append(f"<b>{p_num}. {p_item['name']}</b> (Owned: {cnt})\n   • Effect: <code>{p_item['desc']}</code>\n   • Price: <code>{p_item['price']} SS</code>")
            keyboard_buttons.append([InlineKeyboardButton(f"🛒 Buy {p_item['name']} ({p_item['price']} SS)", callback_data=f"buy_pill_{p_num}")])

        shop_text = "\n\n".join(shop_lines)
        keyboard = InlineKeyboardMarkup(keyboard_buttons)

        text = (
            f"✅ <b>Bought 1x {pill_name}!</b> 🎉\n\n"
            f"💊 <b>CULTIVATION PILL SHOP</b>\n\n"
            f"💰 <b>Your Spirit Stones Balance:</b> <code>{ss_bal} SS</code>\n\n"
            f"{shop_text}\n\n"
            f"👇 <b>Tap an inline button below to buy pills instantly!</b>"
        )
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        return

    # Handle Cultivate Pill inline feed buttons (cult_<code>_<num>)
    if data.startswith("cult_"):
        parts = data.split("_")
        if len(parts) < 3:
            return

        code = parts[1]
        num = parts[2]

        card = user_cards_db.get(code)
        if not card or card.get("owner_id") != user_id:
            await query.edit_message_text(f"❌ Card <code>{code}</code> not found in your roster!", parse_mode='HTML')
            return

        target_item = PILL_CATALOG.get(num)
        if not target_item:
            await query.answer("❌ Invalid pill!", show_alert=True)
            return

        pill_name = target_item["name"]
        eco = get_user_economy(user_id)
        pills = eco.get("pills", {})

        if pills.get(pill_name, 0) <= 0:
            await query.answer(f"❌ You don't have any {pill_name} in stock!", show_alert=True)
            return

        pills[pill_name] -= 1
        save_user_economy(user_economy_db)

        now = time.time()
        if target_item["seconds"] == -1:
            card["is_permanent"] = True
        else:
            current_expire = max(now, card.get("expire_at", now))
            card["expire_at"] = current_expire + target_item["seconds"]

        save_user_cards(user_cards_db)

        c_name = escape_html(card.get("name"))
        tier_str = TIER_EMOJIS.get(card.get("tier"), card.get("tier"))
        new_time_left = format_time_left(card)

        await query.edit_message_text(
            f"✨ <b>CULTIVATION SUCCESSFUL!</b> 🧘‍♂️\n\n"
            f"🎴 <b>Card:</b> {tier_str} - <b>{c_name}</b> (Code: <code>{code}</code>)\n"
            f"💊 <b>Fed Pill:</b> {pill_name}\n"
            f"⏳ <b>New Stay Limit:</b> <code>{new_time_left}</code>",
            parse_mode='HTML'
        )
        return

async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user or not update.message.photo:
        return

    user = update.message.from_user
    user_id = user.id
    track_user_data(user)

    if is_owner(user_id) and user_id in owner_rarity_sessions:
        session = owner_rarity_sessions[user_id]
        if session.get("step") == "awaiting_image":
            photo_file_id = update.message.photo[-1].file_id
            base_card = session.get("target_card")
            await finalize_rarity_image_update(update, user_id, base_card, photo_file_id)
            return

    if is_owner(user_id) and user_id in owner_addcard_sessions:
        session = owner_addcard_sessions[user_id]
        if session.get("step") == "awaiting_photo":
            photo_file_id = update.message.photo[-1].file_id
            session["photo"] = photo_file_id
            session["step"] = "awaiting_tier"

            tier_buttons = [
                [
                    InlineKeyboardButton("⚪ Mortal", callback_data="addcard_tier_Mortal"),
                    InlineKeyboardButton("🟢 Awakened", callback_data="addcard_tier_Awakened")
                ],
                [
                    InlineKeyboardButton("🔵 Transcendent", callback_data="addcard_tier_Transcendent"),
                    InlineKeyboardButton("🟣 Celestial", callback_data="addcard_tier_Celestial")
                ],
                [
                    InlineKeyboardButton("🔥 Primordial", callback_data="addcard_tier_Primordial")
                ],
                [
                    InlineKeyboardButton("❌ Cancel Wizard", callback_data="addcard_cancel")
                ]
            ]
            keyboard = InlineKeyboardMarkup(tier_buttons)

            await update.message.reply_text(
                "✨ <b>CARD CREATION WIZARD (Step 2/3)</b>\n\n"
                "📷 <b>Photo:</b> Photo Attached ✅\n\n"
                "🏷️ <b>2nd: Select Card Tier below:</b>",
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            return

async def offer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Offers card for sale to another user in exchange for Spirit Stones."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text("⚠️ Usage: <code>/offer &lt;card_code&gt; &lt;price_in_ss&gt;</code>", parse_mode='HTML')
        return

    code = args[0].strip().upper()
    if not args[1].isdigit():
        await update.message.reply_text("⚠️ Price must be a positive integer SS value!", parse_mode='HTML')
        return

    price = int(args[1])
    card = user_cards_db.get(code)

    if not card or card.get("owner_id") != user.id:
        await update.message.reply_text(f"❌ Card code <code>{code}</code> not found in your roster!", parse_mode='HTML')
        return

    offer_id = f"OFFER_{int(time.time())}_{code}"
    active_card_offers[offer_id] = {
        "offer_id": offer_id,
        "code": code,
        "seller_id": user.id,
        "price_ss": price,
        "time": time.time()
    }

    c_name = escape_html(card.get("name"))
    tier_str = TIER_EMOJIS.get(card.get("tier"), card.get("tier"))
    seller_name = escape_html(user.first_name)

    send_telegram_log(
        f"🛍️ <b>MARKET OFFER CREATED</b>\n"
        f"👤 <b>Seller:</b> {seller_name} (@{user.username or 'none'}, ID: <code>{user.id}</code>)\n"
        f"🎴 <b>Card Offered:</b> <b>{c_name}</b> (Code: <code>{code}</code>, Tier: {card.get('tier')})\n"
        f"💎 <b>Price:</b> <code>{price} SS</code>\n"
        f"🆔 <b>Offer ID:</b> <code>{offer_id}</code>"
    )

    await update.message.reply_text(
        f"🛍️ <b>CARD OFFER CREATED FOR SALE!</b>\n\n"
        f"👤 <b>Seller:</b> {seller_name}\n"
        f"🎴 <b>Card:</b> {tier_str} - <b>{c_name}</b> (Code: <code>{code}</code>)\n"
        f"💎 <b>Asking Price:</b> <code>{price} SS</code>\n\n"
        f"👉 Anyone can buy this card by typing: <code>/buyoffer {offer_id}</code>",
        parse_mode='HTML'
    )

async def buyoffer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Purchases an offered card using Spirit Stones."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()

    args = context.args or []
    if not args:
        await update.message.reply_text("⚠️ Usage: <code>/buyoffer &lt;offer_id&gt;</code>", parse_mode='HTML')
        return

    offer_id = args[0].strip()
    offer = active_card_offers.get(offer_id)

    if not offer:
        await update.message.reply_text("❌ Offer expired or not found!", parse_mode='HTML')
        return

    code = offer["code"].upper()
    seller_id = offer["seller_id"]
    price = offer["price_ss"]

    if seller_id == user.id:
        await update.message.reply_text("❌ You cannot buy your own offer!", parse_mode='HTML')
        return

    card = find_user_card_by_code(seller_id, code)
    if not card:
        active_card_offers.pop(offer_id, None)
        await update.message.reply_text("❌ Card is no longer in seller's roster!", parse_mode='HTML')
        return

    buyer_eco = get_user_economy(user.id)
    if buyer_eco["spirit_stones"] < price:
        await update.message.reply_text(
            f"❌ <b>Insufficient Spirit Stones!</b>\nPrice: <code>{price} SS</code> | Your Balance: <code>{buyer_eco['spirit_stones']} SS</code>",
            parse_mode='HTML'
        )
        return

    seller_eco = get_user_economy(seller_id)
    buyer_eco["spirit_stones"] -= price
    seller_eco["spirit_stones"] += price
    save_user_economy(user_economy_db)

    remove_user_card_by_code(seller_id, code)
    card["owner_id"] = user.id
    user_cards_db[f"{user.id}_{code}"] = card
    save_user_cards(user_cards_db)
    active_card_offers.pop(offer_id, None)

    buyer_name = escape_html(user.first_name)
    c_name = escape_html(card.get("name"))

    send_telegram_log(
        f"🤝 <b>MARKET OFFER BOUGHT / TRADED</b>\n"
        f"👤 <b>Buyer:</b> {buyer_name} (@{user.username or 'none'}, ID: <code>{user.id}</code>)\n"
        f"👤 <b>Seller ID:</b> <code>{seller_id}</code>\n"
        f"🎴 <b>Card Traded:</b> <b>{c_name}</b> (Code: <code>{code}</code>)\n"
        f"💎 <b>Price Paid:</b> <code>{price} SS</code>\n"
        f"🆔 <b>Offer ID:</b> <code>{offer_id}</code>"
    )

    await update.message.reply_text(
        f"🎉 <b>CARD PURCHASE COMPLETED!</b>\n\n"
        f"👤 <b>New Owner:</b> {buyer_name}\n"
        f"🎴 <b>Card:</b> <b>{c_name}</b> (Code: <code>{code}</code>)\n"
        f"💎 <b>Price Paid:</b> <code>{price} SS</code>",
        parse_mode='HTML'
    )

async def swap_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Swaps cards between users.
    STRICT RULE: ONLY SAME TIER CARDS CAN BE SWAPPED!
    Usage: /swap <your_card_code> <their_card_code>
    """
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "⚠️ Usage: <code>/swap &lt;your_card_code&gt; &lt;their_card_code&gt;</code>\n"
            "<i>Note: Card swapping is strictly allowed ONLY between cards of the SAME TIER!</i>",
            parse_mode='HTML'
        )
        return

    my_code = args[0].strip().upper()
    their_code = args[1].strip().upper()

    my_card = user_cards_db.get(my_code)
    their_card = user_cards_db.get(their_code)

    if not my_card or my_card.get("owner_id") != user.id:
        await update.message.reply_text(f"❌ Your card code <code>{my_code}</code> was not found in your roster!", parse_mode='HTML')
        return

    if not their_card:
        await update.message.reply_text(f"❌ Target card code <code>{their_code}</code> was not found!", parse_mode='HTML')
        return

    other_owner_id = their_card.get("owner_id")
    if other_owner_id == user.id:
        await update.message.reply_text("❌ Both cards belong to you! No need to swap.", parse_mode='HTML')
        return

    my_tier = my_card.get("tier", "").capitalize()
    their_tier = their_card.get("tier", "").capitalize()

    if my_tier.lower() != their_tier.lower():
        await update.message.reply_text(
            f"🚫 <b>SWAP DENIED! DIFFERENT TIERS DETECTED!</b>\n\n"
            f"• Your Card (<code>{my_code}</code>): {TIER_EMOJIS.get(my_tier, my_tier)}\n"
            f"• Their Card (<code>{their_code}</code>): {TIER_EMOJIS.get(their_tier, their_tier)}\n\n"
            f"❌ <b>Rule:</b> You can ONLY swap cards that belong to the <b>SAME TIER</b>!",
            parse_mode='HTML'
        )
        return

    my_card["owner_id"] = other_owner_id
    their_card["owner_id"] = user.id
    save_user_cards(user_cards_db)

    my_cname = escape_html(my_card.get("name"))
    their_cname = escape_html(their_card.get("name"))
    other_info = users_db.get(str(other_owner_id), {})
    other_name = escape_html(other_info.get("first_name", f"User {other_owner_id}"))

    send_telegram_log(
        f"🔄 <b>SAME-TIER CARD SWAP COMPLETED</b>\n"
        f"👤 <b>User 1:</b> {escape_html(user.first_name)} (@{user.username or 'none'}, ID: <code>{user.id}</code>) gave <b>{my_cname}</b> (Code: <code>{my_code}</code>)\n"
        f"👤 <b>User 2:</b> {other_name} (ID: <code>{other_owner_id}</code>) gave <b>{their_cname}</b> (Code: <code>{their_code}</code>)\n"
        f"✨ <b>Tier Matched:</b> {my_tier}"
    )

    await update.message.reply_text(
        f"🔄 <b>SAME-TIER CARD SWAP COMPLETED!</b> 🎉\n\n"
        f"✨ <b>Tier Matched:</b> {TIER_EMOJIS.get(my_tier, my_tier)}\n\n"
        f"• You gave <b>{my_cname}</b> (<code>{my_code}</code>) to {other_name}\n"
        f"• You received <b>{their_cname}</b> (<code>{their_code}</code>) in return!",
        parse_mode='HTML'
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays user's game progress, Spirit Stones balance, card collection, and completion percentage."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()

    eco = get_user_economy(user.id)
    ss_bal = eco["spirit_stones"]
    uname = escape_html(user.first_name)

    # Fetch user's cards
    user_cards = [c for c in user_cards_db.values() if c.get("owner_id") == user.id]
    total_roster_cards = len(user_cards)

    # Calculate collection progress against global cards_pool_db
    total_pool_count = len(cards_pool_db)

    # Set of user's owned card names & tiers
    user_owned_set = {
        (c.get("name", "").strip().lower(), c.get("tier", "").strip().lower())
        for c in user_cards
    }

    # Unique base cards matched
    unique_owned_count = 0
    tier_progress: Dict[str, Dict[str, int]] = {
        "Mortal": {"owned": 0, "total": 0},
        "Awakened": {"owned": 0, "total": 0},
        "Transcendent": {"owned": 0, "total": 0},
        "Celestial": {"owned": 0, "total": 0},
        "Primordial": {"owned": 0, "total": 0}
    }

    for base_card in cards_pool_db:
        b_name = base_card.get("name", "").strip().lower()
        b_tier = base_card.get("tier", "Mortal").capitalize()

        if b_tier not in tier_progress:
            tier_progress[b_tier] = {"owned": 0, "total": 0}

        tier_progress[b_tier]["total"] += 1

        if (b_name, b_tier.lower()) in user_owned_set:
            unique_owned_count += 1
            tier_progress[b_tier]["owned"] += 1

    # Calculate completion percentage
    if total_pool_count > 0:
        completion_pct = (unique_owned_count / total_pool_count) * 100.0
    else:
        completion_pct = 0.0

    # Build tier breakdown string
    tier_lines = []
    for t_name in ["Primordial", "Celestial", "Transcendent", "Awakened", "Mortal"]:
        t_data = tier_progress.get(t_name, {"owned": 0, "total": 0})
        t_owned = t_data["owned"]
        t_total = t_data["total"]
        emoji = TIER_EMOJIS.get(t_name, t_name)
        if t_total > 0:
            t_pct = (t_owned / t_total) * 100.0
            tier_lines.append(f"  • {emoji}: <b>{t_owned}/{t_total}</b> (<code>{t_pct:.1f}%</code>)")
        else:
            tier_lines.append(f"  • {emoji}: <b>0/0</b> (<code>N/A</code>)")

    tier_str = "\n".join(tier_lines)

    progress_bar_length = 10
    filled_blocks = int((completion_pct / 100.0) * progress_bar_length)
    progress_bar = "🟩" * filled_blocks + "⬜" * (progress_bar_length - filled_blocks)

    text = (
        f"📊 <b>CULTIVATOR GAME STATUS: {uname}</b>\n\n"
        f"💎 <b>Spirit Stones (SS):</b> <code>{ss_bal} SS</code>\n"
        f"🎴 <b>Roster Cards Owned:</b> <code>{total_roster_cards} cards</code>\n"
        f"🎯 <b>Unique Cards Collected:</b> <code>{unique_owned_count}/{total_pool_count} base cards</code>\n"
        f"🏆 <b>Game Completion:</b> <b>{completion_pct:.1f}%</b>\n"
        f"[{progress_bar}]\n\n"
        f"🏷️ <b>Collection Progress by Tier:</b>\n"
        f"{tier_str}\n\n"
        f"💡 <i>Tip: Recruited cards increase your completion %! When new cards are added to the pool, completion % adjusts automatically.</i>"
    )

    await update.message.reply_text(text, parse_mode='HTML')

async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends an inline button with the Cultivation Mini App link."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    web_app_url = os.getenv("RENDER_EXTERNAL_URL") or "https://t-xcem.onrender.com"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("OPEN MINI APP", web_app=WebAppInfo(url=web_app_url))]
    ])

    await message.reply_text(
        "🌟 <b>CULTIVATION CULT TELEGRAM MINI APP</b> ⛩️\n\n"
        "Tap the button below to open your cultivation dashboard!",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

# ==================== CONNECT 4 GAME (/JOIN4) ====================
active_connect4_games: Dict[int, Dict] = {}  # chat_id -> game_state

def check_connect4_win(board, token) -> bool:
    rows = 6
    cols = 7
    # Horizontal
    for r in range(rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r][c+1] == token and board[r][c+2] == token and board[r][c+3] == token:
                return True
    # Vertical
    for r in range(rows - 3):
        for c in range(cols):
            if board[r][c] == token and board[r+1][c] == token and board[r+2][c] == token and board[r+3][c] == token:
                return True
    # Diagonal positive slope
    for r in range(rows - 3):
        for c in range(cols - 3):
            if board[r][c] == token and board[r+1][c+1] == token and board[r+2][c+2] == token and board[r+3][c+3] == token:
                return True
    # Diagonal negative slope
    for r in range(3, rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r-1][c+1] == token and board[r-2][c+2] == token and board[r-3][c+3] == token:
                return True
    return False

def is_board_full(board) -> bool:
    return all(board[0][c] != "⚪️" for c in range(7))

def drop_token(board, col, token) -> int:
    for r in range(5, -1, -1):
        if board[r][col] == "⚪️":
            board[r][col] = token
            return r
    return -1

def build_connect4_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    row_buttons = []
    for c in range(7):
        row_buttons.append(InlineKeyboardButton(str(c+1), callback_data=f"c4_col_{c}"))
    return InlineKeyboardMarkup([row_buttons])

def format_connect4_board(game: Dict) -> str:
    board = game["board"]
    board_str = "\n".join([" ".join(row) for row in board])
    p1 = escape_html(game["player1_name"])
    p2 = escape_html(game["player2_name"])
    turn_name = p1 if game["current_turn"] == game["player1_id"] else p2
    turn_token = "🔴" if game["current_turn"] == game["player1_id"] else "🟡"

    if game["player2_id"] == 0:
        pot_info = "🎁 <b>Reward on Win:</b> <code>+100 SS</code> (Free Play vs Bot)"
    else:
        pot_info = f"💰 <b>Pot / Bet:</b> <code>{game['bet'] * 2} SS</code> (<code>{game['bet']} SS</code> each)"

    return (
        f"🔴 <b>CONNECT 4 BATTLE</b> 🟡\n\n"
        f"👤 <b>Player 1 (🔴):</b> {p1}\n"
        f"👤 <b>Player 2 (🟡):</b> {p2}\n"
        f"{pot_info}\n\n"
        f"{board_str}\n\n"
        f"👉 <b>Turn:</b> {turn_token} <b>{escape_html(turn_name)}</b>\n"
        f"<i>Tap column numbers (1-7) below to drop your disk!</i>"
    )

async def join4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a Connect 4 game vs Bot (Free, +100 SS on win) or PvP against another user with SS bet."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    chat_id = update.message.chat.id

    args = context.args or []
    opponent_id = 0
    opponent_name = "Sylvi Bot 🤖"
    bet = 0

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        opp_user = update.message.reply_to_message.from_user
        if opp_user.id == user.id:
            await update.message.reply_text("❌ You cannot play against yourself!", parse_mode='HTML')
            return
        if opp_user.is_bot:
            await update.message.reply_text("❌ You cannot challenge bots as a user opponent!", parse_mode='HTML')
            return

        if not args or not args[0].isdigit():
            await update.message.reply_text(
                "⚠️ <b>Usage for PvP:</b> <code>/join4 &lt;bet_ss&gt;</code> (replying to opponent's message)\n"
                "<i>Example:</i> <code>/join4 50</code>",
                parse_mode='HTML'
            )
            return

        bet = int(args[0])
        if bet < 10:
            await update.message.reply_text("❌ Minimum bet for PvP Connect 4 is <b>10 SS</b>!", parse_mode='HTML')
            return

        eco1 = get_user_economy(user.id)
        if eco1["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ You don't have enough Spirit Stones! Your balance: <code>{eco1['spirit_stones']} SS</code>", parse_mode='HTML')
            return

        eco2 = get_user_economy(opp_user.id)
        if eco2["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ Opponent <b>{escape_html(opp_user.first_name)}</b> does not have enough Spirit Stones ({bet} SS required)!", parse_mode='HTML')
            return

        opponent_id = opp_user.id
        opponent_name = opp_user.first_name

        eco1["spirit_stones"] -= bet
        eco2["spirit_stones"] -= bet
        save_user_economy(user_economy_db)
    else:
        bet = 0

    game_state = {
        "chat_id": chat_id,
        "player1_id": user.id,
        "player1_name": user.first_name,
        "player2_id": opponent_id,
        "player2_name": opponent_name,
        "bet": bet,
        "board": [["⚪️" for _ in range(7)] for _ in range(6)],
        "current_turn": user.id,
        "status": "active"
    }
    active_connect4_games[chat_id] = game_state

    text = format_connect4_board(game_state)
    keyboard = build_connect4_keyboard(chat_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

async def connect4_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user = query.from_user
    chat_id = query.message.chat.id
    data = query.data or ""

    game = active_connect4_games.get(chat_id)
    if not game or game["status"] != "active":
        await query.answer("⚠️ No active Connect 4 game in this chat! Start one with /join4.", show_alert=True)
        return

    if user.id != game["current_turn"]:
        await query.answer("⏳ It's not your turn!", show_alert=True)
        return

    col = int(data.replace("c4_col_", ""))
    token = "🔴" if user.id == game["player1_id"] else "🟡"

    row = drop_token(game["board"], col, token)
    if row == -1:
        await query.answer("⚠️ Column is full! Choose another column.", show_alert=True)
        return

    await query.answer()

    if check_connect4_win(game["board"], token):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        winner_id = user.id
        winner_name = user.first_name

        if game["player2_id"] == 0:
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += 100
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - YOU BEAT THE BOT!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Reward Received:</b> <code>+100 Spirit Stones (SS)</code> 💎\n\n"
                f"<i>Type /join4 to play again!</i>"
            )
        else:
            pot = game["bet"] * 2
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += pot
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - WINNER!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Prize Won:</b> <code>+{pot} SS</code> 💎\n\n"
                f"<i>Type /join4 &lt;bet&gt; to play again!</i>"
            )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    if is_board_full(game["board"]):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        if game["player2_id"] != 0:
            eco1 = get_user_economy(game["player1_id"])
            eco1["spirit_stones"] += game["bet"]
            eco2 = get_user_economy(game["player2_id"])
            eco2["spirit_stones"] += game["bet"]
            save_user_economy(user_economy_db)

        board_str = "\n".join([" ".join(r) for r in game["board"]])
        refund_str = f" Both players' bets (<code>{game['bet']} SS</code> each) have been refunded!" if game["player2_id"] != 0 else ""
        text = (
            f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
            f"{board_str}\n\n"
            f"<i>Board is full.{refund_str}</i>"
        )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    next_turn = game["player2_id"] if game["current_turn"] == game["player1_id"] else game["player1_id"]
    game["current_turn"] = next_turn

    if game["status"] == "active" and game["current_turn"] == 0:
        bot_token = "🟡"
        valid_cols = [c for c in range(7) if game["board"][0][c] == "⚪️"]
        if valid_cols:
            # Loophole: Bot picks a random valid column and never defends or blocks player!
            bot_col = random.choice(valid_cols)
            drop_token(game["board"], bot_col, bot_token)

            if check_connect4_win(game["board"], bot_token):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤖 <b>CONNECT 4 - SYLVI BOT WINS!</b> 💀\n\n"
                    f"{board_str}\n\n"
                    f"👑 <b>Winner:</b> <b>Sylvi Bot 🤖</b>\n\n"
                    f"<i>Type /join4 to try again!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            if is_board_full(game["board"]):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
                    f"{board_str}\n\n"
                    f"<i>Game ended in a draw!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            game["current_turn"] = game["player1_id"]

    text = format_connect4_board(game)
    keyboard = build_connect4_keyboard(chat_id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        pass

async def logsgc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to set the target group chat ID for real-time activity logs."""
    global LOGS_GC_ID
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_owner(user.id):
        await message.reply_text("🚫 Only the bot owner can use this command!")
        return

    args = context.args or []
    if not args:
        chat_id = message.chat.id
        LOGS_GC_ID = chat_id
        save_logs_gc_id(chat_id)
        send_telegram_log(f"✅ Activity logs group set to this chat ID: <code>{chat_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"This chat (ID: <code>{chat_id}</code>) will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
        return

    try:
        target_id = int(args[0].strip())
        LOGS_GC_ID = target_id
        save_logs_gc_id(target_id)
        send_telegram_log(f"✅ Activity logs group set to chat ID: <code>{target_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"Target Chat ID: <code>{target_id}</code> will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
    except ValueError:
        await message.reply_text("⚠️ Invalid Chat ID! Must be a valid integer.", parse_mode='HTML')

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the Spirit Stones economy leaderboard (top players)."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    if not user_economy_db:
        await message.reply_text("🏆 <b>Leaderboard is currently empty!</b>", parse_mode='HTML')
        return

    sorted_econ = sorted(
        user_economy_db.values(),
        key=lambda x: x.get("spirit_stones", 0) if isinstance(x, dict) else 0,
        reverse=True
    )

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = []

    for idx, eco in enumerate(sorted_econ[:10]):
        if not isinstance(eco, dict):
            continue
        uid = eco.get("user_id")
        ss = eco.get("spirit_stones", 0)
        u_info = users_db.get(str(uid), {})
        uname = u_info.get("username")
        fname = u_info.get("first_name") or f"Cultivator {uid}"

        display_name = f"@{escape_html(str(uname))}" if uname else escape_html(str(fname))
        medal = medals[idx] if idx < len(medals) else f"•"

        lines.append(f"{medal} <b>{display_name}</b> — <code>{ss} SS</code> 💎")

    lb_str = "\n".join(lines)

    text = (
        f"🏆 <b>CULTIVATION SPIRIT STONES LEADERBOARD</b> 💎\n\n"
        f"{lb_str}\n\n"
        f"<i>💡 Earn more Spirit Stones via /daily, /summon, or betting in /mine!</i>"
    )

    await message.reply_text(text, parse_mode='HTML')

async def claim_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Claims a card dropped via /summon or 100-msg group drop."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    chat_id = update.message.chat.id
    track_user_data(user)

    dropped = active_drops.get(chat_id)
    if not dropped:
        await update.message.reply_text("ℹ️ No active recruit drop in this chat right now!", parse_mode='HTML')
        return

    active_drops.pop(chat_id, None)

    claimed_card = create_card_instance(user.id, forced_tier=dropped["tier"])
    claimed_card["name"] = dropped["name"]
    claimed_card["photo"] = dropped["photo"]
    claimed_card["worth"] = dropped.get("worth", TIER_WORTH.get(dropped["tier"], 200))
    save_user_cards(user_cards_db)

    uname = escape_html(user.first_name)
    cname = escape_html(claimed_card["name"])
    tier_str = TIER_EMOJIS.get(claimed_card["tier"], claimed_card["tier"])
    worth = get_card_worth(claimed_card)
    code = claimed_card["code"]

    send_telegram_log(
        f"🎉 <b>CARD RECRUITED / CLAIMED</b>\n"
        f"👤 <b>User:</b> {uname} (@{user.username or 'none'}, ID: <code>{user.id}</code>)\n"
        f"🎴 <b>Card Recruited:</b> <b>{cname}</b> (Code: <code>{code}</code>, Tier: {claimed_card['tier']})\n"
        f"📍 <b>Group Chat:</b> <code>{chat_id}</code>"
    )

    await update.message.reply_text(
        f"🎉 <b>{uname} recruited {cname} into their crew!</b> 🏆\n\n"
        f"⚔️ <b>Character:</b> <b>{cname}</b>\n"
        f"✨ <b>Tier:</b> {tier_str}\n"
        f"💎 <b>Worth:</b> <code>{worth} SS</code>\n"
        f"🔑 <b>Code:</b> <code>{code}</code>\n\n"
        f"💡 Check your crew anytime with <code>/mycards</code>!",
        parse_mode='HTML'
    )

async def removecard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to delete a card from the global database pool, or user command to remove from roster."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "⚠️ Usage:\n"
            "• Owner: <code>/removecard &lt;card_pool_id_or_name&gt;</code> (Deletes from global database pool)\n"
            "• User: <code>/removecard &lt;4-digit_code&gt;</code> (Removes from roster)",
            parse_mode='HTML'
        )
        return

    target = args[0].strip()

    # If owner is trying to delete from global cards pool
    if is_owner(user.id):
        matched_pool_card = None
        for c in cards_pool_db:
            if c.get("id", "").lower() == target.lower() or c.get("name", "").lower() == target.lower():
                matched_pool_card = c
                break

        if matched_pool_card:
            card_id = matched_pool_card["id"]
            c_name = escape_html(matched_pool_card.get("name", "Card"))
            cards_pool_db[:] = [c for c in cards_pool_db if c.get("id") != card_id]
            save_cards_pool(cards_pool_db)

            db = get_mongo_db()
            if db is not None:
                try:
                    db["cards_pool"].delete_one({"_id": card_id})
                except Exception as e:
                    logger.error(f"Error deleting card pool item {card_id} from MongoDB: {e}")

            await update.message.reply_text(
                f"🗑️ <b>GLOBAL DATABASE CARD DELETED!</b>\n\n"
                f"Card <b>{c_name}</b> (Pool ID: <code>{card_id}</code>) has been permanently deleted from the bot database pool.",
                parse_mode='HTML'
            )
            return

    # Otherwise, treat as 4-digit roster code for user (or owner roster card)
    card = user_cards_db.get(target.upper())
    if not card:
        await update.message.reply_text(f"❌ Card code or pool ID <code>{target}</code> not found in your roster or global database!", parse_mode='HTML')
        return

    if card.get("owner_id") != user.id and not is_owner(user.id):
        await update.message.reply_text("🚫 You do not own this card!", parse_mode='HTML')
        return

    c_name = escape_html(card.get("name", "Card"))
    user_cards_db.pop(target.upper(), None)

    db = get_mongo_db()
    if db is not None:
        try:
            db["user_cards"].delete_one({"_id": target.upper()})
        except Exception as e:
            logger.error(f"Error removing card {target} from MongoDB: {e}")

    save_user_cards(user_cards_db)

    await update.message.reply_text(
        f"🗑️ <b>CARD REMOVED FROM ROSTER!</b>\n\n"
        f"Card <b>{c_name}</b> (Code: <code>{target.upper()}</code>) has been removed from the crew/roster.",
        parse_mode='HTML'
    )

async def discard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Discards a card from user's roster and converts it to Spirit Stones (SS)."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    clean_expired_cards()

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "⚠️ Usage: <code>/discard &lt;4-digit_code&gt;</code> (e.g. <code>/discard 4444</code>)\n"
            "<i>Note: Discarding a card permanently removes it from your roster and refunds its Worth in Spirit Stones (SS)!</i>",
            parse_mode='HTML'
        )
        return

    code = args[0].strip().upper()
    card = find_user_card_by_code(user.id, code)

    if not card:
        await update.message.reply_text(f"❌ Card code <code>{code}</code> was not found in your roster!", parse_mode='HTML')
        return

    c_name = escape_html(card.get("name", "Cultivator"))
    tier_name = card.get("tier", "Mortal")
    tier_emoji = TIER_EMOJIS.get(tier_name, tier_name)
    worth = get_card_worth(card)

    remove_user_card_by_code(user.id, code)

    eco = get_user_economy(user.id)
    eco["spirit_stones"] += worth
    save_user_economy(user_economy_db)

    uname = escape_html(user.first_name)
    new_ss_bal = eco["spirit_stones"]

    await update.message.reply_text(
        f"♻️ <b>CARD DISCARDED FOR SPIRIT STONES!</b> 💎\n\n"
        f"👤 <b>Cultivator:</b> {uname}\n"
        f"🎴 <b>Discarded:</b> {tier_emoji} - <b>{c_name}</b> (Code: <code>{code}</code>)\n"
        f"💰 <b>Spirit Stones Received:</b> <code>+{worth} SS</code> 💎\n"
        f"💎 <b>New SS Balance:</b> <code>{new_ss_bal} SS</code>",
        parse_mode='HTML'
    )

async def allcards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays all character cards in the global base pool."""
    if not update.message:
        return

    if not cards_pool_db:
        await update.message.reply_text("📜 Global card pool is empty!", parse_mode='HTML')
        return

    tiered_cards: Dict[str, List[Dict]] = {
        "Primordial": [],
        "Celestial": [],
        "Transcendent": [],
        "Awakened": [],
        "Mortal": []
    }

    for c in cards_pool_db:
        t = c.get("tier", "Mortal").capitalize()
        if t in tiered_cards:
            tiered_cards[t].append(c)
        else:
            tiered_cards.setdefault("Mortal", []).append(c)

    sections = []
    for tier_name in ["Primordial", "Celestial", "Transcendent", "Awakened", "Mortal"]:
        c_list = tiered_cards[tier_name]
        if not c_list:
            continue
        emoji = TIER_EMOJIS.get(tier_name, tier_name)
        lines = [f"<b>{emoji} ({len(c_list)}):</b>"]
        for card in c_list:
            c_id = card.get("id", "N/A")
            name = escape_html(card.get("name", "Unknown"))
            lines.append(f"  • [{c_id}] <b>{name}</b>")
        sections.append("\n".join(lines))

    full_text = "📜 <b>GLOBAL CULTIVATION CARDS POOL:</b>\n\n" + "\n\n".join(sections)

    if len(full_text) > 4000:
        for chunk in [full_text[i:i+3500] for i in range(0, len(full_text), 3500)]:
            await update.message.reply_text(chunk, parse_mode='HTML')
    else:
        await update.message.reply_text(full_text, parse_mode='HTML')

# ==================== COMMAND HANDLERS ====================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays help menu with all available commands."""
    if not update.message:
        return

    user = update.message.from_user
    track_user_data(user)
    user_id = user.id if user else 0

    help_text = (
        "🤖 <b>Sylvi AI &amp; Cultivation Card Bot - Help Menu</b>\n\n"
        "🌌 <b>Cultivation Anime Card System:</b>\n"
        "• <code>/summon</code> - Summon a recruit drop.\n"
        "• <code>/recruit</code> / <code>/claim</code> / <code>/grab</code> - Bring dropped card into your crew.\n"
        "• <code>/mycards</code> / <code>/roster</code> - View your owned cards and stay limits.\n"
        "• <code>/scard &lt;code&gt;</code> / <code>/showcard</code> - View card details (Name, Worth, Tier, Stay Time).\n"
        "• <code>/discard &lt;code&gt;</code> - Discard a card to receive Spirit Stones (SS) equal to its worth.\n"
        "• <code>/allcards</code> - View all cards available in the global card pool.\n"
        "• <code>/daily</code> - Claim daily 500 Spirit Stones (SS) + 1 Bonus Mortal Card.\n"
        "• <code>/pillshop</code> - View pills shop to extend stay duration.\n"
        "• <code>/buy &lt;pill&gt; [qty]</code> - Buy pills (e.g. <code>/buy qi 2</code>, <code>/buy eternal</code>).\n"
        "• <code>/inventory</code> - Check your Spirit Stones balance and pills stock.\n"
        "• <code>/cultivate &lt;code&gt; &lt;pill&gt;</code> - Feed pills to extend card stay duration.\n"
        "• <code>/offer &lt;code&gt; &lt;price_ss&gt;</code> - Sell card to another user for Spirit Stones.\n"
        "• <code>/buyoffer &lt;offer_id&gt;</code> - Buy an offered card.\n"
        "• <code>/swap &lt;your_code&gt; &lt;their_code&gt;</code> - Swap cards (strictly SAME TIER only!).\n\n"
        "📌 <b>General Commands:</b>\n"
        "• <code>/help</code> ya <code>/start</code> - Help menu dekhein.\n"
        "• <code>/statsgc</code> - Group chat stats &amp; memory info dekhein.\n"
        "• <code>/4words</code> - 4-letter word guessing game (30 tries + hints).\n"
        "• <code>/join4 &lt;bet_ss&gt;</code> - Connect 4 game (PvP vs user by replying, or vs Bot without reply).\n\n"
        "🔥 <b>AI Assistant Features:</b>\n"
        "• <b>AI Name:</b> Bot ka naam <b>Sylvi</b> hai! Reply karke, <code>@username</code> mention karke, ya <b>Sylvi</b> bolkar baat karein.\n"
    )

    if is_owner(user_id):
        help_text += (
            "\n👑 <b>Owner Special Commands:</b>\n"
            "• <code>/addcard &lt;tier&gt; &lt;name&gt;</code> - Add new character card to pool (with photo).\n"
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
        "cards_pool": cards_pool_db,
        "user_cards": user_cards_db,
        "user_economy": user_economy_db,
        "exported_at": time.time()
    }

    export_json = json.dumps(export_data, indent=2, ensure_ascii=False)
    bytes_io = io.BytesIO(export_json.encode("utf-8"))
    filename = f"sylvi_mongo_export_{int(time.time())}.json"

    await update.message.reply_document(
        document=bytes_io,
        filename=filename,
        caption="📤 <b>MongoDB Data Export Complete!</b>\nAll users, reminders, Groq API keys, cards, and economy exported successfully.",
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
        save_cards_pool(cards_pool_db)
        save_user_cards(user_cards_db)
        save_user_economy(user_economy_db)
        await update.message.reply_text("📥 <b>Local Data Synced to MongoDB Successfully!</b>", parse_mode="HTML")
        return

    try:
        tg_file = await doc.get_file()
        file_bytes = await tg_file.download_as_bytearray()
        imp_data = json.loads(file_bytes.decode("utf-8"))

        imp_users = imp_data.get("users", {})
        imp_reminders = imp_data.get("reminders", {})
        imp_keys = imp_data.get("groq_keys", [])
        imp_cards_pool = imp_data.get("cards_pool", [])
        imp_user_cards = imp_data.get("user_cards", {})
        imp_user_economy = imp_data.get("user_economy", {})

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

        if imp_cards_pool:
            cards_pool_db.clear()
            cards_pool_db.extend(imp_cards_pool)
            save_cards_pool(cards_pool_db)

        if imp_user_cards:
            user_cards_db.update(imp_user_cards)
            save_user_cards(user_cards_db)

        if imp_user_economy:
            user_economy_db.update(imp_user_economy)
            save_user_economy(user_economy_db)

        await update.message.reply_text(
            f"📥 <b>MongoDB Data Import Successful!</b>\n"
            f"• Users: <code>{len(imp_users)}</code>\n"
            f"• Reminders: <code>{len(imp_reminders)}</code>\n"
            f"• Keys: <code>{len(imp_keys)}</code>\n"
            f"• Base Cards: <code>{len(imp_cards_pool)}</code>\n"
            f"• User Cards: <code>{len(imp_user_cards)}</code>\n"
            f"• Economies: <code>{len(imp_user_economy)}</code>",
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

async def web_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends an inline button with the Cultivation Mini App link."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    web_app_url = os.getenv("RENDER_EXTERNAL_URL") or "https://t-xcem.onrender.com"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("OPEN MINI APP", web_app=WebAppInfo(url=web_app_url))]
    ])

    await message.reply_text(
        "🌟 <b>CULTIVATION CULT TELEGRAM MINI APP</b> ⛩️\n\n"
        "Tap the button below to open your cultivation dashboard!",
        reply_markup=keyboard,
        parse_mode='HTML'
    )

# ==================== CONNECT 4 GAME (/JOIN4) ====================
active_connect4_games: Dict[int, Dict] = {}  # chat_id -> game_state

def check_connect4_win(board, token) -> bool:
    rows = 6
    cols = 7
    # Horizontal
    for r in range(rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r][c+1] == token and board[r][c+2] == token and board[r][c+3] == token:
                return True
    # Vertical
    for r in range(rows - 3):
        for c in range(cols):
            if board[r][c] == token and board[r+1][c] == token and board[r+2][c] == token and board[r+3][c] == token:
                return True
    # Diagonal positive slope
    for r in range(rows - 3):
        for c in range(cols - 3):
            if board[r][c] == token and board[r+1][c+1] == token and board[r+2][c+2] == token and board[r+3][c+3] == token:
                return True
    # Diagonal negative slope
    for r in range(3, rows):
        for c in range(cols - 3):
            if board[r][c] == token and board[r-1][c+1] == token and board[r-2][c+2] == token and board[r-3][c+3] == token:
                return True
    return False

def is_board_full(board) -> bool:
    return all(board[0][c] != "⚪️" for c in range(7))

def drop_token(board, col, token) -> int:
    for r in range(5, -1, -1):
        if board[r][col] == "⚪️":
            board[r][col] = token
            return r
    return -1

def build_connect4_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    row_buttons = []
    for c in range(7):
        row_buttons.append(InlineKeyboardButton(str(c+1), callback_data=f"c4_col_{c}"))
    return InlineKeyboardMarkup([row_buttons])

def format_connect4_board(game: Dict) -> str:
    board = game["board"]
    board_str = "\n".join([" ".join(row) for row in board])
    p1 = escape_html(game["player1_name"])
    p2 = escape_html(game["player2_name"])
    turn_name = p1 if game["current_turn"] == game["player1_id"] else p2
    turn_token = "🔴" if game["current_turn"] == game["player1_id"] else "🟡"

    if game["player2_id"] == 0:
        pot_info = "🎁 <b>Reward on Win:</b> <code>+100 SS</code> (Free Play vs Bot)"
    else:
        pot_info = f"💰 <b>Pot / Bet:</b> <code>{game['bet'] * 2} SS</code> (<code>{game['bet']} SS</code> each)"

    return (
        f"🔴 <b>CONNECT 4 BATTLE</b> 🟡\n\n"
        f"👤 <b>Player 1 (🔴):</b> {p1}\n"
        f"👤 <b>Player 2 (🟡):</b> {p2}\n"
        f"{pot_info}\n\n"
        f"{board_str}\n\n"
        f"👉 <b>Turn:</b> {turn_token} <b>{escape_html(turn_name)}</b>\n"
        f"<i>Tap column numbers (1-7) below to drop your disk!</i>"
    )

async def join4_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts a Connect 4 game vs Bot (Free, +100 SS on win) or PvP against another user with SS bet."""
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    track_user_data(user)
    chat_id = update.message.chat.id

    args = context.args or []
    opponent_id = 0
    opponent_name = "Sylvi Bot 🤖"
    bet = 0

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        opp_user = update.message.reply_to_message.from_user
        if opp_user.id == user.id:
            await update.message.reply_text("❌ You cannot play against yourself!", parse_mode='HTML')
            return
        if opp_user.is_bot:
            await update.message.reply_text("❌ You cannot challenge bots as a user opponent!", parse_mode='HTML')
            return

        if not args or not args[0].isdigit():
            await update.message.reply_text(
                "⚠️ <b>Usage for PvP:</b> <code>/join4 &lt;bet_ss&gt;</code> (replying to opponent's message)\n"
                "<i>Example:</i> <code>/join4 50</code>",
                parse_mode='HTML'
            )
            return

        bet = int(args[0])
        if bet < 10:
            await update.message.reply_text("❌ Minimum bet for PvP Connect 4 is <b>10 SS</b>!", parse_mode='HTML')
            return

        eco1 = get_user_economy(user.id)
        if eco1["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ You don't have enough Spirit Stones! Your balance: <code>{eco1['spirit_stones']} SS</code>", parse_mode='HTML')
            return

        eco2 = get_user_economy(opp_user.id)
        if eco2["spirit_stones"] < bet:
            await update.message.reply_text(f"❌ Opponent <b>{escape_html(opp_user.first_name)}</b> does not have enough Spirit Stones ({bet} SS required)!", parse_mode='HTML')
            return

        opponent_id = opp_user.id
        opponent_name = opp_user.first_name

        eco1["spirit_stones"] -= bet
        eco2["spirit_stones"] -= bet
        save_user_economy(user_economy_db)
    else:
        bet = 0

    game_state = {
        "chat_id": chat_id,
        "player1_id": user.id,
        "player1_name": user.first_name,
        "player2_id": opponent_id,
        "player2_name": opponent_name,
        "bet": bet,
        "board": [["⚪️" for _ in range(7)] for _ in range(6)],
        "current_turn": user.id,
        "status": "active"
    }
    active_connect4_games[chat_id] = game_state

    text = format_connect4_board(game_state)
    keyboard = build_connect4_keyboard(chat_id)
    await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')

async def connect4_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.from_user:
        return

    user = query.from_user
    chat_id = query.message.chat.id
    data = query.data or ""

    game = active_connect4_games.get(chat_id)
    if not game or game["status"] != "active":
        await query.answer("⚠️ No active Connect 4 game in this chat! Start one with /join4.", show_alert=True)
        return

    if user.id != game["current_turn"]:
        await query.answer("⏳ It's not your turn!", show_alert=True)
        return

    col = int(data.replace("c4_col_", ""))
    token = "🔴" if user.id == game["player1_id"] else "🟡"

    row = drop_token(game["board"], col, token)
    if row == -1:
        await query.answer("⚠️ Column is full! Choose another column.", show_alert=True)
        return

    await query.answer()

    if check_connect4_win(game["board"], token):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        winner_id = user.id
        winner_name = user.first_name

        if game["player2_id"] == 0:
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += 100
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - YOU BEAT THE BOT!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Reward Received:</b> <code>+100 Spirit Stones (SS)</code> 💎\n\n"
                f"<i>Type /join4 to play again!</i>"
            )
        else:
            pot = game["bet"] * 2
            eco = get_user_economy(winner_id)
            eco["spirit_stones"] += pot
            save_user_economy(user_economy_db)

            board_str = "\n".join([" ".join(r) for r in game["board"]])
            text = (
                f"🏆 <b>CONNECT 4 - WINNER!</b> 🎉\n\n"
                f"{board_str}\n\n"
                f"👑 <b>Winner:</b> <b>{escape_html(winner_name)}</b>\n"
                f"💎 <b>Prize Won:</b> <code>+{pot} SS</code> 💎\n\n"
                f"<i>Type /join4 &lt;bet&gt; to play again!</i>"
            )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    if is_board_full(game["board"]):
        game["status"] = "finished"
        active_connect4_games.pop(chat_id, None)

        if game["player2_id"] != 0:
            eco1 = get_user_economy(game["player1_id"])
            eco1["spirit_stones"] += game["bet"]
            eco2 = get_user_economy(game["player2_id"])
            eco2["spirit_stones"] += game["bet"]
            save_user_economy(user_economy_db)

        board_str = "\n".join([" ".join(r) for r in game["board"]])
        refund_str = f" Both players' bets (<code>{game['bet']} SS</code> each) have been refunded!" if game["player2_id"] != 0 else ""
        text = (
            f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
            f"{board_str}\n\n"
            f"<i>Board is full.{refund_str}</i>"
        )
        await query.edit_message_text(text, parse_mode='HTML')
        return

    next_turn = game["player2_id"] if game["current_turn"] == game["player1_id"] else game["player1_id"]
    game["current_turn"] = next_turn

    if game["status"] == "active" and game["current_turn"] == 0:
        bot_token = "🟡"
        valid_cols = [c for c in range(7) if game["board"][0][c] == "⚪️"]
        if valid_cols:
            # Loophole: Bot picks a random valid column and never defends or blocks player!
            bot_col = random.choice(valid_cols)
            drop_token(game["board"], bot_col, bot_token)

            if check_connect4_win(game["board"], bot_token):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤖 <b>CONNECT 4 - SYLVI BOT WINS!</b> 💀\n\n"
                    f"{board_str}\n\n"
                    f"👑 <b>Winner:</b> <b>Sylvi Bot 🤖</b>\n\n"
                    f"<i>Type /join4 to try again!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            if is_board_full(game["board"]):
                game["status"] = "finished"
                active_connect4_games.pop(chat_id, None)

                board_str = "\n".join([" ".join(r) for r in game["board"]])
                text = (
                    f"🤝 <b>CONNECT 4 - IT'S A DRAW!</b> ⚖️\n\n"
                    f"{board_str}\n\n"
                    f"<i>Game ended in a draw!</i>"
                )
                await query.edit_message_text(text, parse_mode='HTML')
                return

            game["current_turn"] = game["player1_id"]

    text = format_connect4_board(game)
    keyboard = build_connect4_keyboard(chat_id)
    try:
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
    except Exception:
        pass

async def logsgc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner command to set the target group chat ID for real-time activity logs."""
    global LOGS_GC_ID
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_owner(user.id):
        await message.reply_text("🚫 Only the bot owner can use this command!")
        return

    args = context.args or []
    if not args:
        chat_id = message.chat.id
        LOGS_GC_ID = chat_id
        save_logs_gc_id(chat_id)
        send_telegram_log(f"✅ Activity logs group set to this chat ID: <code>{chat_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"This chat (ID: <code>{chat_id}</code>) will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
        return

    try:
        target_id = int(args[0].strip())
        LOGS_GC_ID = target_id
        save_logs_gc_id(target_id)
        send_telegram_log(f"✅ Activity logs group set to chat ID: <code>{target_id}</code> by owner.")
        await message.reply_text(
            f"✅ <b>Activity Logs Group Set Successfully!</b>\n"
            f"Target Chat ID: <code>{target_id}</code> will now receive all real-time bot user activity logs.",
            parse_mode='HTML'
        )
    except ValueError:
        await message.reply_text("⚠️ Invalid Chat ID! Must be a valid integer.", parse_mode='HTML')

async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the Spirit Stones economy leaderboard (top players)."""
    message = update.effective_message
    if not message:
        return
    user = update.effective_user
    if user:
        track_user_data(user)

    if not user_economy_db:
        await message.reply_text("🏆 <b>Leaderboard is currently empty!</b>", parse_mode='HTML')
        return

    sorted_econ = sorted(
        user_economy_db.values(),
        key=lambda x: x.get("spirit_stones", 0) if isinstance(x, dict) else 0,
        reverse=True
    )

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = []

    for idx, eco in enumerate(sorted_econ[:10]):
        if not isinstance(eco, dict):
            continue
        uid = eco.get("user_id")
        ss = eco.get("spirit_stones", 0)
        u_info = users_db.get(str(uid), {})
        uname = u_info.get("username")
        fname = u_info.get("first_name") or f"Cultivator {uid}"

        display_name = f"@{escape_html(str(uname))}" if uname else escape_html(str(fname))
        medal = medals[idx] if idx < len(medals) else f"•"

        lines.append(f"{medal} <b>{display_name}</b> — <code>{ss} SS</code> 💎")

    lb_str = "\n".join(lines)

    text = (
        f"🏆 <b>CULTIVATION SPIRIT STONES LEADERBOARD</b> 💎\n\n"
        f"{lb_str}\n\n"
        f"<i>💡 Earn more Spirit Stones via /daily, /summon, or betting in /mine!</i>"
    )

    await message.reply_text(text, parse_mode='HTML')

BOT_COMMANDS_KNOWLEDGE = """
[Bot Features & Commands Reference Manual for Sylvi]:
1. Cultivation Anime Card System:
   • /summon - Group chat mein random recruit drop summon karne ke liye .
   • /claim, /recruit, /grab - Drop hui card ko apne crew mein claim karne ke liye.
   • /mycards, /roster - Apne owned cards, pagination, tier filters (<24h expiring filter) ke sath dekhne ke liye.
   • /showcard <code> - Specific card ki details (Name, Tier, Worth, Stay Time - no code shown) dekhne ke liye.
   • /discard <code> - Card discard karke uski worth ke barabar Spirit Stones (SS) pane ke liye.
   • /allcards - Global card pool mein available saari cards dekhne ke liye.
   • /daily - Roz ke 500 Spirit Stones (SS) + 1 free Mortal card claim karne ke liye.
   • /pillshop - Card ka stay time badhane ke liye pills kharidne ka shop.
   • /buy <pill> [qty] - Pills buy karne ke liye (e.g. /buy qi 2, /buy eternal).
   • /inventory - Apne Spirit Stones (SS) balance aur pills stock check karne ke liye.
   • /cultivate <code> <pill> - Card ko pill khilane ke liye taaki uska stay time extend ho ya permanent ho jaye.
   • /offer <code> <price_ss> - Apne card ko doosre user ko Spirit Stones ke badle sell/offer par lagane ke liye. (SELLING COMMAND!)
   • /buyoffer <offer_id> - Offer ki gayi card ko buy karne ke liye.
   • /swap <my_code> <their_code> - Same tier ki cards ko aapas mein swap karne ke liye.
   • /myprofile, /profile - Apna game completion %, unique cards collected, aur tier progress profile dekhne ke liye.
   • /leaderboard, /lb - Sabse zyada Spirit Stones (SS) wale top players ka leaderboard dekhne ke liye.
   • /web - Telegram Mini App open karne ke liye interactive web app button.
   • /mine <bet_ss> <mines_count> - Spirit Stones bet karke Mines betting game khelne ke liye (min bet 50 SS).
2. General & Game Commands:
   • /status - Bot ka system health, uptime, database status, aur active game stats dekhne ke liye.
   • /help, /start - Help menu dekhne ke liye.
   • /4words - 4-letter word guessing game khelne ke liye.
   • /statsgc - Group chat message stats aur memory info ke liye.
3. Owner Commands:
   • /addcard <tier> <name> - Global pool mein nayi card add karne ke liye (photo ke sath).
   • /addapi <key> - Nayi Groq AI API key dynamically add karne ke liye.
   • /listapi - Active Groq AI keys ka status dekhne ke liye.
   • /allusers - Tracked users database summary ke liye.
   • /mongoexport & /mongoimport - MongoDB backup export/import ke liye.
   • /removecard <pool_id_or_name> - Global database pool se card permanently delete karne ke liye.
"""

# ==================== MAIN MESSAGE HANDLER ====================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    message = update.message
    chat_type = message.chat.type
    user_text = message.text.strip()
    if not user_text:
        return

    # Collect user data from sender
    user = message.from_user
    if user:
        track_user_data(user)

    # Check if owner is in wizard step 'awaiting_name' for /addcard
    if user and is_owner(user.id) and user.id in owner_addcard_sessions:
        session = owner_addcard_sessions[user.id]
        if session.get("step") == "awaiting_name":
            char_name = user_text.strip()
            tier = session.get("tier", "Mortal")
            photo = session.get("photo", "")
            owner_addcard_sessions.pop(user.id, None)

            await finalize_add_card(update, user.id, tier, char_name, photo)
            return

    # Check if owner is in wizard step for /rarity
    if user and is_owner(user.id) and user.id in owner_rarity_sessions:
        session = owner_rarity_sessions[user.id]
        step = session.get("step")

        if step == "awaiting_card_code":
            target_code = user_text.strip().upper()
            base_card = None
            for c in cards_pool_db:
                if str(c.get("code", "")).upper() == target_code or str(c.get("id", "")).upper() == target_code:
                    base_card = c
                    break
            if not base_card:
                for c in user_cards_db.values():
                    if str(c.get("code", "")).upper() == target_code:
                        cname = c.get("name", "").strip().lower()
                        for pc in cards_pool_db:
                            if pc.get("name", "").strip().lower() == cname:
                                base_card = pc
                                break
                        break

            if not base_card:
                await message.reply_text(
                    f"❌ Card with Code or Pool ID <code>{target_code}</code> not found in database!\n"
                    f"Please send a valid 4-digit card code (or tap /rarity to try again):",
                    parse_mode='HTML'
                )
                return

            session["target_card"] = base_card
            act = session.get("action")

            if act == "change_image":
                session["step"] = "awaiting_image"
                await message.reply_text(
                    f"📷 <b>CARD IMAGE UPDATE (Step 2/2)</b>\n\n"
                    f"Found card: <b>{escape_html(base_card.get('name'))}</b> (Code: <code>{base_card.get('code')}</code>)\n\n"
                    f"Now please <b>send/upload the new Photo/Image</b> for this card in chat:",
                    parse_mode='HTML'
                )
                return
            elif act == "change_name":
                session["step"] = "awaiting_name"
                await message.reply_text(
                    f"✏️ <b>CARD NAME UPDATE (Step 2/2)</b>\n\n"
                    f"Found card: <b>{escape_html(base_card.get('name'))}</b> (Code: <code>{base_card.get('code')}</code>)\n\n"
                    f"Now please type and send the <b>New Character Name</b> in chat:",
                    parse_mode='HTML'
                )
                return

        elif step == "awaiting_name":
            new_name = user_text.strip()
            base_card = session.get("target_card")
            await finalize_rarity_name_update(update, user.id, base_card, new_name)
            return

        elif step == "awaiting_rate_val":
            try:
                new_rate = float(user_text.strip())
                if new_rate < 0.0001 or new_rate > 50.0:
                    await message.reply_text("⚠️ Rate must be a valid number between <code>0.0001</code> and <code>50.0</code>!", parse_mode='HTML')
                    return
            except ValueError:
                await message.reply_text("❌ Invalid number! Please send a valid percentage (e.g. <code>0.001</code> or <code>0.05</code>).", parse_mode='HTML')
                return

            target_tier = session.get("target_tier", "Primordial")
            TIER_PROBABILITIES[target_tier] = new_rate
            save_tier_probabilities(TIER_PROBABILITIES)
            owner_rarity_sessions.pop(user.id, None)

            symbol = TIER_SYMBOLS.get(target_tier, target_tier)
            send_telegram_log(
                f"⚖️ <b>TIER SPAWN RATE UPDATED</b>\n"
                f"🏷️ <b>Tier:</b> {symbol} {target_tier}\n"
                f"📈 <b>New Rate:</b> <code>{new_rate}%</code>"
            )

            await message.reply_text(
                f"✅ <b>SPAWN RATE UPDATED GLOBALLY!</b> 🎉\n\n"
                f"🏷️ <b>Tier:</b> {symbol} <b>{target_tier}</b>\n"
                f"📈 <b>New Spawn Probability:</b> <code>{new_rate}%</code>\n\n"
                f"<i>This new rarity rate is now active globally for all summons/drops!</i>",
                parse_mode='HTML'
            )
            return

    base_text = user_text.lower().strip().split('@')[0]
    bot_uname = context.bot.username.lower() if (context.bot and context.bot.username) else ""
    is_web_cmd = base_text in ["/web", "web"]
    if bot_uname and user_text.lower().strip() == f"/web@{bot_uname}":
        is_web_cmd = True

    if is_web_cmd:
        await web_command(update, context)
        return

    # Ignore command messages starting with '/'
    if user_text.startswith('/'):
        return

    # Collect user data from replied-to user if present
    if message.reply_to_message and message.reply_to_message.from_user:
        track_user_data(message.reply_to_message.from_user)

    sender_name = user.first_name if user else "Bantai"
    sender_user_handle = get_user_tag(user)
    chat_id = message.chat.id

    u_info_log = f"{escape_html(sender_name)} ({sender_user_handle}, ID: <code>{user.id if user else 0}</code>)"
    if user_text.startswith('/'):
        c_cmd_log = user_text.split()[0]
        c_loc_log = "Private DM" if chat_type == 'private' else f"Group <code>{chat_id}</code>"
        send_telegram_log(f"💬 <b>Command Executed:</b> <code>{escape_html(c_cmd_log)}</code>\n👤 <b>User:</b> {u_info_log}\n📍 <b>Location:</b> {c_loc_log}")
    elif chat_type == 'private':
        send_telegram_log(f"✉️ <b>Private DM Message:</b>\n👤 <b>User:</b> {u_info_log}\n💬 <b>Msg:</b> <i>{escape_html(user_text[:200])}</i>")

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

                eco = get_user_economy(user.id)
                eco["spirit_stones"] += 100
                save_user_economy(user_economy_db)
                new_ss_bal = eco["spirit_stones"]

                text = (
                    f"🎉 <b>CONGRATULATIONS {uname}! YOU WON!</b> 🏆\n\n"
                    f"<b>4-letter mode</b>\n"
                    f"<i>{tries}/30 guesses</i>\n\n"
                    f"{hint_display}\n\n"
                    f"🎯 Secret Word: <b>{secret}</b>\n"
                    f"💎 <b>Reward Received:</b> <code>+100 Spirit Stones (SS)</code>\n"
                    f"💰 <b>New SS Balance:</b> <code>{new_ss_bal} SS</code>\n\n"
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

        bot_username = context.bot.username if context.bot else ""
        is_direct_bot_reply = False
        is_bot_mentioned = False
        is_name_called = False

        if message.reply_to_message and message.reply_to_message.from_user:
            if context.bot and message.reply_to_message.from_user.id == context.bot.id:
                is_direct_bot_reply = True

        if bot_username and f"@{bot_username.lower()}" in user_text.lower():
            is_bot_mentioned = True

        if re.search(r'\bsylvi\b', user_text, re.IGNORECASE):
            is_name_called = True

        should_reply = is_direct_bot_reply or is_bot_mentioned or is_name_called

        if not should_reply:
            logger.info(f"Skipping DM message from {sender_name} ({user_id}) - 'Sylvi' not called/mentioned.")
            return

        logger.info(f"Processing DM message from {sender_name} ({user_id}){role_label} (Sylvi called): {user_text[:30]}...")

        owner_context = " Aap apne Owner ke saath baat kar rahe ho." if user_is_owner else ""
        messages_payload = [
            {
                "role": "system",
                "content": (
                    f"Aapka naam Sylvi hai. Aap ek friendly, smart aur helpful human-like Hinglish AI assistant ho.{owner_context} "
                    "Aap Telegram user ke saath private DM mein baat kar rahe ho. "
                    "Friendly, polite, natural human tone aur helpful manner mein clear Hinglish/English mein reply do.\n\n"
                    f"{BOT_COMMANDS_KNOWLEDGE}"
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

        # 100-MESSAGE AUTOMATIC CULTIVATION CARD DROP TRIGGER
        if cards_pool_db:
            group_message_counts[chat_id] = group_message_counts.get(chat_id, 0) + 1
            if group_message_counts[chat_id] >= 100:
                group_message_counts[chat_id] = 0
                dropped_tier = roll_card_tier()
                matching_pool = [c for c in cards_pool_db if c.get("tier", "").lower() == dropped_tier.lower()]

                if matching_pool:
                    base_card = random.choice(matching_pool)
                else:
                    base_card = random.choice(cards_pool_db)

                c_name = base_card.get("name", "Cultivator")
                dropped_tier = base_card.get("tier", "Mortal")
                c_photo = base_card.get("photo", "")

                active_drops[chat_id] = {
                    "name": c_name,
                    "tier": dropped_tier,
                    "photo": c_photo,
                    "worth": TIER_WORTH.get(dropped_tier, 200)
                }

                symbol = TIER_SYMBOLS.get(dropped_tier, "⚪️")
                c_name_esc = escape_html(c_name)

                drop_msg = (
                    f"A {dropped_tier} {symbol} recruit {c_name_esc} has appeared...\n\n"
                    f"Use /recruit to bring them into your crew"
                )
                if c_photo:
                    try:
                        await message.reply_photo(photo=c_photo, caption=drop_msg)
                    except Exception:
                        await message.reply_text(drop_msg)
                else:
                    await message.reply_text(drop_msg)

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
            f"{BOT_COMMANDS_KNOWLEDGE}\n\n"
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

web_active_mines: Dict[int, Dict] = {}  # user_id -> game_state for web mini app

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query_params = urllib.parse.parse_qs(parsed_path.query)

        if path in ["/", "/app", "/webapp"]:
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mini_app.html")
            if os.path.exists(html_path):
                try:
                    with open(html_path, "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(content)
                    return
                except Exception as e:
                    logger.error(f"Error serving mini_app.html: {e}")

        elif path == "/api/user":
            user_id_str = query_params.get("user_id", ["5193216992"])[0]
            try:
                user_id = int(user_id_str)
            except ValueError:
                user_id = 5193216992

            eco = get_user_economy(user_id)
            user_cards = [c for c in user_cards_db.values() if c.get("owner_id") == user_id]

            total_pool_count = len(cards_pool_db)
            owned_card_names = {c.get("name", "").strip().lower() for c in user_cards}
            unique_owned_count = sum(1 for b in cards_pool_db if b.get("name", "").strip().lower() in owned_card_names) if total_pool_count > 0 else len(owned_card_names)
            completion_pct = round((unique_owned_count / total_pool_count * 100.0) if total_pool_count > 0 else 0.0, 1)

            cards_list = []
            for c in user_cards:
                cards_list.append({
                    "code": c.get("code"),
                    "name": c.get("name"),
                    "tier": c.get("tier"),
                    "worth": get_card_worth(c),
                    "stay_time": format_time_left(c)
                })

            data = {
                "spirit_stones": eco["spirit_stones"],
                "pills": eco.get("pills", {}),
                "pill_catalog": PILL_CATALOG,
                "roster_count": len(user_cards),
                "unique_collected": unique_owned_count,
                "total_pool": total_pool_count,
                "completion_pct": completion_pct,
                "user_cards": cards_list
            }

            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Sylvi Groq AI Bot is running")

    def do_POST(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path

        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        try:
            req = json.loads(post_data.decode('utf-8'))
        except Exception:
            req = {}

        if path == "/api/buy":
            user_id = int(req.get("user_id", 0))
            pill_num = str(req.get("pill_num", ""))
            item = PILL_CATALOG.get(pill_num)

            if not item or not user_id:
                response = {"success": False, "message": "Invalid request"}
            else:
                eco = get_user_economy(user_id)
                price = item["price"]
                pill_name = item["name"]

                if eco["spirit_stones"] < price:
                    response = {"success": False, "message": f"Insufficient Spirit Stones! Required: {price} SS"}
                else:
                    eco["spirit_stones"] -= price
                    eco["pills"][pill_name] = eco["pills"].get(pill_name, 0) + 1
                    save_user_economy(user_economy_db)

                    user_cards = [c for c in user_cards_db.values() if c.get("owner_id") == user_id]
                    total_pool_count = len(cards_pool_db)
                    owned_card_names = {c.get("name", "").strip().lower() for c in user_cards}
                    unique_owned_count = sum(1 for b in cards_pool_db if b.get("name", "").strip().lower() in owned_card_names) if total_pool_count > 0 else len(owned_card_names)
                    completion_pct = round((unique_owned_count / total_pool_count * 100.0) if total_pool_count > 0 else 0.0, 1)

                    cards_list = [{"code": c.get("code"), "name": c.get("name"), "tier": c.get("tier"), "worth": get_card_worth(c), "stay_time": format_time_left(c)} for c in user_cards]

                    app_data = {
                        "spirit_stones": eco["spirit_stones"],
                        "pills": eco.get("pills", {}),
                        "pill_catalog": PILL_CATALOG,
                        "roster_count": len(user_cards),
                        "unique_collected": unique_owned_count,
                        "total_pool": total_pool_count,
                        "completion_pct": completion_pct,
                        "user_cards": cards_list
                    }
                    response = {"success": True, "pill_name": pill_name, "app_data": app_data}

            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))
            return

        elif path == "/api/daily":
            user_id = int(req.get("user_id", 0))
            if not user_id:
                response = {"success": False, "message": "Invalid user ID"}
            else:
                eco = get_user_economy(user_id)
                now = time.time()
                last_daily = eco.get("last_daily", 0.0)
                cooldown = 24 * 3600
                time_passed = now - last_daily

                if time_passed < cooldown:
                    remaining_sec = cooldown - time_passed
                    hours = int(remaining_sec // 3600)
                    mins = int((remaining_sec % 3600) // 60)
                    response = {"success": False, "message": f"⏳ Daily reward already claimed! Please wait {hours}h {mins}m."}
                else:
                    eco["spirit_stones"] += 500
                    eco["last_daily"] = now
                    save_user_economy(user_economy_db)

                    mortal_card = create_card_instance(user_id, forced_tier="Mortal")

                    user_cards = [c for c in user_cards_db.values() if c.get("owner_id") == user_id]
                    total_pool_count = len(cards_pool_db)
                    owned_card_names = {c.get("name", "").strip().lower() for c in user_cards}
                    unique_owned_count = sum(1 for b in cards_pool_db if b.get("name", "").strip().lower() in owned_card_names) if total_pool_count > 0 else len(owned_card_names)
                    completion_pct = round((unique_owned_count / total_pool_count * 100.0) if total_pool_count > 0 else 0.0, 1)

                    cards_list = [{"code": c.get("code"), "name": c.get("name"), "tier": c.get("tier"), "worth": get_card_worth(c), "stay_time": format_time_left(c)} for c in user_cards]

                    app_data = {
                        "spirit_stones": eco["spirit_stones"],
                        "pills": eco.get("pills", {}),
                        "pill_catalog": PILL_CATALOG,
                        "roster_count": len(user_cards),
                        "unique_collected": unique_owned_count,
                        "total_pool": total_pool_count,
                        "completion_pct": completion_pct,
                        "user_cards": cards_list
                    }
                    response = {"success": True, "message": f"💎 Daily Claimed! +500 SS & Mortal Card ({mortal_card['name']})", "app_data": app_data}

            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))
            return

        elif path == "/api/mine/start":
            user_id = int(req.get("user_id", 0))
            bet = int(req.get("bet", 50))
            mines_count = int(req.get("mines_count", 3))

            eco = get_user_economy(user_id)
            if bet < 50 or eco["spirit_stones"] < bet or mines_count < 1 or mines_count > 24:
                response = {"success": False, "message": "Invalid bet or insufficient Spirit Stones!"}
            else:
                eco["spirit_stones"] -= bet
                save_user_economy(user_economy_db)

                mine_positions = random.sample(range(25), mines_count)
                game_state = {
                    "bet": bet,
                    "mines_count": mines_count,
                    "mine_positions": mine_positions,
                    "revealed": [],
                    "tiles_opened": 0,
                    "multiplier": 1.00,
                    "potential": bet,
                    "game_over": False,
                    "won": False,
                    "winnings": 0
                }
                web_active_mines[user_id] = game_state
                response = {"success": True, "game": game_state}

            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))
            return

        elif path == "/api/mine/click":
            user_id = int(req.get("user_id", 0))
            tile_idx = int(req.get("tile_idx", 0))
            game = web_active_mines.get(user_id)

            if not game or game["game_over"]:
                response = {"success": False, "message": "No active game"}
            else:
                if tile_idx in game["mine_positions"]:
                    game["game_over"] = True
                    game["won"] = False
                    game["winnings"] = 0
                else:
                    if tile_idx not in game["revealed"]:
                        game["revealed"].append(tile_idx)
                        game["tiles_opened"] += 1
                        game["multiplier"] = calculate_mines_multiplier(25, game["mines_count"], game["tiles_opened"])
                        game["potential"] = int(game["bet"] * game["multiplier"])

                        safe_total = 25 - game["mines_count"]
                        if game["tiles_opened"] == safe_total:
                            game["game_over"] = True
                            game["won"] = True
                            game["winnings"] = game["potential"]
                            eco = get_user_economy(user_id)
                            eco["spirit_stones"] += game["winnings"]
                            save_user_economy(user_economy_db)

                response = {"success": True, "game": game}

            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))
            return

        elif path == "/api/mine/cashout":
            user_id = int(req.get("user_id", 0))
            game = web_active_mines.get(user_id)

            if not game or game["game_over"] or game["tiles_opened"] <= 0:
                response = {"success": False, "message": "Cannot cash out"}
            else:
                game["game_over"] = True
                game["won"] = True
                game["winnings"] = game["potential"]
                eco = get_user_economy(user_id)
                eco["spirit_stones"] += game["winnings"]
                save_user_economy(user_economy_db)
                response = {"success": True, "game": game, "winnings": game["winnings"]}

            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP access logs to keep console logs clean

def start_health_check_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), HealthCheckHandler)
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

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify about the error."""
    logger.error(msg="Exception while handling an update:", exc_info=context.error)
    try:
        err_str = str(context.error)
        send_telegram_log(f"❌ <b>BOT EXCEPTION ERROR</b>\n<pre>{escape_html(err_str)}</pre>")
    except Exception:
        pass

def main():
    logger.info("Starting Sylvi Groq AI Telegram Bot (Owner DM + Dynamic API Keys Active)...")
    start_health_check_server()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Register error handler
    app.add_error_handler(error_handler)

    # Public & Owner Command Handlers
    app.add_handler(CommandHandler("start", help_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("web", web_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("lb", leaderboard_command))
    app.add_handler(CommandHandler("logsgc", logsgc_command))
    app.add_handler(CommandHandler("addapi", addapi_command))
    app.add_handler(CommandHandler("listapi", listapi_command))
    app.add_handler(CommandHandler("statsgc", statsgc_command))
    app.add_handler(CommandHandler("allusers", allusers_command))
    app.add_handler(CommandHandler("mongoexport", mongoexport_command))
    app.add_handler(CommandHandler("mongoimport", mongoimport_command))
    app.add_handler(CommandHandler("4words", fourwords_command))
    app.add_handler(CommandHandler("join4", join4_command))

    # Cultivation Anime Card Command Handlers
    app.add_handler(CommandHandler("summon", summon_command))
    app.add_handler(CommandHandler("mycards", mycards_command))
    app.add_handler(CommandHandler("roster", mycards_command))
    app.add_handler(CommandHandler("favcard", favcard_command))
    app.add_handler(CommandHandler(["scard", "showcard"], showcard_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("discard", discard_command))
    app.add_handler(CommandHandler(["rcard", "removecard"], removecard_command))
    app.add_handler(CommandHandler("allcards", allcards_command))
    app.add_handler(CommandHandler("daily", daily_command))
    app.add_handler(CommandHandler("pillshop", pillshop_command))
    app.add_handler(CommandHandler("buypill", buypill_command))
    app.add_handler(CommandHandler("buy", buypill_command))
    app.add_handler(CommandHandler("inventory", inventory_command))
    app.add_handler(CommandHandler("myprofile", myprofile_command))
    app.add_handler(CommandHandler("profile", myprofile_command))
    app.add_handler(CommandHandler("mine", mine_command))
    app.add_handler(CommandHandler("mines", mine_command))
    app.add_handler(CommandHandler("cultivate", cultivate_command))
    app.add_handler(CommandHandler("addcard", addcard_command))
    app.add_handler(CommandHandler("rarity", rarity_command))
    app.add_handler(CommandHandler("offer", offer_command))
    app.add_handler(CommandHandler("buyoffer", buyoffer_command))
    app.add_handler(CommandHandler("swap", swap_command))
    app.add_handler(CommandHandler("claim", claim_command))
    app.add_handler(CommandHandler("recruit", claim_command))
    app.add_handler(CommandHandler("grab", claim_command))

    # Callback Queries Handler (For /addcard Wizard & Cultivation Inline Buttons)
    app.add_handler(CallbackQueryHandler(addcard_callback_handler, pattern="^addcard_"))
    app.add_handler(CallbackQueryHandler(rarity_callback_handler, pattern="^rarity_act_"))
    app.add_handler(CallbackQueryHandler(connect4_callback_handler, pattern="^c4_col_"))
    app.add_handler(CallbackQueryHandler(pill_cultivate_callback_handler, pattern="^(buy_pill_|cult_|open_pillshop)"))
    app.add_handler(CallbackQueryHandler(mines_callback_handler, pattern="^mines_"))
    app.add_handler(CallbackQueryHandler(mycards_callback_handler, pattern="^mycards_"))
    app.add_handler(CallbackQueryHandler(search_card_callback_handler, pattern="^search_card_"))

    # Photo Messages Handler (For /addcard Wizard Photo Upload)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))

    # Member Join Event Handler
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_members_handler))

    # General Text Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()

if __name__ == '__main__':
    main()
