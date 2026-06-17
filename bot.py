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

CONFIG_FILE = "config.json"

# Load or initialize config
if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
    except Exception:
        config = {}
else:
    config = {}

# Default configuration values
config.setdefault("owner_id", None)
config.setdefault("target_group", None)
config.setdefault("auto_chat_enabled", False)
config.setdefault("greeting_interval", 300)  # Default 5 minutes
config.setdefault("active_users", [])        # List of active user IDs or usernames

def save_config():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving config: {e}")

# Validate Telegram API credentials
if not API_ID or not API_HASH or not BOT_TOKEN:
    print("\n[!] Please fill in the API_ID, API_HASH, and BOT_TOKEN in the .env file first!\n")
    # We will still define the clients but raise warning.
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
            "You are a real human Telegram user chatting in a group.\n"
            "Your task is to chat in casual 'Hinglish' (Hindi written in Latin script/English alphabets).\n"
            "RULES:\n"
            "1. Keep your replies very short, simple, and direct (usually 1-2 sentences maximum, keep it natural).\n"
            "2. Do NOT use AI-like emojis (e.g., 🤖, ✨, 🚀, 📝). Use casual human emojis (e.g., 😂, 👍, 😭, 💀, ya normal texting).\n"
            "3. Do NOT sound like an AI assistant. Speak like a regular Indian friend/teen/young adult.\n"
            "4. Never write formal sentences. Use words like 'bhai', 'yaar', 'kya', 'haan', 'nahi', 'accha', 'sahi hai', 'ekdum'.\n"
            "5. If someone is asking something casual, respond casually. No big explanations."
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
def get_control_keyboard():
    status_emoji = "🟢 ON" if config["auto_chat_enabled"] else "🔴 OFF"
    userbot_auth = "Connected" if user_client.is_connected() and asyncio.run_coroutine_threadsafe(user_client.is_user_authorized(), asyncio.get_event_loop()).result() else "Disconnected"
    
    auth_emoji = "🔑" if userbot_auth == "Disconnected" else "✅ Connected"
    
    buttons = [
        [
            Button.inline(f"{auth_emoji} Login/Change ID", data="auth_flow"),
            Button.inline(f"⚡ Toggle Userbot: {status_emoji}", data="toggle_userbot")
        ],
        [
            Button.inline("⚙️ Set Target Group", data="set_group"),
            Button.inline("⏱️ Set Interval", data="set_interval")
        ],
        [
            Button.inline("🏓 Ping", data="ping"),
            Button.inline("🔄 Status/Refresh", data="refresh_status")
        ]
    ]
    if userbot_auth == "Connected":
        buttons.append([Button.inline("❌ Logout/Disconnect", data="logout")])
    return buttons

# Helper: Build Control Panel Text
async def get_status_text():
    target = config["target_group"] or "Not Set"
    status = "Active 🟢" if config["auto_chat_enabled"] else "Inactive 🔴"
    interval = config["greeting_interval"]
    
    try:
        user_auth = await user_client.is_user_authorized() if user_client.is_connected() else False
    except Exception:
        user_auth = False
        
    user_status = "LoggedIn ✅" if user_auth else "Not Logged In ❌"
    
    text = (
        "🤖 **Userbot Controller Bot** 🤖\n\n"
        f"👤 **Userbot Account Status:** {user_status}\n"
        f"⚡ **Auto-Chat Loop:** {status}\n"
        f"👥 **Target Group:** `{target}`\n"
        f"⏱️ **Greeting Interval:** `{interval}s`\n\n"
        "Use the buttons below to control the bot. Any credentials or group configs are saved dynamically."
    )
    return text

# Controller Bot Start Handler
@bot_client.on(events.NewMessage(pattern="/start"))
async def start_handler(event):
    # Register first user as owner if not set
    if config["owner_id"] is None:
        config["owner_id"] = event.sender_id
        save_config()
        await event.respond(f"👑 You have been set as the owner of this Controller Bot!")
    
    if event.sender_id != config["owner_id"]:
        return  # Ignore unauthorized users

    text = await get_status_text()
    await event.respond(text, buttons=get_control_keyboard())

