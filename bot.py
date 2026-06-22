import os
import re
import json
import asyncio
import logging
import random
import time
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PasswordHashInvalidError
import google.generativeai as genai

# Load environment variables
load_dotenv()

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("UserbotController")

# Configurations
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

from pymongo import MongoClient
import pymongo

CONFIG_FILE = "config.json"
MONGO_URL = os.getenv("MONGO_URL") or os.getenv("MONGO_URI")
mongo_client = None
db = None
config_col = None
members_col = None

# Configure MongoDB connection if URL is provided
if MONGO_URL:
    try:
        # Prevent long timeouts during connection issues
        mongo_client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)
        db = mongo_client["chatbot_userbot"]
        config_col = db["config"]
        members_col = db["members"]
        logger.info("Successfully connected to MongoDB!")
    except Exception as e:
        logger.error(f"Error connecting to MongoDB: {e}")
        mongo_client = None

config = {}

def load_config():
    global config
    if mongo_client:
        try:
            data = config_col.find_one({"_id": "bot_config"})
            if data:
                # Remove MongoDB _id if present to keep json compatibility
                data.pop("_id", None)
                config = data
            else:
                config = {}
        except Exception as e:
            logger.error(f"Error loading config from MongoDB: {e}")
            config = {}
    else:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                config = {}
        else:
            config = {}
            
    # Default configuration values
    config.setdefault("owner_id", None)
    config.setdefault("groups", {})              # Format: {chat_id_str: {"name": str, "username": str, "enabled": bool}}
    config.setdefault("all_groups_enabled", False)
    config.setdefault("auto_chat_enabled", False)
    config.setdefault("greeting_interval", 300)  # Default 5 minutes
    config.setdefault("tagger_enabled", True)
    config.setdefault("tagger_interval", 900)    # Default 15 minutes
    config.setdefault("tagger_batch_size", 5)
    config.setdefault("active_users", [])        # Fallback active users

# Initial configuration load
load_config()

def save_config():
    try:
        if mongo_client:
            # We copy the config and upsert
            data = dict(config)
            data["_id"] = "bot_config"
            config_col.replace_one({"_id": "bot_config"}, data, upsert=True)
        else:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving config: {e}")

# --- Active Members Database Utilities ---
MEMBERS_FILE = "members.json"
members_db = {}

def load_members():
    global members_db
    if mongo_client:
        return
    if os.path.exists(MEMBERS_FILE):
        try:
            with open(MEMBERS_FILE, "r", encoding="utf-8") as f:
                members_db = json.load(f)
        except Exception as e:
            logger.error(f"Error loading members from file: {e}")
            members_db = {}
    else:
        members_db = {}

def save_members():
    if mongo_client:
        return
    try:
        with open(MEMBERS_FILE, "w", encoding="utf-8") as f:
            json.dump(members_db, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving members to file: {e}")

def record_member(chat_id_str, chat, sender):
    user_id_str = str(sender.id)
    username = getattr(sender, "username", None)
    first_name = getattr(sender, "first_name", None) or "bhai"
    last_name = getattr(sender, "last_name", None)
    
    if mongo_client:
        try:
            doc_id = f"{chat_id_str}:{user_id_str}"
            doc = {
                "_id": doc_id,
                "group_id": chat_id_str,
                "user_id": user_id_str,
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "last_active": int(time.time())
            }
            members_col.replace_one({"_id": doc_id}, doc, upsert=True)
        except Exception as e:
            logger.error(f"Error saving member to MongoDB: {e}")
    else:
        load_members()
        if chat_id_str not in members_db:
            members_db[chat_id_str] = {}
            
        members_db[chat_id_str][user_id_str] = {
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "last_active": int(time.time())
        }
        
        # Bound tracking to 500 members per group to save space
        if len(members_db[chat_id_str]) > 500:
            sorted_m = sorted(members_db[chat_id_str].items(), key=lambda x: x[1].get("last_active", 0))
            del members_db[chat_id_str][sorted_m[0][0]]
            
        save_members()

def get_group_members(chat_id_str):
    if mongo_client:
        try:
            cursor = members_col.find({"group_id": chat_id_str}).sort("last_active", -1).limit(100)
            res = {}
            for doc in cursor:
                res[doc["user_id"]] = {
                    "username": doc.get("username"),
                    "first_name": doc.get("first_name") or "bhai",
                    "last_name": doc.get("last_name")
                }
            return res
        except Exception as e:
            logger.error(f"Error fetching group members from MongoDB: {e}")
            return {}
    else:
        load_members()
        return members_db.get(chat_id_str, {})

def get_total_members_tracked():
    if mongo_client:
        try:
            return members_col.count_documents({})
        except Exception as e:
            logger.error(f"Error counting members in MongoDB: {e}")
            return 0
    else:
        load_members()
        return sum(len(m_list) for m_list in members_db.values())

def record_group(chat_id_str, chat):
    title = getattr(chat, "title", "Unknown Group")
    username = getattr(chat, "username", None)
    groups = config.setdefault("groups", {})
    
    updated = False
    if chat_id_str not in groups:
        groups[chat_id_str] = {
            "name": title,
            "username": username,
            "enabled": True
        }
        updated = True
    else:
        if groups[chat_id_str].get("name") != title or groups[chat_id_str].get("username") != username:
            groups[chat_id_str]["name"] = title
            groups[chat_id_str]["username"] = username
            updated = True
            
    if updated:
        save_config()


# Validate Telegram API credentials
if not API_ID or not API_HASH or not BOT_TOKEN:
    print("\n[!] Please fill in the API_ID, API_HASH, and BOT_TOKEN in the .env file first!\n")
    API_ID = API_ID or "YOUR_API_ID"
    API_HASH = API_HASH or "YOUR_API_HASH"
    BOT_TOKEN = BOT_TOKEN or "YOUR_BOT_TOKEN"

# Gemini API setup
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    # Using gemini-1.5-flash for fast and cost-effective chat completions
    gemini_model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=(
            "You are a real young Indian guy (boy/teenager/young adult) chatting with close friends in a Telegram group.\n"
            "Your task is to chat in casual, natural 'Hinglish' (Hindi written in Latin script/English alphabets).\n"
            "Tone: Pure 'boy feelings' / sweet, caring, emotional, loyal Indian bro vibes. Talk like a real brother/friend.\n"
            "Slang/Words: Use words like 'bhai', 'yaar', 'bro', 'meri jaan', 'sahi me', 'kya scene hai', 'tension mat le', 'sahi hai', 'dil se', 'kya haal'.\n"
            "RULES:\n"
            "1. Keep your replies very short, simple, and direct (usually 1-2 sentences maximum, keep it natural).\n"
            "2. Do NOT use AI-like emojis (e.g., 🤖, ✨, 🚀, 📝). Use casual human emojis (e.g., 😂, ❤️, 😭, 💀, 👍, 🥺).\n"
            "3. Do NOT sound like an AI assistant. Speak like a regular Indian friend.\n"
            "4. If someone is sad or shares a problem, show real empathy and support ('koi na bhai', 'tension mat le main hu na').\n"
            "5. If someone accuses you of being a bot, reply with high emotion and denial ('kya yaar main bot lagta hu?', 'dil se bura lagta hai bhai, real hu main')."
        )
    )
