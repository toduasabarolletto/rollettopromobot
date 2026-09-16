"""
Telegram Promo Bot
==================
Requirements: python-telegram-bot>=20.0, aiosqlite, python-dotenv, gspread, google-auth
"""

import logging
import asyncio
import aiosqlite
import os
import json
import base64
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.error import Forbidden, BadRequest

# Load environment variables from .env file (local development)
load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable not set")

ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
MIN_AGE_MINUTES: int = 10
MAX_AGE_HOURS: int = 24
DB_PATH: str = os.getenv("DB_PATH", "promo_bot.db")
# Per-user cooldown: react to a given user's "bonus" at most once per this many seconds (default 5 min).
BONUS_COOLDOWN_SECONDS: int = int(os.getenv("BONUS_COOLDOWN_SECONDS", "900"))

# ---------------------------------------------------------------------------
# Google Sheets setup
# ---------------------------------------------------------------------------
SHEET_ID = os.getenv("SHEET_ID", "1EZSMD7bDmarRimhpdmfaGzlFPOdXtXzaOlzMiVJppoQ")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")

def get_sheet():
    """Connect to Google Sheets and return the worksheet."""
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        raw = GOOGLE_CREDENTIALS_JSON.strip()
        if not raw:
            logger.error("GOOGLE_CREDENTIALS_JSON is empty — paste the service account JSON (or its base64) into the env var")
            return None
        # Accept both raw JSON and base64-encoded JSON (base64 avoids paste/newline issues in env vars)
        try:
            creds_dict = json.loads(raw)
        except json.JSONDecodeError:
            try:
                creds_dict = json.loads(base64.b64decode(raw).decode("utf-8"))
            except Exception:
                logger.error("GOOGLE_CREDENTIALS_JSON is not valid JSON and not valid base64-encoded JSON — re-paste it (see notes)")
                return None
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        return sheet
    except Exception as e:
        logger.error("Failed to connect to Google Sheets: %s", e)
        return None

def upsert_sheet_row(
    telegram_id: int,
    rolletto_username: str,
    language: str,
    claimed: str,
    date_time: str,
    tg_username: str,
    first_name: str,
    last_name: str,
):
    try:
        sheet = get_sheet()
        if not sheet:
            return

        telegram_id_str = str(telegram_id)
        existing_row = None
        try:
            col_e_values = sheet.col_values(5)
            if telegram_id_str in col_e_values:
                existing_row = col_e_values.index(telegram_id_str) + 1
        except Exception:
            existing_row = None

        if existing_row:
            sheet.update_cell(existing_row, 1, rolletto_username)
            sheet.update_cell(existing_row, 2, language)
            sheet.update_cell(existing_row, 3, claimed)
            sheet.update_cell(existing_row, 4, date_time)
            sheet.update_cell(existing_row, 6, tg_username)
            sheet.update_cell(existing_row, 7, first_name)
            sheet.update_cell(existing_row, 8, last_name)
            logger.info("Updated existing row %d for telegram_id: %s", existing_row, telegram_id_str)
        else:
            all_values = sheet.get_all_values()
            last_data_row = len([r for r in all_values if any(cell.strip() for cell in r)])
            next_row = last_data_row + 1
            sheet.update_cell(next_row, 1, rolletto_username)
            sheet.update_cell(next_row, 2, language)
            sheet.update_cell(next_row, 3, claimed)
            sheet.update_cell(next_row, 4, date_time)
            sheet.update_cell(next_row, 5, telegram_id_str)
            sheet.update_cell(next_row, 6, tg_username)
            sheet.update_cell(next_row, 7, first_name)
            sheet.update_cell(next_row, 8, last_name)
            logger.info("Added new row at row %d for telegram_id: %s", next_row, telegram_id_str)

    except Exception as e:
        logger.error("Failed to upsert Google Sheets row: %s", e)

