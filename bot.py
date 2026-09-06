import asyncio
from datetime import datetime, timedelta, timezone
import html
import json
import logging
import os
import re
import sys
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import (
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginUser,
    ReactionTypeEmoji,
    User,
)

# Load environment variables (.env)
def load_dotenv(filepath: str = ".env") -> None:
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8607841082:AAG4XXxHtjuCE2NOhv2ke9nyp7z49rLqoJE")
BOT_USERNAME = os.getenv("BOT_USERNAME", "qorovul_sweethousebot")
ENV_ADMIN_ID = os.getenv("ADMIN_ID")

USERS_FILE = "registered_admins.json"

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Initialize Bot & Dispatcher
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Regex pattern for advertising/links
AD_PATTERN = re.compile(
    r"(?i)(?:https?://|www\.|t\.me/|telegram\.me/|@[a-zA-Z0-9_]+)"
)

BLOCKED_ENTITY_TYPES = {
    MessageEntityType.URL,
    MessageEntityType.TEXT_LINK,
    MessageEntityType.MENTION,
}

# Configurable profanity dictionary
PROFANITY_WORDS = {
    "jalap", "jalab", "haromi", "xaromi", "sikay", "sikey", "skay", "skey",
    "sikaman", "sikish", "koting", "kot", "koti", "am", "oming", "omi",
    "qotoq", "qotoqbosh", "qotoqvoy", "dalbayob", "dalbaeb", "itvachcha",
    "shilta", "chmo", "la'nati", "lanati", "onangni", "padariga", "oneni",
    "suka", "blyad", "blat", "gandon", "pidar", "pidaraz", "tvar"
}

# Leetspeak / Homoglyph mapping for normalization
HOMOGLYPHS = {
    "@": "a", "0": "o", "1": "i", "!": "i", "$": "s", "3": "e", "4": "a"
}

# In-memory caches
_recent_welcomes: dict[tuple[int, int], float] = {}
_user_warnings: dict[tuple[int, int], int] = {}
_registered_admins: set[int] = set()

if ENV_ADMIN_ID and ENV_ADMIN_ID.isdigit():
    _registered_admins.add(int(ENV_ADMIN_ID))

def load_registered_admins() -> None:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    _registered_admins.update(data)
        except Exception as exc:
            logger.warning("Could not read %s: %s", USERS_FILE, exc)

def save_registered_admin(user_id: int) -> None:
    _registered_admins.add(user_id)
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(_registered_admins), f)
    except Exception as exc:
        logger.warning("Could not save %s: %s", USERS_FILE, exc)

load_registered_admins()


def normalize_text(text: str) -> str:
    """Normalize text by converting to lower case and replacing common homoglyphs."""
    text_lower = text.lower()
    for char, replacement in HOMOGLYPHS.items():
        text_lower = text_lower.replace(char, replacement)
    return text_lower


def contains_profanity(text: str) -> bool:
    """Detect profanity words in normalized text."""
    if not text:
        return False
    normalized = normalize_text(text)
    tokens = set(re.findall(r"\b\w+\b", normalized))
    if tokens & PROFANITY_WORDS:
        return True
    for bad_word in PROFANITY_WORDS:
        if len(bad_word) >= 3 and bad_word in normalized:
            return True
    return False


def format_user_mention(user: User) -> str:
    """Format user with Telegram profile link and optional username."""
    safe_name = html.escape(user.full_name)
    mention_link = f'<a href="tg://user?id={user.id}">{safe_name}</a>'
    if user.username:
        return f"{mention_link} (@{user.username})"
    return mention_link


async def is_admin(message: Message, bot_instance: Bot) -> bool:
    """Check if the sender is an administrator or creator."""
    if message.from_user is None:
        if message.sender_chat and message.sender_chat.id == message.chat.id:
            return True
        return False

    try:
        member = await bot_instance.get_chat_member(
            chat_id=message.chat.id,
            user_id=message.from_user.id,
        )
        return member.status in {
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.ADMINISTRATOR,
        }
    except TelegramBadRequest:
        return False
    except Exception as exc:
        logger.warning("Admin check failed: %s", exc)
        return False