else:
    gemini_model = None
    logger.warning("GEMINI_API_KEY not found. AI Chat replies will be disabled.")

# Initialize Clients
bot_client = TelegramClient("controller_bot", int(API_ID) if API_ID.isdigit() else 0, API_HASH).start(bot_token=BOT_TOKEN)
user_client = TelegramClient("user_session", int(API_ID) if API_ID.isdigit() else 0, API_HASH)

# States for login flow and group setup
login_state = {}
# Format: {user_id: {"step": "phone/otp/2fa", "phone": str, "phone_code_hash": str, "client": TelegramClient}}
user_state = {}
# Format: {user_id: {"expecting": "group_link/interval"}}

# Bot Accusation Patterns
BOT_ACCUSATION_WORDS = ["bot", "robot", "userbot", "script", "automation", "auto", "machine", "fake account"]

# Hinglish Bot Denials
BOT_DENIALS = [
    "Abe main bot nahi hu 😂, kya bol raha hai yaar.",
    "Haan haan, sabko bot hi bol diya karo! Main real insaan hu bhai.",
    "Kya kuch bhi? Bot kyu hounga mai? 😂",
    "Abe real banda hu mai, bot lagta hu kya? 💀",
    "Nahi bhai, bot nahi hu mai. Real user hu.",
    "Abey bot hoga tera khandaan, mai real hu 😭",
    "Hahaha, lagta hai tumne pehli baar koi real active user dekha hai.",
    "Lol, main bot nahi hu yaar, bas active rehta hu thoda."
]

# Random Hinglish Greetings for Rank Boosting
RANDOM_GREETINGS = [
    "Hi {}, kaise ho?",
    "Hey {}, kya chal raha hai?",
    "Hello {}, sab badhiya?",
    "Yo {}, kya haal chaal?",
    "Oi {}, online hi rehte ho kya hamesha? 😂",
    "{} haan bhai, kya scene?",
    "{} hello yaar, kaise ho",
    "Kya chal raha hai group me?",
    "Bohot shanti hai aaj group me 😂",
    "Aur batao sab, kya chal raha hai",
    "Sahi hai yaar"
]

# Helper: Parse Group Link or Username
def parse_group_identifier(input_str):
    input_str = input_str.strip()
    if input_str.startswith("https://t.me/"):
        # Handle invite links or public usernames
        part = input_str.replace("https://t.me/", "")
        if part.startswith("+") or part.startswith("joinchat/"):
            return input_str  # Invite link
        return part.split("/")[0]  # Public username
    elif input_str.startswith("@"):
        return input_str[1:]
    return input_str

