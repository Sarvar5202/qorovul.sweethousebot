import asyncio
from datetime import datetime, timedelta, timezone
import html
import logging
import os
import re
import sys
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginUser,
    ReactionTypeEmoji,
    User as TgUser,
)
from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    desc,
    func,
    select,
    update,
)
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ------------------ CONFIGURATION & ENVIRONMENT ------------------ #

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "8607841082:AAG4XXxHtjuCE2NOhv2ke9nyp7z49rLqoJE")
BOT_USERNAME = os.getenv("BOT_USERNAME", "qorovul_sweethousebot")
ENV_ADMIN_ID = os.getenv("ADMIN_ID")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Automatically format connection scheme for asyncpg
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif DATABASE_URL.startswith("postgresql://") and not DATABASE_URL.startswith("postgresql+asyncpg://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    # Fallback to local SQLite async database for development/standalone run if PostgreSQL is not specified
    DATABASE_URL = "sqlite+aiosqlite:///bot.db"

# Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ------------------ DATABASE MODELS & SESSION ------------------ #

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    invites_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=func.now())


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    referred_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), default=func.now())


# Async Engine & SessionMaker
engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    """Initialize database tables on startup."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized successfully on %s", DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL)


# ------------------ BOT INITIALIZATION ------------------ #

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Advertising / Links regex
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
    "ahmoq", "tentak", "mol", "itvachcha", "harom", "chumo", "jallob", "iflos",
    "jalap", "jalab", "haromi", "xaromi", "sikay", "sikey", "skay", "skey",
    "sikaman", "sikish", "koting", "kot", "koti", "am", "oming", "omi",
    "qotoq", "qotoqbosh", "qotoqvoy", "dalbayob", "dalbaeb", "shilta", "chmo",
    "la'nati", "lanati", "onangni", "padariga", "oneni", "suka", "blyad",
    "blat", "gandon", "pidar", "pidaraz", "tvar"
}

# Leetspeak / Homoglyph mapping
HOMOGLYPHS = {
    "@": "a", "0": "o", "1": "i", "!": "i", "$": "s", "3": "e", "4": "a"
}

# In-memory caches for anti-flood and temporary states
_recent_welcomes: dict[tuple[int, int], float] = {}
_user_warnings: dict[tuple[int, int], int] = {}
_registered_admins: set[int] = set()

if ENV_ADMIN_ID and ENV_ADMIN_ID.isdigit():
    _registered_admins.add(int(ENV_ADMIN_ID))


# ------------------ HELPER FUNCTIONS ------------------ #

def normalize_text(text: str) -> str:
    """Normalize text by converting to lower case and replacing common homoglyphs."""
    text_lower = text.lower()
    for char, replacement in HOMOGLYPHS.items():
        text_lower = text_lower.replace(char, replacement)
    return text_lower


def contains_profanity(text: str) -> bool:
    """Detect profanity words in normalized text with token and substring matching."""
    if not text:
        return False
    normalized = normalize_text(text)
    tokens = set(re.findall(r"\b\w+\b", normalized, re.UNICODE))
    if tokens & PROFANITY_WORDS:
        return True
    for bad_word in PROFANITY_WORDS:
        if len(bad_word) >= 4 and bad_word in normalized:
            return True
        elif bad_word in tokens:
            return True
    return False


def format_user_mention(user: TgUser) -> str:
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


def get_main_keyboard() -> InlineKeyboardMarkup:
    """Generate main inline keyboard for private chat /start UI."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="➕ Guruhga qo'shish",
                    url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📊 Mening hisobim",
                    callback_data="my_stats",
                ),
                InlineKeyboardButton(
                    text="🏆 Top taklifchilar",
                    callback_data="top_referrals",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💬 Qo'llab-quvvatlash",
                    url="https://t.me/qa_test_community",
                )
            ],
        ]
    )


async def get_user_stats_text(user_id: int, user_name: str) -> str:
    """Fetch user stats from database and return formatted text card."""
    invites_count = 0
    try:
        async with async_session() as session:
            result = await session.execute(select(User).where(User.user_id == user_id))
            user_row = result.scalar_one_or_none()
            if user_row:
                invites_count = user_row.invites_count
    except Exception as exc:
        logger.error("Error querying user stats for %s: %s", user_id, exc)

    ref_link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
    safe_name = html.escape(user_name)
    return (
        "📊 <b>Sizning hisobingiz:</b>\n"
        "━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Foydalanuvchi:</b> {safe_name}\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"👥 <b>Taklif qilgan do‘stlaringiz:</b> <b>{invites_count}</b> ta\n\n"
        "🔗 <b>Sizning taklif havolangiz:</b>\n"
        f"<code>{ref_link}</code>\n"
        "━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Ushbu havolani do‘stlaringizga yuboring va ball to‘plang!</i>"
    )