def is_ad_or_violating(message: Message) -> bool:
    """Check if message has links, ads, mentions, or forwards from channels/bots."""
    if message.forward_origin is not None:
        if isinstance(message.forward_origin, (MessageOriginChannel, MessageOriginChat)):
            return True
        if (
            isinstance(message.forward_origin, MessageOriginUser)
            and message.forward_origin.sender_user.is_bot
        ):
            return True

    entities = (message.entities or []) + (message.caption_entities or [])
    for entity in entities:
        if entity.type in BLOCKED_ENTITY_TYPES:
            return True

    content = f"{message.text or ''}\n{message.caption or ''}"
    if AD_PATTERN.search(content):
        return True

    return False


async def delete_after_delay(msg: Message, delay: int = 30) -> None:
    """Asynchronously delete a message after a given delay in seconds."""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except TelegramBadRequest:
        pass
    except Exception as exc:
        logger.debug("Failed to auto-delete temporary message: %s", exc)


async def send_new_member_card_to_private(chat_id: int, chat_title: str, user: User, bot_instance: Bot) -> None:
    """Send structured join notification card directly to the bot's private chat (admin chat)."""
    if user.is_bot:
        return

    now = time.time()
    for key, ts in list(_recent_welcomes.items()):
        if now - ts > 60:
            _recent_welcomes.pop(key, None)

    cache_key = (chat_id, user.id)
    if cache_key in _recent_welcomes and (now - _recent_welcomes[cache_key]) < 15:
        return
    _recent_welcomes[cache_key] = now

    safe_name = html.escape(user.full_name)
    username_str = f"@{user.username}" if user.username else "Mavjud emas"
    safe_title = html.escape(chat_title or "Guruh")
    current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    join_card_text = (
        "🔔 <b>Guruhga yangi a'zo qo'shildi!</b>\n\n"
        f"👥 <b>Guruh:</b> {safe_title}\n"
        f"👤 <b>Foydalanuvchi:</b> <a href=\"tg://user?id={user.id}\">{safe_name}</a>\n"
        f"🔗 <b>Username:</b> {username_str}\n"
        f"🆔 <b>ID:</b> <code>{user.id}</code>\n"
        f"📅 <b>Vaqt:</b> {current_time_str}\n"
        "🛡 <b>Status:</b> Kanal obunasi va Bot tekshiruviga yuborildi."
    )

    # Gather target recipient IDs (group creator/admins and registered private users)
    recipients = set(_registered_admins)

    try:
        admins = await bot_instance.get_chat_administrators(chat_id=chat_id)
        for admin in admins:
            if not admin.user.is_bot:
                recipients.add(admin.user.id)
    except Exception as exc:
        logger.debug("Could not get chat administrators for chat %s: %s", chat_id, exc)

    # Send join notification card to bot's private chats
    for admin_id in recipients:
        try:
            await bot_instance.send_message(chat_id=admin_id, text=join_card_text)
            logger.info("Sent join log to private chat of user %s", admin_id)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.debug("Could not send private notification to %s (user hasn't started bot in private): %s", admin_id, exc)
        except Exception as exc:
            logger.warning("Error sending private notification: %s", exc)


# ------------------ /start COMMAND HANDLER ------------------ #