# Helper: Generate Control Panel Keyboard
async def get_control_keyboard():
    status_emoji = "🟢 ON" if config.get("auto_chat_enabled", False) else "🔴 OFF"
    tagger_emoji = "🟢 ON" if config.get("tagger_enabled", True) else "🔴 OFF"
    
    try:
        is_auth = await user_client.is_user_authorized() if user_client.is_connected() else False
    except Exception:
        is_auth = False
        
    auth_emoji = "✅ Connected" if is_auth else "🔑 Disconnected"
    
    buttons = [
        [
            Button.inline(f"👤 Auth: {auth_emoji}", data="auth_flow" if not is_auth else "confirm_logout"),
            Button.inline(f"⚡ Chatbot: {status_emoji}", data="toggle_userbot")
        ],
        [
            Button.inline(f"🏷️ Tagger: {tagger_emoji}", data="toggle_tagger"),
            Button.inline("👥 Manage Groups", data="go_groups_menu")
        ],
        [
            Button.inline("⚙️ Settings", data="go_settings_menu"),
            Button.inline("🏓 Ping", data="ping")
        ],
        [
            Button.inline("🔄 Refresh Status", data="refresh_status")
        ]
    ]
    return buttons

# Helper: Build Control Panel Text
async def get_status_text():
    chat_status = "Active 🟢" if config.get("auto_chat_enabled", False) else "Inactive 🔴"
    tagger_status = "Active 🟢" if config.get("tagger_enabled", True) else "Inactive 🔴"
    
    try:
        user_auth = await user_client.is_user_authorized() if user_client.is_connected() else False
    except Exception:
        user_auth = False
        
    user_status = "LoggedIn ✅" if user_auth else "Not Logged In ❌"
    
    groups_count = len(config.get("groups", {}))
    all_groups_mode = "Enabled 🌐" if config.get("all_groups_enabled", False) else "Disabled 👥"
    
    load_members()
    total_members_tracked = sum(len(m_list) for m_list in members_db.values())
    
    text = (
        "🤖 **Userbot Controller Bot** 🤖\n\n"
        f"👤 **Userbot Account Status:** {user_status}\n"
        f"⚡ **AI Chatbot Loop:** {chat_status}\n"
        f"🏷️ **Active Tagger Loop:** {tagger_status}\n"
        f"🌐 **Group Target Mode:** {all_groups_mode}\n"
        f"👥 **Managed Groups:** `{groups_count}` groups\n"
        f"💾 **Tracked Chat Members:** `{total_members_tracked}` in DB\n\n"
        "Use the buttons below to control the bot. Settings are saved automatically."
    )
    return text

# Helper: Generate Groups Submenu Text & Keyboard
def get_groups_text():
    all_groups_mode = "🌐 ON (All Groups)" if config.get("all_groups_enabled", False) else "👥 OFF (Only Selected)"
    groups = config.get("groups", {})
    
    text = (
        "👥 **Manage Groups Configuration**\n\n"
        f"🌐 **All Groups Mode:** `{all_groups_mode}`\n"
        "When ON, the bot operates in all groups the userbot has joined. "
        "When OFF, it only runs in enabled groups list below.\n\n"
        "**Currently Configured Groups:**\n"
    )
    
    if not groups:
        text += "_No groups configured yet. Send messages in groups or add one below._"
    else:
        for idx, (gid, ginfo) in enumerate(groups.items(), start=1):
            gstatus = "🟢" if ginfo.get("enabled", True) else "🔴"
            gname = ginfo.get("name") or "Unknown"
            guser = f" (@{ginfo['username']})" if ginfo.get("username") else ""
            text += f"{idx}. {gstatus} **{gname}** `{gid}`{guser}\n"
            
    return text

def get_groups_keyboard():
    all_groups_status = "🌐 All Groups Mode: " + ("ON" if config.get("all_groups_enabled", False) else "OFF")
    
    buttons = [
        [Button.inline(all_groups_status, data="toggle_all_groups")]
    ]
    
    groups = config.get("groups", {})
    for gid, ginfo in groups.items():
        gname = ginfo.get("name") or "Unknown"
        truncated_name = gname[:15] + "..." if len(gname) > 15 else gname
        status_emoji = "🟢" if ginfo.get("enabled", True) else "🔴"
        
        buttons.append([
            Button.inline(f"{status_emoji} {truncated_name}", data=f"toggle_grp_{gid}"),
            Button.inline("🗑️ Delete", data=f"del_grp_{gid}")
        ])
        
    buttons.append([
        Button.inline("➕ Add Group", data="add_grp_flow"),
        Button.inline("🏡 Main Menu", data="go_main_menu")
    ])
    return buttons

# Helper: Generate Settings Submenu Text & Keyboard
def get_settings_text():
    chat_interval = config.get("greeting_interval", 300)
    tagger_interval = config.get("tagger_interval", 900)
    batch_size = config.get("tagger_batch_size", 5)
    
    text = (
        "⚙️ **System Settings Configuration**\n\n"
        f"⏱️ **General Chat Interval:** `{chat_interval}s`\n"
        "_Interval for periodic random greetings/rank booster._\n\n"
        f"⏱️ **Tagger Interval:** `{tagger_interval}s`\n"
        "_Interval for periodic group member tagging mentions._\n\n"
        f"👥 **Tagger Batch Size:** `{batch_size}` members\n"
        "_Number of group members tagged per message to avoid spam bans._"
    )
    return text