def restore_claimed_from_sheet() -> list[int]:
    """
    Read Google Sheets and return a list of telegram_ids where claimed = 'Yes'.
    Called once at startup to rebuild the DB after a Railway redeploy wipes it.
    """
    try:
        sheet = get_sheet()
        if not sheet:
            return []
        all_rows = sheet.get_all_values()
        claimed_ids = []
        for row in all_rows[1:]:  # skip header row if any
            try:
                claimed_col = row[2].strip().lower() if len(row) > 2 else ""
                telegram_id_col = row[4].strip() if len(row) > 4 else ""
                if claimed_col == "yes" and telegram_id_col.isdigit():
                    claimed_ids.append(int(telegram_id_col))
            except Exception:
                continue
        logger.info("Restored %d claimed users from Google Sheets", len(claimed_ids))
        return claimed_ids
    except Exception as e:
        logger.error("Failed to restore claimed from Google Sheets: %s", e)
        return []


def update_claimed_in_sheet(telegram_id: int):
    """Update claimed status in Google Sheet by Telegram ID (column E)."""
    try:
        sheet = get_sheet()
        if sheet:
            telegram_id_str = str(telegram_id)
            col_e_values = sheet.col_values(5)
            if telegram_id_str in col_e_values:
                row = col_e_values.index(telegram_id_str) + 1
                sheet.update_cell(row, 3, "Yes")
                logger.info("Updated claimed status for telegram_id: %s", telegram_id_str)
    except Exception as e:
        logger.error("Failed to update claimed in Google Sheets: %s", e)

# ---------------------------------------------------------------------------
# Channel IDs per language
# ---------------------------------------------------------------------------
CHANNEL_IDS = {
    "en": -1002326259934,
    "it": -1003220500138,
    "fr": -1003471986771,
    "mx": -1002326259934,  # Spanish users join the English channel
}

# ---------------------------------------------------------------------------
# Discussion Group IDs per language
# ---------------------------------------------------------------------------
DISCUSSION_GROUP_IDS = {
    "en": -1002464292560,
    "it": -1003255978169,
    "fr": -1003434078194,
    "mx": -1002464292560,  # Spanish users write bonus in the English discussion group
}

# ---------------------------------------------------------------------------
# Promo codes per language
# ---------------------------------------------------------------------------
PROMO_CODES = {
    "en": "JVUTJF",
    "it": "ADJSDU",
    "fr": "OADIDMA",
    "mx": "SDAVES",  # Spanish users receive the English promo code
}

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------
ASKING_USERNAME = 1

# ---------------------------------------------------------------------------
# Language content
# ---------------------------------------------------------------------------

LANG_SELECT_TEXT = (
    "🇬🇧 Hello!\n"
    "🇮🇹 Ciao!\n"
    "🇫🇷 Bonjour!\n"
    "🇪🇸 ¡Hola!\n\n"
    "Please choose your language / Scegli la lingua / Choisissez la langue / Elige tu idioma:"
)

ASK_USERNAME_MESSAGES = {
    "en": "Please paste your Rolletto username separately in one text message.",
    "it": "Per favore incolla il tuo nome utente Rolletto separatamente in un unico messaggio di testo.",
    "fr": "Veuillez coller votre nom d'utilisateur Rolletto séparément dans un seul message.",
    "mx": "Por favor pega tu nombre de usuario de Rolletto por separado en un solo mensaje de texto.",
}