@dp.message(F.chat.type == ChatType.PRIVATE, CommandStart())
async def handle_start_private(message: Message) -> None:
    """Handle /start command in private chats with modern UX and action buttons."""
    if message.from_user:
        save_registered_admin(message.from_user.id)

    user_name = html.escape(message.from_user.full_name) if message.from_user else "Foydalanuvchi"
    
    start_text = (
        "🛡 <b>GURUH QOROVULI | XAVFSIZLIK TIZIMI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👋 Salom, <b>{user_name}</b>!\n\n"
        "Men guruhingizni spam, arabcha botlar, reklama havolalari va behayo so‘zlardan "
        "<b>24/7</b> avtomatik tozalab turuvchi qorovulman.\n\n"
        "⚡️ <b>Imkoniyatlarim:</b>\n"
        "├ 🚫 <b>Anti-Link:</b> Har qanday havola va reklamani o‘chirish\n"
        "├ 🧹 <b>Anti-Flood:</b> Ketma-ket spam yozuvchilarni jazolash\n"
        "├ 🤖 <b>Anti-Bot:</b> Begona botlar kiritilishini bloklash\n"
        "└ 🔇 <b>Smart Mute/Ban:</b> So‘kinganlarni darhol cheklash\n\n"
        "⚙️ <b>Qanday ishlatiladi?</b>\n"
        "1. Pastdagi tugma orqali meni guruhingizga qo‘shing.\n"
        "2. Botga <b>«Administrator»</b> huquqini bering.\n"
        "3. Qolganini o‘zimga qo‘yib bering!\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚖️ <i>Guruhda tartib va xotirjamlikni birga ta’minlaymiz.</i>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Guruhga qo'shish",
                    url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💬 Qo'llab-quvvatlash",
                    url="https://t.me/qa_test_community",
                )
            ],
        ]
    )

    try:
        await message.answer(start_text, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        logger.warning("Failed to send /start reply: %s", exc)


# ------------------ ADMIN MODERATION COMMANDS ------------------ #

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), Command("unmute"))
async def handle_unmute_command(message: Message, bot: Bot) -> None:
    """Admin command to unrestrict / unmute a user."""
    if not await is_admin(message, bot):
        return

    target_user = None
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user

    if not target_user:
        try:
            await message.reply("Foydalanuvchi xabariga javob (reply) qilib <code>/unmute</code> deb yozing.")
        except TelegramBadRequest:
            pass
        return

    full_permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )

    try:
        await bot.restrict_chat_member(
            chat_id=message.chat.id,
            user_id=target_user.id,
            permissions=full_permissions,
        )
        _user_warnings[(message.chat.id, target_user.id)] = 0
        safe_name = html.escape(target_user.full_name)
        await message.reply(
            f"✅ <a href=\"tg://user?id={target_user.id}\">{safe_name}</a> dan cheklov (mute) olib tashlandi!"
        )
    except TelegramBadRequest as exc:
        await message.reply(f"Xatolik: {exc}")


# ------------------ MEMBERSHIP JOIN / LEAVE HANDLERS ------------------ #

@dp.chat_member(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    ChatMemberUpdatedFilter(JOIN_TRANSITION),
)
async def handle_chat_member_joined(event: ChatMemberUpdated, bot: Bot) -> None:
    """Listen for chat member join events."""
    chat_title = event.chat.title or "Guruh"
    await send_new_member_card_to_private(
        chat_id=event.chat.id,
        chat_title=chat_title,
        user=event.new_chat_member.user,
        bot_instance=bot,
    )


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.new_chat_members)
async def handle_new_chat_members(message: Message, bot: Bot) -> None:
    """Handle Telegram service join messages."""
    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    if not message.new_chat_members:
        return

    chat_title = message.chat.title or "Guruh"
    for user in message.new_chat_members:
        await send_new_member_card_to_private(
            chat_id=message.chat.id,
            chat_title=chat_title,
            user=user,
            bot_instance=bot,
        )


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.left_chat_member)
async def handle_left_chat_member(message: Message) -> None:
    """Silently delete member left service notifications."""
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