def get_settings_keyboard():
    buttons = [
        [
            Button.inline("⏱️ Set Chat Interval", data="set_chat_interval_flow"),
            Button.inline("⏱️ Set Tagger Interval", data="set_tagger_interval_flow")
        ],
        [
            Button.inline("👥 Set Tagger Batch Size", data="set_batch_size_flow")
        ],
        [
            Button.inline("🏡 Main Menu", data="go_main_menu")
        ]
    ]
    return buttons


# Controller Bot Start Handler
@bot_client.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    if config["owner_id"] is None:
        config["owner_id"] = event.sender_id
        save_config()
        await event.respond("👑 You have been set as the owner of this Controller Bot!")
    
    if event.sender_id != config["owner_id"]:
        return

    text = await get_status_text()
    await event.respond(text, buttons=await get_control_keyboard())

# Controller Bot Callback Queries Handler
@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
    if event.sender_id != config["owner_id"]:
        await event.answer("You are not the owner!", alert=True)
        return

    data = event.data.decode("utf-8")
    
    # 1. Main Navigation / Refresh
    if data == "refresh_status" or data == "go_main_menu":
        text = await get_status_text()
        await event.edit(text, buttons=await get_control_keyboard())
        await event.answer("Main Menu")
        
    elif data == "go_groups_menu":
        text = get_groups_text()
        await event.edit(text, buttons=get_groups_keyboard())
        await event.answer("Group Settings")
        
    elif data == "go_settings_menu":
        text = get_settings_text()
        await event.edit(text, buttons=get_settings_keyboard())
        await event.answer("System Settings")
        
    # 2. Status Toggles
    elif data == "toggle_userbot":
        config["auto_chat_enabled"] = not config.get("auto_chat_enabled", False)
        save_config()
        text = await get_status_text()
        await event.edit(text, buttons=await get_control_keyboard())
        await event.answer(f"Chatbot {'ON' if config['auto_chat_enabled'] else 'OFF'}")
        
    elif data == "toggle_tagger":
        config["tagger_enabled"] = not config.get("tagger_enabled", True)
        save_config()
        text = await get_status_text()
        await event.edit(text, buttons=await get_control_keyboard())
        await event.answer(f"Tagger {'ON' if config['tagger_enabled'] else 'OFF'}")
        
    elif data == "toggle_all_groups":
        config["all_groups_enabled"] = not config.get("all_groups_enabled", False)
        save_config()
        text = get_groups_text()
        await event.edit(text, buttons=get_groups_keyboard())
        await event.answer(f"All Groups Mode {'ON' if config['all_groups_enabled'] else 'OFF'}")
        
    # 3. Group Operations
    elif data.startswith("toggle_grp_"):
        gid = data.replace("toggle_grp_", "")
        if gid in config.get("groups", {}):
            config["groups"][gid]["enabled"] = not config["groups"][gid].get("enabled", True)
            save_config()
            text = get_groups_text()
            await event.edit(text, buttons=get_groups_keyboard())
            await event.answer("Group status toggled!")
        else:
            await event.answer("Group not found!", alert=True)
            
    elif data.startswith("del_grp_"):
        gid = data.replace("del_grp_", "")
        if gid in config.get("groups", {}):
            config["groups"].pop(gid)
            save_config()
            text = get_groups_text()
            await event.edit(text, buttons=get_groups_keyboard())
            await event.answer("Group deleted!")
        else:
            await event.answer("Group not found!", alert=True)

    # 4. Inputs Trigger Flow
    elif data == "add_grp_flow":
        user_state[event.sender_id] = {"expecting": "group_link"}
        await event.edit(
            "➕ **Add Target Group**\n\n"
            "Please send the group link (e.g. `https://t.me/groupusername`, `@groupusername`, or chat/group ID):",
            buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
        )
        await event.answer()
        
    elif data == "set_chat_interval_flow":
        user_state[event.sender_id] = {"expecting": "chat_interval"}
        await event.edit(
            "⏱️ **Set Chatbot Interval**\n\n"
            "Please send the interval in seconds for general greetings/chats (minimum 60s):",
            buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
        )
        await event.answer()
        
    elif data == "set_tagger_interval_flow":
        user_state[event.sender_id] = {"expecting": "tagger_interval"}
        await event.edit(
            "⏱️ **Set Tagger Interval**\n\n"
            "Please send the interval in seconds for periodic member tagging (minimum 60s):",
            buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
        )
        await event.answer()
        
    elif data == "set_batch_size_flow":
        user_state[event.sender_id] = {"expecting": "tagger_batch_size"}
        await event.edit(
            "👥 **Set Tagger Batch Size**\n\n"
            "Please send the number of members to tag per message (1 to 15):",
            buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
        )
        await event.answer()
        
    # 5. Cancel / Back Flow
    elif data == "cancel_flow":
        user_state.pop(event.sender_id, None)
        login_state.pop(event.sender_id, None)
        text = await get_status_text()
        await event.edit(text, buttons=await get_control_keyboard())
        await event.answer("Cancelled")
        
    # 6. Diagnostics
    elif data == "ping":
        start_time = time.time()
        await event.answer("Pinging...")
        bot_latency = int((time.time() - start_time) * 1000)
        
        user_latency = "N/A"
        if user_client.is_connected():
            try:
                u_start = time.time()
                await user_client.get_me()
                user_latency = f"{int((time.time() - u_start) * 1000)}ms"
            except Exception:
                user_latency = "Error"
                
        await event.respond(
            f"🏓 **Pong!**\n\n"
            f"• Controller Bot: `{bot_latency}ms`\n"
            f"• Userbot session: `{user_latency}`",
            buttons=[[Button.inline("🏡 Main Menu", data="go_main_menu")]]
        )
        
    # 7. Userbot Authentication
    elif data == "auth_flow":
        await event.answer("Starting Login Process...")
        login_state[event.sender_id] = {"step": "phone"}
        await event.edit(
            "🔑 **Userbot Login**\n\n"
            "Please send your Telegram phone number with country code (e.g. `+919876543210`):",
            buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
        )
        
    elif data == "confirm_logout":
        await event.edit(
            "⚠️ **Confirm Logout**\n\n"
            "Are you sure you want to log out and disconnect the userbot account?",
            buttons=[
                [Button.inline("✅ Yes, Logout", data="logout")],
                [Button.inline("❌ Cancel", data="go_main_menu")]
            ]
        )
        await event.answer()
        
    elif data == "logout":
        await event.answer("Logging out...")
        if user_client.is_connected():
            try:
                await user_client.log_out()
                await user_client.disconnect()
            except Exception as e:
                logger.error(f"Error during logout: {e}")
        text = await get_status_text()
        await event.respond(
            "❌ Logged out successfully and session deleted.",
            buttons=[[Button.inline("🏡 Main Menu", data="go_main_menu")]]
        )
        await event.edit(text, buttons=await get_control_keyboard())