# Controller Bot Inline Buttons Handler
@bot_client.on(events.CallbackQuery)
async def callback_handler(event):
    if event.sender_id != config["owner_id"]:
        await event.answer("You are not the owner!", alert=True)
        return

    data = event.data.decode("utf-8")
    
    if data == "refresh_status":
        text = await get_status_text()
        await event.edit(text, buttons=get_control_keyboard())
        await event.answer("Status Refreshed!")
        
    elif data == "toggle_userbot":
        config["auto_chat_enabled"] = not config["auto_chat_enabled"]
        save_config()
        text = await get_status_text()
        await event.edit(text, buttons=get_control_keyboard())
        await event.answer(f"Auto-Chat turned {'ON' if config['auto_chat_enabled'] else 'OFF'}")
        
    elif data == "set_group":
        user_state[event.sender_id] = {"expecting": "group_link"}
        await event.respond("Please send the target Group link (e.g. `https://t.me/groupusername` or `@groupusername`):")
        await event.answer()
        
    elif data == "set_interval":
        user_state[event.sender_id] = {"expecting": "interval"}
        await event.respond("Please send the interval (in seconds) for periodic greetings/mentions (minimum 60s):")
        await event.answer()
        
    elif data == "ping":
        start_time = time.time()
        # Bot ping
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
                
        await event.respond(f"🏓 **Pong!**\n\n• Controller Bot: `{bot_latency}ms`\n• Userbot ID: `{user_latency}`")
        
    elif data == "auth_flow":
        # Start userbot login flow
        await event.answer("Starting Login Process...")
        login_state[event.sender_id] = {"step": "phone"}
        await event.respond("Please send your Telegram phone number with country code (e.g. `+919876543210`):")
        
    elif data == "logout":
        if user_client.is_connected():
            await user_client.log_out()
            await user_client.disconnect()
        await event.respond("❌ Logged out successfully and session deleted.")
        text = await get_status_text()
        await event.edit(text, buttons=get_control_keyboard())
        await event.answer("Logged Out")

# Controller Bot Text Message Collector (for login flow / configs)
@bot_client.on(events.NewMessage)
async def message_collector(event):
    if event.sender_id != config["owner_id"]:
        return
        
    text = event.text.strip()
    
    # 1. Handle Login Flow
    if event.sender_id in login_state:
        state = login_state[event.sender_id]
        
        if state["step"] == "phone":
            # Sanitize phone number (remove spaces, dashes)
            phone = re.sub(r"[^\d+]", "", text)
            if not phone.startswith("+"):
                await event.respond("⚠️ Please include the country code (e.g. `+919876543210`). Try login again.")
                login_state.pop(event.sender_id, None)
                return
                
            await event.respond(f"Sending OTP to `{phone}`...")
            try:
                if not user_client.is_connected():
                    await user_client.connect()
                
                # Request code
                sent_code = await user_client.send_code_request(phone)
                state["phone"] = phone
                state["phone_code_hash"] = sent_code.phone_code_hash
                state["step"] = "otp"
                await event.respond("OTP sent. Please enter the OTP code (you can write it in `1 2 3 4 5` format or normally):")
            except Exception as e:
                logger.error(f"Error sending code: {e}")
                await event.respond(f"❌ Error: {str(e)}\nTry /start again.")
                login_state.pop(event.sender_id, None)
                
        elif state["step"] == "otp":
            # Extract digits only to handle format "1 2 3 4 5"
            otp = re.sub(r"\D", "", text)
            if not otp:
                await event.respond("⚠️ Invalid code. Digits only. Please enter the OTP code again:")
                return
                
            await event.respond("Verifying OTP...")
            try:
                await user_client.sign_in(
                    phone=state["phone"],
                    code=otp,
                    phone_code_hash=state["phone_code_hash"]
                )
                await event.respond("✅ Userbot logged in successfully!")
                login_state.pop(event.sender_id, None)
                
                # Start Userbot client if authorized
                if await user_client.is_user_authorized():
                    await event.respond("⚡ Userbot account is active and listening to target group.")
                
            except PhoneCodeInvalidError:
                await event.respond("❌ Invalid OTP. Please try entering the OTP code again:")
            except SessionPasswordNeededError:
                state["step"] = "2fa"
                await event.respond("🔑 2-Step Verification is enabled on your account. Please enter your Password:")
            except Exception as e:
                logger.error(f"Error signing in: {e}")
                await event.respond(f"❌ Sign-in failed: {str(e)}\nTry /start again.")
                login_state.pop(event.sender_id, None)
                
        elif state["step"] == "2fa":
            await event.respond("Verifying 2-Step Verification password...")
            try:
                await user_client.sign_in(password=text)
                await event.respond("✅ Userbot logged in successfully!")
                login_state.pop(event.sender_id, None)
            except PasswordHashInvalidError:
                await event.respond("❌ Incorrect Password. Please try again:")
            except Exception as e:
                logger.error(f"Error signing in 2FA: {e}")
                await event.respond(f"❌ Failed: {str(e)}\nTry /start again.")
                login_state.pop(event.sender_id, None)
        return

    # 2. Handle Group Link and Interval Setup
    if event.sender_id in user_state:
        state = user_state[event.sender_id]
        if state["expecting"] == "group_link":
            group_id = parse_group_identifier(text)
            config["target_group"] = group_id
            save_config()
            user_state.pop(event.sender_id, None)
            
            # Check userbot membership
            if user_client.is_connected() and await user_client.is_user_authorized():
                try:
                    await user_client.get_entity(group_id)
                    await event.respond(f"✅ Target Group set to: `{group_id}`. Userbot can access this group.")
                except Exception as e:
                    await event.respond(
                        f"⚠️ Target Group set to `{group_id}`, but Userbot cannot access/find it. "
                        f"Ensure the userbot account has joined this group.\nError details: {e}"
                    )
            else:
                await event.respond(f"✅ Target Group set to: `{group_id}`. Please login userbot to verify access.")
                
        elif state["expecting"] == "interval":
            try:
                sec = int(text)
                if sec < 60:
                    await event.respond("⚠️ Minimum interval must be 60 seconds to avoid spam bans. Please enter again:")
                    return
                config["greeting_interval"] = sec
                save_config()
                user_state.pop(event.sender_id, None)
                await event.respond(f"✅ Greeting interval set to `{sec}` seconds.")
            except ValueError:
                await event.respond("⚠️ Please enter a valid number of seconds:")
        return