WELCOME_MESSAGES = {
    "en": (
        "👋 Welcome, {name}!\n\n"
        "Thanks for being a member of our channel 🤝\n"
        "Follow Rolletto on our platforms and stay updated with the latest promotions, news, and rewards\n\n"
        "<b>Then go to the comments on any post and send the word \"bonus\" to claim your promo code.</b>\n\n"
        "<b>✨Join our channel first:</b>\n\n"
        "👉 <a href='https://t.me/+9jS0DgDO_KI5YzYy'>Click here to join the channel</a>"
    ),
    "it": (
        "👋 Benvenuto, {name}!\n\n"
        "Grazie per essere un membro del nostro canale 🤝\n"
        "Segui Rolletto sulle nostre piattaforme e rimani aggiornato con le ultime promozioni, notizie e premi\n\n"
        "<b>Poi vai nei commenti di qualsiasi post e invia la parola \"bonus\" per ricevere il tuo codice promo.</b>\n\n"
        "<b>✨Unisciti prima al nostro canale:</b>\n\n"
        "👉 <a href='https://t.me/+Fkw3DMpmZE1hYzYy'>Clicca qui per unirti al canale</a>"
    ),
    "fr": (
        "👋 Bienvenue, {name}!\n\n"
        "Merci d'être membre de notre chaîne 🤝\n"
        "Suivez Rolletto sur nos plateformes et restez informé des dernières promotions, actualités et récompenses\n\n"
        "<b>Ensuite, allez dans les commentaires de n'importe quel post et envoyez le mot \"bonus\" pour recevoir votre code promo.</b>\n\n"
        "<b>✨Rejoignez d'abord notre chaîne:</b>\n\n"
        "👉 <a href='https://t.me/+-8aE5nJOGSsxZjdi'>Cliquez ici pour rejoindre la chaîne</a>"
    ),
    "mx": (
        "👋 ¡Bienvenido, {name}!\n\n"
        "Gracias por ser miembro de nuestro canal 🤝\n"
        "Sigue a Rolletto en nuestras plataformas y mantente al día con las últimas promociones, noticias y recompensas\n\n"
        "<b>Luego ve los comentarios de cualquier publicación y envía la palabra \"bonus\" para reclamar tu código promo.</b>\n\n"
        "<b>✨Únete primero a nuestro canal:</b>\n\n"
        "👉 <a href='https://t.me/+9jS0DgDO_KI5YzYy'>Haz clic aquí para unirte al canal</a>"
    ),
}