# Controller Bot Text Message Collector
@bot_client.on(events.NewMessage)
async def message_collector(event):
    if event.sender_id != config.get("owner_id"):
        return
        
    text = event.text.strip()
    
    # Check if we are in login flow
    if event.sender_id in login_state:
        state = login_state[event.sender_id]
        
        if state["step"] == "phone":
            phone = re.sub(r"[^\d+]", "", text)
            if not phone.startswith("+"):
                await event.respond(
                    "⚠️ Please include the country code (e.g. `+919876543210`).",
                    buttons=[
                        [Button.inline("🔄 Retry Login", data="auth_flow")],
                        [Button.inline("❌ Cancel", data="cancel_flow")]
                    ]
                )
                login_state.pop(event.sender_id, None)
                return
                
            msg = await event.respond(f"Sending OTP to `{phone}`...")
            try:
                if not user_client.is_connected():
                    await user_client.connect()
                
                sent_code = await user_client.send_code_request(phone)
                state["phone"] = phone
                state["phone_code_hash"] = sent_code.phone_code_hash
                state["step"] = "otp"
                
                await msg.delete()
                await event.respond(
                    "OTP sent. Please enter the OTP code (you can write it in `1 2 3 4 5` format or normally):",
                    buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                )
            except Exception as e:
                logger.error(f"Error sending code: {e}")
                await event.respond(
                    f"❌ Error sending OTP: {str(e)}",
                    buttons=[
                        [Button.inline("🔄 Retry Login", data="auth_flow")],
                        [Button.inline("🏡 Main Menu", data="cancel_flow")]
                    ]
                )
                login_state.pop(event.sender_id, None)
                
        elif state["step"] == "otp":
            otp = re.sub(r"\D", "", text)
            if not otp:
                await event.respond(
                    "⚠️ Invalid code format. Enter digits only:",
                    buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                )
                return
                
            msg = await event.respond("Verifying OTP...")
            try:
                await user_client.sign_in(
                    phone=state["phone"],
                    code=otp,
                    phone_code_hash=state["phone_code_hash"]
                )
                login_state.pop(event.sender_id, None)
                await msg.delete()
                await event.respond(
                    "✅ Userbot logged in successfully!",
                    buttons=[[Button.inline("🏡 Main Menu", data="go_main_menu")]]
                )
                
            except PhoneCodeInvalidError:
                await msg.delete()
                await event.respond(
                    "❌ Invalid OTP. Please enter the OTP code again:",
                    buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                )
            except SessionPasswordNeededError:
                state["step"] = "2fa"
                await msg.delete()
                await event.respond(
                    "🔑 2-Step Verification is enabled on your account. Please enter your Password:",
                    buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                )
            except Exception as e:
                logger.error(f"Error signing in: {e}")
                await msg.delete()
                await event.respond(
                    f"❌ Sign-in failed: {str(e)}",
                    buttons=[
                        [Button.inline("🔄 Retry Login", data="auth_flow")],
                        [Button.inline("🏡 Main Menu", data="cancel_flow")]
                    ]
                )
                login_state.pop(event.sender_id, None)
                
        elif state["step"] == "2fa":
            msg = await event.respond("Verifying 2-Step password...")
            try:
                await user_client.sign_in(password=text)
                login_state.pop(event.sender_id, None)
                await msg.delete()
                await event.respond(
                    "✅ Userbot logged in successfully!",
                    buttons=[[Button.inline("🏡 Main Menu", data="go_main_menu")]]
                )
            except PasswordHashInvalidError:
                await msg.delete()
                await event.respond(
                    "❌ Incorrect Password. Please try again:",
                    buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                )
            except Exception as e:
                logger.error(f"Error signing in 2FA: {e}")
                await msg.delete()
                await event.respond(
                    f"❌ Authentication failed: {str(e)}",
                    buttons=[
                        [Button.inline("🔄 Retry Login", data="auth_flow")],
                        [Button.inline("🏡 Main Menu", data="cancel_flow")]
                    ]
                )
                login_state.pop(event.sender_id, None)
        return

    # Check if we are expecting settings/groups inputs
    if event.sender_id in user_state:
        state = user_state[event.sender_id]
        expecting = state["expecting"]
        
        if expecting == "group_link":
            group_id = parse_group_identifier(text)
            user_state.pop(event.sender_id, None)
            
            title = "Group Added By Link"
            username = None
            if user_client.is_connected() and await user_client.is_user_authorized():
                try:
                    entity = await user_client.get_entity(group_id)
                    title = getattr(entity, "title", "Group Chat")
                    username = getattr(entity, "username", None)
                    group_id_str = str(entity.id)
                except Exception as e:
                    logger.warning(f"Could not get entity for group: {e}")
                    group_id_str = str(group_id)
            else:
                group_id_str = str(group_id)
                
            groups = config.setdefault("groups", {})
            groups[group_id_str] = {
                "name": title,
                "username": username,
                "enabled": True
            }
            save_config()
            
            await event.respond(
                f"✅ Group set successfully!\n\n"
                f"• Name: **{title}**\n"
                f"• ID/User: `{group_id_str}`\n\n"
                f"_Ensure the userbot is a member of this group to function._",
                buttons=[
                    [Button.inline("👥 Manage Groups", data="go_groups_menu")],
                    [Button.inline("🏡 Main Menu", data="go_main_menu")]
                ]
            )
            
        elif expecting == "chat_interval":
            try:
                sec = int(text)
                if sec < 60:
                    await event.respond(
                        "⚠️ Minimum interval is 60 seconds to avoid spam.",
                        buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                    )
                    return
                config["greeting_interval"] = sec
                save_config()
                user_state.pop(event.sender_id, None)
                await event.respond(
                    f"✅ Chatbot/Greeting interval set to `{sec}` seconds.",
                    buttons=[
                        [Button.inline("⚙️ Settings Menu", data="go_settings_menu")],
                        [Button.inline("🏡 Main Menu", data="go_main_menu")]
                    ]
                )
            except ValueError:
                await event.respond(
                    "⚠️ Please enter a valid number of seconds (digits only):",
                    buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                )
                
        elif expecting == "tagger_interval":
            try:
                sec = int(text)
                if sec < 60:
                    await event.respond(
                        "⚠️ Minimum interval is 60 seconds to avoid spam.",
                        buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                    )
                    return
                config["tagger_interval"] = sec
                save_config()
                user_state.pop(event.sender_id, None)
                await event.respond(
                    f"✅ Periodic tagger interval set to `{sec}` seconds.",
                    buttons=[
                        [Button.inline("⚙️ Settings Menu", data="go_settings_menu")],
                        [Button.inline("🏡 Main Menu", data="go_main_menu")]
                    ]
                )
            except ValueError:
                await event.respond(
                    "⚠️ Please enter a valid number of seconds (digits only):",
                    buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                )
                
        elif expecting == "tagger_batch_size":
            try:
                size = int(text)
                if size < 1 or size > 15:
                    await event.respond(
                        "⚠️ Batch size must be between 1 and 15.",
                        buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                    )
                    return
                config["tagger_batch_size"] = size
                save_config()
                user_state.pop(event.sender_id, None)
                await event.respond(
                    f"✅ Periodic tagger batch size set to `{size}` members.",
                    buttons=[
                        [Button.inline("⚙️ Settings Menu", data="go_settings_menu")],
                        [Button.inline("🏡 Main Menu", data="go_main_menu")]
                    ]
                )
            except ValueError:
                await event.respond(
                    "⚠️ Please enter a valid number (digits only):",
                    buttons=[[Button.inline("❌ Cancel", data="cancel_flow")]]
                )
        return