# ----------------- Userbot Handlers -----------------

# Auto-chat response using Gemini
async def get_gemini_reply(user_msg_text, sender_name):
    if not gemini_model:
        return "Haan bhai sahi baat hai."
        
    prompt = (
        f"Group member {sender_name} said: '{user_msg_text}'\n"
        f"Give a short natural Hinglish response."
    )
    
    try:
        # Run in executor to avoid blocking the async event loop
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: gemini_model.generate_content(prompt)
        )
        reply = response.text.strip()
        # Clean response from quotes or extra markings
        reply = re.sub(r'^["\']|["\']$', '', reply).strip()
        return reply
    except Exception as e:
        logger.error(f"Gemini API Error: {e}")
        return "Haan wahi toh 😂"

# Userbot Message Handler for monitoring the target group
@user_client.on(events.NewMessage)
async def userbot_message_handler(event):
    # Check if userbot is enabled and we have authorized userbot
    if not config["auto_chat_enabled"]:
        return
        
    if not config["target_group"]:
        return
        
    if not event.is_group:
        return

    # Check if the message is from our target group
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        target = config["target_group"]
        
        # Verify match with group link, username, or group ID
        is_target = False
        if str(chat_id) in target or (chat.username and chat.username.lower() == target.lower()):
            is_target = True
        else:
            # Also check if it's the target entity
            try:
                target_entity = await user_client.get_entity(target)
                if target_entity.id == chat_id:
                    is_target = True
            except Exception:
                pass
                
        if not is_target:
            return
    except Exception as e:
        logger.error(f"Error checking group match: {e}")
        return

    sender = await event.get_sender()
    if not sender:
        return
        
    sender_id = sender.id
    me = await user_client.get_me()
    
    # Ignore messages sent by the userbot itself
    if sender_id == me.id:
        return

    # Keep track of active users for rank/greeting booster
    username_or_id = f"@{sender.username}" if getattr(sender, 'username', None) else sender.first_name
    if username_or_id and username_or_id not in config["active_users"]:
        config["active_users"].append(username_or_id)
        # Keep list size reasonable (last 50 active users)
        if len(config["active_users"]) > 50:
            config["active_users"].pop(0)
        save_config()

    # Determine if userbot should respond
    is_reply_to_me = False
    is_mentioning_me = event.mentioned
    
    # Check if reply to my message
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        if reply_msg and reply_msg.sender_id == me.id:
            is_reply_to_me = True

    # If tagged/mentioned OR replied to:
    if is_mentioning_me or is_reply_to_me:
        text = event.text.lower()
        
        # Human typing delay simulation (Extended delay to avoid bot patterns)
        await asyncio.sleep(random.uniform(4.0, 8.0))
        
        # Check for bot accusations
        is_accused = any(word in text for word in BOT_ACCUSATION_WORDS)
        
        if is_accused:
            # Select random denial
            reply_text = random.choice(BOT_DENIALS)
        else:
            # Fetch response from Gemini
            sender_name = sender.first_name or "bhai"
            reply_text = await get_gemini_reply(event.text, sender_name)
            
        try:
            async with user_client.action(chat_id, 'typing'):
                await asyncio.sleep(random.uniform(2.0, 5.0))
                await event.reply(reply_text)
                logger.info(f"Replied to {sender_name}: '{reply_text}'")
        except Exception as e:
            from telethon.errors import FloodWaitError
            if isinstance(e, FloodWaitError):
                logger.warning(f"Message reply failed due to flood wait. Must sleep for {e.seconds} seconds.")
                await asyncio.sleep(e.seconds + 5)
            else:
                logger.error(f"Error replying to message: {e}")