BONUS_MESSAGES = {
    "already_claimed": {
        "en": "You have already claimed this promotion.",
        "it": "Hai già riscattato questa promozione.",
        "fr": "Vous avez déjà réclamé cette promotion.",
        "mx": "Ya has reclamado esta promoción.",
    },
    "not_subscribed": {
        "en": "You must join the channel first to receive the bonus.",
        "it": "Devi prima unirti al canale per ricevere il bonus.",
        "fr": "Vous devez d'abord rejoindre la chaîne pour recevoir le bonus.",
        "mx": "Debes unirte al canal primero para recibir el bono.",
    },
    "too_old": {
        "en": "This promotion is only available for new members (within 24 hours).",
        "it": "Questa promozione è disponibile solo per i nuovi membri (entro 24 ore).",
        "fr": "Cette promotion est uniquement disponible pour les nouveaux membres (dans les 24 heures).",
        "mx": "Esta promoción solo está disponible para nuevos miembros (dentro de las 24 horas).",
    },
    "too_new": {
        "en": "You should be a member for at least 10 minutes – try again in about {mins} minute(s).",
        "it": "Devi essere membro da almeno 10 minuti – riprova tra circa {mins} minuto/i.",
        "fr": "Vous devez être membre depuis au moins 10 minutes – réessayez dans environ {mins} minute(s).",
        "mx": "Debes ser miembro por al menos 10 minutos – inténtalo de nuevo en {mins} minuto(s).",
    },
    "no_dm": {
        "en": "⚠️ I could not send you a DM. Please start a private conversation with me first (@rollettopromobot), then type 'bonus' here again.",
        "it": "⚠️ Non riesco a inviarti un messaggio privato. Avvia prima una conversazione privata con me (@rollettopromobot), poi scrivi 'bonus' qui.",
        "fr": "⚠️ Je ne peux pas vous envoyer de message privé. Commencez d'abord une conversation privée avec moi (@rollettopromobot), puis tapez 'bonus' ici.",
        "mx": "⚠️ No pude enviarte un mensaje privado. Primero inicia una conversación privada conmigo (@rollettopromobot), luego escribe 'bonus' aquí.",
    },
    "sent": {
        "en": "✅ Promo code sent to your direct messages.",
        "it": "✅ Codice promo inviato ai tuoi messaggi diretti.",
        "fr": "✅ Code promo envoyé dans vos messages privés.",
        "mx": "✅ Código promo enviado a tus mensajes directos.",
    },
    "promo_dm": {
        "en": "🎉 Congratulations! Here is your exclusive promo code:\n\n<code>{code}</code>\n\n<b>Requirements: Your email and ID must be verified in order to claim this bonus.</b>\n\n<b>Additionally, if you have not completed your email and ID verification yet, you will receive free spins once your verification is completed.</b>\n\nUse it before it expires. Enjoy!",
        "it": "🎉 Congratulazioni! Ecco il tuo codice promo esclusivo:\n\n<code>{code}</code>\n\n<b>Requisiti: La tua email e il tuo documento d'identità devono essere verificati per poter richiedere questo bonus.</b>\n\n<b>Inoltre, se non hai ancora completato la verifica dell'email e del documento d'identità, riceverai i giri gratuiti una volta completata la verifica.</b>\n\nUsalo prima che scada. Buon divertimento!",
        "fr": "🎉 Félicitations! Voici votre code promo exclusif:\n\n<code>{code}</code>\n\n<b>Conditions: Votre adresse e-mail et votre pièce d'identité doivent être vérifiées pour pouvoir bénéficier de ce bonus.</b>\n\n<b>De plus, si vous n'avez pas encore vérifié votre e-mail et votre pièce d'identité, vous recevrez des tours gratuits une fois la vérification effectuée.</b>\n\nUtilisez-le avant qu'il n'expire. Profitez-en!",
        "mx": "🎉 ¡Felicidades! Aquí está tu código promo exclusivo:\n\n<code>{code}</code>\n\n<b>Requisitos: Tu correo electrónico y tu identificación deben estar verificados para poder reclamar este bono.</b>\n\n<b>Además, si aún no has verificado tu correo y tu identificación, recibirás giros gratis una vez que se complete la verificación.</b>\n\n¡Úsalo antes de que expire. ¡Disfrútalo!",
    },

}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id           INTEGER PRIMARY KEY,
                first_seen        REAL NOT NULL,
                claimed           INTEGER NOT NULL DEFAULT 0,
                language          TEXT NOT NULL DEFAULT 'en',
                rolletto_username TEXT,
                tg_username       TEXT,
                first_name        TEXT,
                last_name         TEXT
            )
            """
        )
        for col in [
            ("rolletto_username", "TEXT"),
            ("tg_username", "TEXT"),
            ("first_name", "TEXT"),
            ("last_name", "TEXT"),
        ]:
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col[0]} {col[1]}")
                logger.info("Migration: added %s column", col[0])
            except Exception:
                pass
        await db.commit()
    logger.info("Database initialised at %s", DB_PATH)


async def get_user(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT user_id, first_seen, claimed, language,
                      rolletto_username, tg_username, first_name, last_name
               FROM users WHERE user_id = ?""",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None


async def upsert_user(user_id: int, tg_username: str = None, first_name: str = None, last_name: str = None) -> dict:
    now = datetime.now(timezone.utc).timestamp()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, first_seen, claimed, language) VALUES (?, ?, 0, 'en')",
            (user_id, now),
        )
        if tg_username is not None or first_name is not None or last_name is not None:
            await db.execute(
                """UPDATE users SET
                    tg_username = COALESCE(?, tg_username),
                    first_name  = COALESCE(?, first_name),
                    last_name   = COALESCE(?, last_name)
                   WHERE user_id = ?""",
                (tg_username, first_name, last_name, user_id),
            )
        await db.commit()
    return await get_user(user_id)


async def set_user_language(user_id: int, language: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET language = ? WHERE user_id = ?",
            (language, user_id),
        )
        await db.commit()


async def save_rolletto_username(user_id: int, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET rolletto_username = ? WHERE user_id = ?",
            (username, user_id),
        )
        await db.commit()


async def try_claim(user_id: int) -> bool:
    """
    Atomically set claimed=1 only if it is currently 0.
    Returns True if this call successfully claimed (won the race).
    Returns False if already claimed by a previous request.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE users SET claimed = 1 WHERE user_id = ? AND claimed = 0",
            (user_id,),
        )
        await db.commit()
        return cursor.rowcount == 1


async def unclaim(user_id: int) -> None:
    """Roll back a claim — used when the DM send fails after claiming."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET claimed = 0 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()