TAGGER_TEMPLATES = [
    "Hello {mentions}, kya chal raha hai? Aao active karo group ko! 😂",
    "Oi {mentions}, kahan gaayab ho sab? Chalo online aao baatein karte hai 😭",
    "Bhai log {mentions}, active ho jao jaldi se! Tumhare bina group sunsaan pada hai ❤️",
    "Kya chal raha hai {mentions}? Aao active karo thoda! 😂",
    "{mentions} kahan ho yaar sab? Dil se bura lagta hai jab koi active nahi rehta 🥺"
]

last_response_time = {}

# Auto-chat response using Gemini
async def get_gemini_reply(user_msg_text, sender_name):
    if not gemini_model:
        return "Haan bhai sahi baat hai."
        
    prompt = (
        f"Group member {sender_name} said: '{user_msg_text}'\n"
        f"Give a short natural Hinglish response with Indian boy feelings/sweet bro vibes."
    )
    
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: gemini_model.generate_content(prompt)
        )
        reply = response.text.strip()
        reply = re.sub(r'^["\']|["\']$', '', reply).strip()
        return reply
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return "Haan wahi toh 😂"

async def get_gemini_tagger_message(members_str):
    if not gemini_model:
        return None
    prompt = (
        f"We want to tag/mention a few group members to make the group active.\n"
        f"The members to tag are: {members_str}\n"
        f"Write a very short, natural Hinglish message (1-2 sentences) tagging these members, "
        f"asking them to come online and chat in the group. "
        f"Speak like a close Indian friend/brother (young boy feelings/bro vibe, using words like 'bhai', 'yaar', 'kahan ho', 'active ho jao'). "
        f"Keep it casual and emotional. "
        f"Make sure to include the exact tags/mentions in your message naturally."
    )
    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: gemini_model.generate_content(prompt)
        )
        reply = response.text.strip()
        reply = re.sub(r'^["\']|["\']$', '', reply).strip()
        return reply
    except Exception as e:
        logger.error(f"Gemini Tagger Prompt Error: {e}")
        return None