# Periodic task: Rank Booster (greetings/mentions)
async def rank_booster_loop():
    while True:
        try:
            # Load current interval
            interval = config.get("greeting_interval", 300)
            # Add some randomness to interval (+/- 20%) to avoid bot detection pattern
            sleep_duration = interval + random.randint(-int(interval*0.2), int(interval*0.2))
            await asyncio.sleep(max(30, sleep_duration))
            
            # Check conditions
            if not config["auto_chat_enabled"] or not config["target_group"]:
                continue
                
            if not user_client.is_connected() or not await user_client.is_user_authorized():
                continue
                
            active_list = config.get("active_users", [])
            if not active_list:
                # No active users tracked yet, fetch from group chat participants
                try:
                    target_entity = await user_client.get_entity(config["target_group"])
                    participants = await user_client.get_participants(target_entity, limit=20)
                    me = await user_client.get_me()
                    active_list = []
                    for p in participants:
                        if p.id != me.id and not p.bot:
                            username_or_id = f"@{p.username}" if p.username else p.first_name
                            active_list.append(username_or_id)
                    if active_list:
                        config["active_users"] = active_list
                        save_config()
                except Exception as e:
                    logger.error(f"Error fetching group participants: {e}")
                    continue
            
            if not active_list:
                continue
                
            # Pick a random user to greet
            target_user = random.choice(active_list)
            greeting_template = random.choice(RANDOM_GREETINGS)
            
            # Format greeting (either with name/username or general)
            if "{}" in greeting_template:
                greeting_text = greeting_template.format(target_user)
            else:
                greeting_text = greeting_template
                
            # Send message to target group
            target_entity = await user_client.get_entity(config["target_group"])
            
            # Simulate typing with human-like delay
            async with user_client.action(target_entity, 'typing'):
                await asyncio.sleep(random.uniform(3.0, 7.0))
                try:
                    await user_client.send_message(target_entity, greeting_text)
                    logger.info(f"Boosted activity: Sent greeting '{greeting_text}' to group.")
                except Exception as e:
                    # Catch Telethon FloodWaitError specifically
                    from telethon.errors import FloodWaitError
                    if isinstance(e, FloodWaitError):
                        logger.warning(f"Flood limit reached! Must sleep for {e.seconds} seconds.")
                        await asyncio.sleep(e.seconds + 5)
                    else:
                        raise e
                
        except Exception as e:
            logger.error(f"Error in rank booster loop: {e}")

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

    # Start rank booster background task
    asyncio.create_task(rank_booster_loop())
    
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