# ------------------ GROUP MODERATION & PROFANITY ENGINE ------------------ #

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def handle_group_message(message: Message, bot: Bot) -> None:
    """Handle group messages: profanity filtering, anti-ad protection, and auto-reactions."""
    is_user_admin = await is_admin(message, bot)
    text_content = f"{message.text or ''} {message.caption or ''}".strip()

    # --- 1. PROFANITY & TOXICITY FILTER ENGINE ---
    if not is_user_admin and text_content and contains_profanity(text_content):
        try:
            await message.delete()
            logger.info("Deleted profanity message in chat %s", message.chat.id)
        except TelegramBadRequest as exc:
            logger.warning("Failed to delete profanity message: %s", exc)

        if message.from_user:
            user = message.from_user
            user_name = html.escape(user.full_name)
            user_id = user.id

            mute_until = datetime.now(timezone.utc) + timedelta(minutes=15)
            restricted_permissions = ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            )

            try:
                await bot.restrict_chat_member(
                    chat_id=message.chat.id,
                    user_id=user_id,
                    permissions=restricted_permissions,
                    until_date=mute_until,
                )
                logger.info("Muted user %s for profanity in chat %s for 15 mins", user_id, message.chat.id)
            except TelegramBadRequest as exc:
                logger.warning("Failed to restrict user %s: %s", user_id, exc)

            alert_text = (
                "🔇 <b>Qoidabuzar jazolandi!</b>\n"
                "━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>Foydalanuvchi:</b> <a href=\"tg://user?id={user_id}\">{user_name}</a>\n"
                "⚠️ <b>Sabab:</b> Guruhda haqoratli so‘z ishlatish taqiqlangan!\n"
                "⏳ <b>Jazo muddati:</b> 15 daqiqa (Mute)\n"
                "━━━━━━━━━━━━━━━━━\n"
                "⚖️ <i>Iltimos, o‘zaro hurmatni saqlang!</i>"
            )

            try:
                alert_msg = await bot.send_message(chat_id=message.chat.id, text=alert_text)
                asyncio.create_task(delete_after_delay(alert_msg, 30))
            except TelegramBadRequest as exc:
                logger.warning("Failed to send profanity alert: %s", exc)

        return

    # --- 2. ANTI-AD & 3-STRIKE LINK PROTECTION ---
    if not is_user_admin and is_ad_or_violating(message):
        try:
            await message.delete()
            logger.info("Deleted ad/link message in chat %s", message.chat.id)
        except TelegramBadRequest as exc:
            logger.warning("Failed to delete ad message: %s", exc)

        if message.from_user:
            user = message.from_user
            warn_key = (message.chat.id, user.id)
            current_warns = _user_warnings.get(warn_key, 0) + 1
            user_mention = format_user_mention(user)

            if current_warns >= 3:
                _user_warnings[warn_key] = 0
                mute_until = datetime.now(timezone.utc) + timedelta(hours=24)
                restricted_permissions = ChatPermissions(can_send_messages=False)
                try:
                    await bot.restrict_chat_member(
                        chat_id=message.chat.id,
                        user_id=user.id,
                        permissions=restricted_permissions,
                        until_date=mute_until,
                    )
                    punish_text = (
                        f"🚫 {user_mention} <b>3 marta</b> qoidani buzgani sababli "
                        f"<b>24 soatga</b> guruhda yozish huquqidan mahrum qilindi (MUTE)!"
                    )
                    await bot.send_message(chat_id=message.chat.id, text=punish_text)
                except TelegramBadRequest as exc:
                    logger.warning("Failed to restrict user: %s", exc)
            else:
                _user_warnings[warn_key] = current_warns
                warn_text = (
                    f"⚠️ {user_mention}, guruhda reklama va havola yuborish taqiqlangan!\n"
                    f"Ogohlantirish: <b>{current_warns}/3</b>\n"
                    f"<i>(3-ogohlantirishdan so'ng 24 soatga yozish huquqidan mahrum qilinasiz)</i>"
                )
                try:
                    await bot.send_message(chat_id=message.chat.id, text=warn_text)
                except TelegramBadRequest as exc:
                    logger.warning("Failed to send link warning: %s", exc)

        return

    # --- 3. AUTO-REACTION FOR VALID MESSAGES ---
    try:
        await message.react([ReactionTypeEmoji(emoji="❤️")])
    except (TelegramBadRequest, Exception) as exc:
        logger.debug("Failed to react: %s", exc)


# ------------------ BOT ENTRYPOINT ------------------ #

async def main() -> None:
    """Main polling runner."""
    logger.info("Starting Telegram Group Guardian Bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(
        bot,
        allowed_updates=["message", "edited_message", "chat_member", "my_chat_member"],
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