# Userbot Message Handler for monitoring group messages
@user_client.on(events.NewMessage)
async def userbot_message_handler(event):
    if not event.is_group:
        return

    try:
        chat = await event.get_chat()
        chat_id_str = str(chat.id)
        
        is_enabled = False
        if config.get("all_groups_enabled", False):
            is_enabled = True
            # Dynamically record group details
            record_group(chat_id_str, chat)
        else:
            groups = config.get("groups", {})
            if chat_id_str in groups:
                is_enabled = groups[chat_id_str].get("enabled", False)
            elif chat.username and chat.username.lower() in [ginfo.get("username", "").lower() for ginfo in groups.values() if ginfo.get("username")]:
                for gid, ginfo in groups.items():
                    if ginfo.get("username") and chat.username.lower() == ginfo["username"].lower():
                        is_enabled = ginfo.get("enabled", False)
                        break
                        
        if not is_enabled:
            return
            
    except Exception as e:
        logger.error(f"Error checking group match: {e}")
        return

    sender = await event.get_sender()
    if not sender or sender.bot:
        return
        
    me = await user_client.get_me()
    if sender.id == me.id:
        return

    # Track active members database
    record_member(chat_id_str, chat, sender)

    # Chatbot reply trigger
    if not config.get("auto_chat_enabled", False):
        return
        
    is_reply_to_me = False
    is_mentioning_me = event.mentioned
    
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        if reply_msg and reply_msg.sender_id == me.id:
            is_reply_to_me = True

    if is_mentioning_me or is_reply_to_me:
        # Group-level rate limiting (cooldown of 15 seconds) to avoid spam/cascade replies
        now = time.time()
        last_time = last_response_time.get(chat.id, 0)
        if now - last_time < 15:
            logger.info(f"Rate limit: Cooldown active in group {chat.id}. Skipping reply.")
            return
        last_response_time[chat.id] = now
        
        text = event.text.lower()
        await asyncio.sleep(random.uniform(3.0, 6.0))
        
        is_accused = any(word in text for word in BOT_ACCUSATION_WORDS)
        if is_accused:
            reply_text = random.choice(BOT_DENIALS)
        else:
            sender_name = sender.first_name or "bhai"
            reply_text = await get_gemini_reply(event.text, sender_name)
            
        try:
            async with user_client.action(chat.id, 'typing'):
                await asyncio.sleep(random.uniform(2.0, 4.0))
                await event.reply(reply_text)
                logger.info(f"Replied to {sender_name} in group {chat.id}: '{reply_text}'")
        except Exception as e:
            from telethon.errors import FloodWaitError
            if isinstance(e, FloodWaitError):
                logger.warning(f"Flood limit reached! Must sleep for {e.seconds} seconds.")
                await asyncio.sleep(e.seconds + 5)
            else:
                logger.error(f"Error replying to message: {e}")