async def get_top_referrals_text() -> str:
    """Fetch top 10 inviters from database and return formatted leaderboard."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(User).order_by(desc(User.invites_count), User.created_at.asc()).limit(10)
            )
            top_users = result.scalars().all()
    except Exception as exc:
        logger.error("Error querying top referrals: %s", exc)
        top_users = []

    if not top_users:
        return (
            "🏆 <b>TOP 10 Taklifchilar reytingi:</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            "Hozircha reytingda hech kim yo‘q.\n"
            "━━━━━━━━━━━━━━━━━\n"
            "⚖️ <i>Do‘stlaringizni taklif qiling va 1-o‘rinni egallang!</i>"
        )

    lines = ["🏆 <b>TOP 10 Taklifchilar reytingi:</b>", "━━━━━━━━━━━━━━━━━"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for idx, u in enumerate(top_users, start=1):
        prefix = medals.get(idx, f"<b>{idx}.</b>")
        safe_name = html.escape(u.full_name)
        lines.append(f"{prefix} {safe_name} — <b>{u.invites_count}</b> ta")
    lines.append("━━━━━━━━━━━━━━━━━")
    lines.append("⚖️ <i>Do‘stlaringizni taklif qiling va yetakchiga aylaning!</i>")
    return "\n".join(lines)


# ------------------ 2. SANOQCHI / REFERRAL SYSTEM (PRIVATE CHAT) ------------------ #

@dp.message(F.chat.type == ChatType.PRIVATE, CommandStart())
async def handle_start_private(message: Message, command: CommandObject, bot: Bot) -> None:
    """Handle /start command in private chats with deep-link referral counter & UI card."""
    if not message.from_user:
        return

    user_id = message.from_user.id
    full_name = message.from_user.full_name
    safe_user_name = html.escape(full_name)
    _registered_admins.add(user_id)

    referrer_arg = command.args.strip() if command.args else None
    referrer_to_notify = None
    new_invites_count = 0

    try:
        async with async_session() as session:
            async with session.begin():
                # 1. Check if incoming user exists in database
                result = await session.execute(select(User).where(User.user_id == user_id))
                existing_user = result.scalar_one_or_none()

                if existing_user is None:
                    # New user flow
                    referrer_id = None
                    if referrer_arg and referrer_arg.isdigit():
                        parsed_ref_id = int(referrer_arg)
                        if parsed_ref_id != user_id:
                            ref_res = await session.execute(
                                select(User).where(User.user_id == parsed_ref_id)
                            )
                            referrer_user = ref_res.scalar_one_or_none()
                            if referrer_user:
                                referrer_id = parsed_ref_id
                                referrer_user.invites_count += 1
                                new_invites_count = referrer_user.invites_count
                                referrer_to_notify = referrer_id

                    # Register new user in users
                    new_user = User(
                        user_id=user_id,
                        full_name=full_name,
                        invites_count=0,
                    )
                    session.add(new_user)

                    # Insert record into referrals
                    if referrer_id:
                        referral_record = Referral(
                            referrer_id=referrer_id,
                            referred_id=user_id,
                        )
                        session.add(referral_record)
                else:
                    # Existing user: keep full_name synchronized
                    if existing_user.full_name != full_name:
                        existing_user.full_name = full_name
    except Exception as exc:
        logger.error("Database transaction error during /start for user %s: %s", user_id, exc)

    # Send real-time Telegram notification to the referrer if valid referral was recorded
    if referrer_to_notify and new_invites_count > 0:
        referrer_notification = (
            "🎉 <b>Yangi a’zo sizning havolangiz orqali kirdi!</b>\n"
            "━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Qo‘shildi:</b> {safe_user_name}\n"
            f"📊 <b>Jami to‘plagan ballaringiz:</b> {new_invites_count} ta"
        )
        try:
            await bot.send_message(chat_id=referrer_to_notify, text=referrer_notification)
            logger.info("Sent referral notification to referrer %s", referrer_to_notify)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.debug("Could not notify referrer %s: %s", referrer_to_notify, exc)
        except Exception as exc:
            logger.warning("Error notifying referrer %s: %s", referrer_to_notify, exc)

    # Main /start UI Card
    start_text = (
        "🛡 <b>GURUH QOROVULI | XAVFSIZLIK TIZIMI</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        f"👋 Salom, <b>{safe_user_name}</b>!\n\n"
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

    try:
        await message.answer(start_text, reply_markup=get_main_keyboard())
    except TelegramBadRequest as exc:
        logger.warning("Failed to send /start UI card: %s", exc)


@dp.callback_query(F.data == "my_stats")
async def cb_my_stats(callback: CallbackQuery) -> None:
    """Callback query for user stats."""
    await callback.answer()
    if callback.from_user:
        text = await get_user_stats_text(
            user_id=callback.from_user.id,
            user_name=callback.from_user.full_name,
        )
        await callback.message.answer(text)


@dp.callback_query(F.data == "top_referrals")
async def cb_top_referrals(callback: CallbackQuery) -> None:
    """Callback query for top 10 referrals leaderboard."""
    await callback.answer()
    text = await get_top_referrals_text()
    await callback.message.answer(text)


@dp.message(Command("meniki"))
async def cmd_my_stats(message: Message) -> None:
    """Command /meniki to view personal referral stats."""
    if not message.from_user:
        return
    text = await get_user_stats_text(
        user_id=message.from_user.id,
        user_name=message.from_user.full_name,
    )
    await message.answer(text)


@dp.message(Command("top"))
async def cmd_top(message: Message) -> None:
    """Command /top to view top 10 inviters."""
    text = await get_top_referrals_text()
    await message.answer(text)


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


# ------------------ 3. GROUP GUARDIAN / PROFANITY FILTER ------------------ #

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def handle_group_message(message: Message, bot: Bot) -> None:
    """Handle group messages: profanity filtering, anti-ad protection, and auto-reactions."""
    is_user_admin = await is_admin(message, bot)
    text_content = f"{message.text or ''} {message.caption or ''}".strip()

    # --- 1. PROFANITY & TOXICITY FILTER ENGINE ---
    if not is_user_admin and text_content and contains_profanity(text_content):
        # 1. Delete the profane message immediately
        try:
            await message.delete()
            logger.info("Deleted profanity message in chat %s", message.chat.id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning("Failed to delete profanity message: %s", exc)

        if message.from_user:
            user = message.from_user
            user_name = html.escape(user.full_name)
            user_id = user.id

            # 2. Mute sender for 15 minutes via bot.restrict_chat_member
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
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                logger.warning("Failed to restrict user %s: %s", user_id, exc)

            # 3. Post alert message
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
                # 4. Auto-delete this alert after 30 seconds
                asyncio.create_task(delete_after_delay(alert_msg, 30))
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                logger.warning("Failed to send profanity alert: %s", exc)

        return

    # --- 2. ANTI-AD & LINK PROTECTION ---
    if not is_user_admin and is_ad_or_violating(message):
        try:
            await message.delete()
            logger.info("Deleted ad/link message in chat %s", message.chat.id)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
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
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
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
                except (TelegramBadRequest, TelegramForbiddenError) as exc:
                    logger.warning("Failed to send link warning: %s", exc)

        return

    # --- 3. AUTO-REACTION FOR VALID MESSAGES ---
    try:
        await message.react([ReactionTypeEmoji(emoji="❤️")])
    except Exception as exc:
        logger.debug("Auto-reaction skipped: %s", exc)


# ------------------ MEMBERSHIP SERVICE HANDLERS ------------------ #

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.new_chat_members)
async def handle_new_chat_members(message: Message) -> None:
    """Handle and clean Telegram join service messages."""
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.left_chat_member)
async def handle_left_chat_member(message: Message) -> None:
    """Silently delete member left service notifications."""
    try:
        await message.delete()
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


# ------------------ BOT ENTRYPOINT ------------------ #

async def main() -> None:
    """Main application runner."""
    logger.info("Initializing database...")
    await init_db()

    logger.info("Starting Telegram Group Guardian & Referral Counter Bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(
        bot,
        allowed_updates=["message", "edited_message", "callback_query", "chat_member", "my_chat_member"],
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