async def get_user_language(user_id: int) -> str:
    user = await get_user(user_id)
    if user:
        return user.get("language", "en")
    return "en"


# ---------------------------------------------------------------------------
# Subscription check
# ---------------------------------------------------------------------------

async def is_subscribed_to_channel(bot, user_id: int, lang: str) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_IDS[lang], user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except (Forbidden, BadRequest) as exc:
        logger.warning("Could not check channel membership for %s: %s", user_id, exc)
        return False


# ---------------------------------------------------------------------------
# /start command – show language selection buttons
# ---------------------------------------------------------------------------

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    await upsert_user(
        user.id,
        tg_username=f"@{user.username}" if user.username else "",
        first_name=user.first_name or "",
        last_name=user.last_name or "",
    )

    keyboard = [
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
            InlineKeyboardButton("🇮🇹 Italiano", callback_data="lang_it"),
        ],
        [
            InlineKeyboardButton("🇫🇷 Français", callback_data="lang_fr"),
            InlineKeyboardButton("🇪🇸 Español", callback_data="lang_mx"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.effective_message.reply_text(
        LANG_SELECT_TEXT,
        reply_markup=reply_markup,
    )
    return ASKING_USERNAME


# ---------------------------------------------------------------------------
# Callback handler – language button pressed, then ask for username
# ---------------------------------------------------------------------------

async def handle_language_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    lang = query.data.replace("lang_", "")

    await upsert_user(
        user.id,
        tg_username=f"@{user.username}" if user.username else "",
        first_name=user.first_name or "",
        last_name=user.last_name or "",
    )
    await set_user_language(user.id, lang)

    context.user_data["lang"] = lang

    await query.edit_message_text(
        text=ASK_USERNAME_MESSAGES[lang],
        parse_mode="HTML",
    )

    logger.info("User %s chose language: %s", user.id, lang)
    return ASKING_USERNAME


# ---------------------------------------------------------------------------
# Handle username input from user
# ---------------------------------------------------------------------------

async def handle_username_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    new_username = update.message.text.strip()

    lang = context.user_data.get("lang") or await get_user_language(user.id)

    existing_record = await get_user(user.id)

    await save_rolletto_username(user.id, new_username)

    tg_username = f"@{user.username}" if user.username else ""
    first_name = user.first_name or ""
    last_name = user.last_name or ""

    await upsert_user(user.id, tg_username=tg_username, first_name=first_name, last_name=last_name)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    claimed_str = "Yes" if (existing_record and existing_record.get("claimed")) else "No"
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, upsert_sheet_row,
        user.id, new_username, lang, claimed_str, now_str,
        tg_username, first_name, last_name
    )

    logger.info("Saved username '%s' for telegram_id: %s (@%s)", new_username, user.id, user.username)

    welcome_text = WELCOME_MESSAGES[lang].format(name=user.first_name)
    await update.message.reply_text(
        text=welcome_text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Core handler – "bonus" keyword in any discussion group
# ---------------------------------------------------------------------------

# Remembers the last time we reacted to each user's "bonus" (in-memory; cleared on restart).
_last_bonus_response: dict[int, datetime] = {}

async def handle_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if chat.id not in DISCUSSION_GROUP_IDS.values():
        return

    # Per-user cooldown: if we reacted to this user's "bonus" less than
    # BONUS_COOLDOWN_SECONDS ago, ignore this one silently (no reply).
    now_ts = datetime.now(timezone.utc)
    last = _last_bonus_response.get(user.id)
    if last is not None and (now_ts - last).total_seconds() < BONUS_COOLDOWN_SECONDS:
        logger.debug("'bonus' from user %s ignored (cooldown active)", user.id)
        return
    _last_bonus_response[user.id] = now_ts

    logger.info("'bonus' received from user %s (%s)", user.id, user.full_name)

    lang = next((l for l, gid in DISCUSSION_GROUP_IDS.items() if gid == chat.id), "en")

    await upsert_user(
        user.id,
        tg_username=f"@{user.username}" if user.username else "",
        first_name=user.first_name or "",
        last_name=user.last_name or "",
    )
    await set_user_language(user.id, lang)

    if not await is_subscribed_to_channel(context.bot, user.id, lang):
        await message.reply_text(BONUS_MESSAGES["not_subscribed"][lang])
        return

    user_record = await get_user(user.id)

    if user_record and user_record["claimed"]:
        await message.reply_text(BONUS_MESSAGES["already_claimed"][lang])
        return

    now = datetime.now(timezone.utc)
    first_seen = datetime.fromtimestamp(user_record["first_seen"], tz=timezone.utc)
    age: timedelta = now - first_seen

    if age > timedelta(hours=MAX_AGE_HOURS):
        await message.reply_text(BONUS_MESSAGES["too_old"][lang])
        return

    if age < timedelta(minutes=MIN_AGE_MINUTES):
        remaining = timedelta(minutes=MIN_AGE_MINUTES) - age
        mins_left = int(remaining.total_seconds() // 60) + 1
        await message.reply_text(
            BONUS_MESSAGES["too_new"][lang].format(mins=mins_left)
        )
        return

    claimed = await try_claim(user.id)
    if not claimed:
        logger.info("User %s already claimed (lost atomic race)", user.id)
        await message.reply_text(BONUS_MESSAGES["already_claimed"][lang])
        return

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=BONUS_MESSAGES["promo_dm"][lang].format(code=PROMO_CODES[lang]),
            parse_mode="HTML",
        )
    except Forbidden:
        await unclaim(user.id)
        logger.warning("DM failed for user %s — claim rolled back", user.id)
        await message.reply_text(BONUS_MESSAGES["no_dm"][lang])
        return

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, update_claimed_in_sheet, user.id)

    logger.info("Promo code delivered to user %s in language %s", user.id, lang)
    await message.reply_text(BONUS_MESSAGES["sent"][lang])


# ---------------------------------------------------------------------------
# Stats handler – only accessible by admin
# ---------------------------------------------------------------------------

async def handle_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total_users = (await cursor.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM users WHERE claimed = 1") as cursor:
            total_claimed = (await cursor.fetchone())[0]
        async with db.execute("SELECT language, COUNT(*) FROM users WHERE claimed = 1 GROUP BY language") as cursor:
            by_language = await cursor.fetchall()

    lang_text = "\n".join([f"  {row[0]}: {row[1]}" for row in by_language])

    await update.effective_message.reply_text(
        f"📊 <b>Bot Stats</b>\n\n"
        f"👤 Total users: <b>{total_users}</b>\n"
        f"🎁 Total claimed: <b>{total_claimed}</b>\n\n"
        f"📌 Claims by language:\n{lang_text}",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    await init_db()

    # Restore claimed statuses from Google Sheets in case the DB was wiped by a redeploy.
    # This runs once at startup and marks everyone who already claimed as claimed=1 in the DB.
    loop = asyncio.get_event_loop()
    claimed_ids = await loop.run_in_executor(None, restore_claimed_from_sheet)
    if claimed_ids:
        async with aiosqlite.connect(DB_PATH) as db:
            for uid in claimed_ids:
                # Insert a minimal row if user not yet in DB, then mark claimed
                await db.execute(
                    "INSERT OR IGNORE INTO users (user_id, first_seen, claimed, language) VALUES (?, ?, 1, 'en')",
                    (uid, datetime.now(timezone.utc).timestamp()),
                )
                await db.execute(
                    "UPDATE users SET claimed = 1 WHERE user_id = ?",
                    (uid,),
                )
            await db.commit()
        logger.info("Startup restore complete: %d users marked as claimed", len(claimed_ids))

    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", handle_start)],
        states={
            ASKING_USERNAME: [
                CallbackQueryHandler(handle_language_choice, pattern="^lang_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username_input),
            ],
        },
        fallbacks=[CommandHandler("start", handle_start)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("stats", handle_stats))
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex(r"(?i)\bbonus\b"),
            handle_bonus,
        )
    )
    app.add_error_handler(error_handler)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    async with app:
        await app.start()
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