# Periodic task: Rank Booster (greetings/mentions)
async def rank_booster_loop():
    while True:
        try:
            interval = config.get("greeting_interval", 300)
            sleep_duration = interval + random.randint(-int(interval*0.2), int(interval*0.2))
            await asyncio.sleep(max(30, sleep_duration))
            
            if not config.get("auto_chat_enabled", False):
                continue
                
            if not user_client.is_connected() or not await user_client.is_user_authorized():
                continue
                
            # Get list of active enabled groups
            target_groups = [gid for gid, ginfo in config.get("groups", {}).items() if ginfo.get("enabled", True)]
                
            if not target_groups:
                continue
                
            for chat_id_str in target_groups:
                await asyncio.sleep(random.uniform(5.0, 10.0))
                
                members = get_group_members(chat_id_str)
                if not members:
                    continue
                    
                user_id, member_info = random.choice(list(members.items()))
                username = member_info.get("username")
                first_name = member_info.get("first_name") or "bhai"
                
                mention = f"@{username}" if username else f"[{first_name}](tg://user?id={user_id})"
                
                greeting_template = random.choice(RANDOM_GREETINGS)
                if "{}" in greeting_template:
                    greeting_text = greeting_template.format(mention)
                else:
                    greeting_text = f"{mention} {greeting_template}"
                    
                try:
                    entity = await user_client.get_entity(chat_id_str if chat_id_str.startswith("-") or chat_id_str.isdigit() else chat_id_str)
                    
                    async with user_client.action(entity, 'typing'):
                        await asyncio.sleep(random.uniform(3.0, 7.0))
                        await user_client.send_message(entity, greeting_text)
                        logger.info(f"Boosted activity: Sent greeting '{greeting_text}' to group {chat_id_str}.")
                except Exception as e:
                    from telethon.errors import FloodWaitError
                    if isinstance(e, FloodWaitError):
                        logger.warning(f"Flood limit in rank booster! Sleeping for {e.seconds}s")
                        await asyncio.sleep(e.seconds + 5)
                    else:
                        logger.error(f"Error in rank booster for group {chat_id_str}: {e}")
                        
        except Exception as e:
            logger.error(f"Error in rank booster loop: {e}")


# Periodic task: Periodic Member Tagger
async def tagger_loop():
    while True:
        try:
            interval = config.get("tagger_interval", 900)
            sleep_duration = interval + random.randint(-int(interval*0.2), int(interval*0.2))
            await asyncio.sleep(max(30, sleep_duration))
            
            if not config.get("tagger_enabled", True):
                continue
                
            if not user_client.is_connected() or not await user_client.is_user_authorized():
                continue
                
            target_groups = [gid for gid, ginfo in config.get("groups", {}).items() if ginfo.get("enabled", True)]
            
            if not target_groups:
                continue
                
            for chat_id_str in target_groups:
                # Spacer delay between group tags to avoid flood
                await asyncio.sleep(random.uniform(10.0, 20.0))
                
                members = get_group_members(chat_id_str)
                if not members:
                    continue
                    
                batch_size = config.get("tagger_batch_size", 5)
                member_items = list(members.items())
                random.shuffle(member_items)
                batch = member_items[:batch_size]
                
                if not batch:
                    continue
                    
                mentions = []
                for uid, info in batch:
                    uname = info.get("username")
                    fname = info.get("first_name") or "bhai"
                    mentions.append(f"@{uname}" if uname else f"[{fname}](tg://user?id={uid})")
                    
                mentions_str = ", ".join(mentions)
                
                tagger_text = None
                if gemini_model:
                    tagger_text = await get_gemini_tagger_message(mentions_str)
                    
                if not tagger_text:
                    template = random.choice(TAGGER_TEMPLATES)
                    tagger_text = template.format(mentions=mentions_str)
                    
                try:
                    entity = await user_client.get_entity(chat_id_str if chat_id_str.startswith("-") or chat_id_str.isdigit() else chat_id_str)
                    
                    async with user_client.action(entity, 'typing'):
                        await asyncio.sleep(random.uniform(4.0, 8.0))
                        # Use markdown parsing explicitly
                        await user_client.send_message(entity, tagger_text, parse_mode='md')
                        logger.info(f"Tagger sent message to group {chat_id_str}.")
                except Exception as e:
                    from telethon.errors import FloodWaitError
                    if isinstance(e, FloodWaitError):
                        logger.warning(f"Flood limit in tagger! Sleeping for {e.seconds}s")
                        await asyncio.sleep(e.seconds + 5)
                    else:
                        logger.error(f"Error in tagger for group {chat_id_str}: {e}")
                        
        except Exception as e:
            logger.error(f"Error in tagger loop: {e}")


# Main Runner function
async def main():
    logger.info("Starting Controller Bot Client...")
    await bot_client.connect()
    
    # Auto-connect userbot if session file exists
    logger.info("Initializing Userbot Client...")
    try:
        await user_client.connect()
        if await user_client.is_user_authorized():
            logger.info("Userbot Session verified and connected successfully!")
        else:
            logger.info("Userbot Session not logged in. Start via Bot controller.")
    except Exception as e:
        logger.error(f"Could not connect Userbot Client: {e}")

    # Start background loop tasks
    asyncio.create_task(rank_booster_loop())
    asyncio.create_task(tagger_loop())
    
    # Keep running
    logger.info("System is fully operational. Running until stopped.")
    await bot_client.run_until_disconnected()

if __name__ == "__main__":
    # Ensure event loop runs the main loop
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Shutting down bot...")

