import datetime
import json
import logging
import logging.config
import os
import random
import re
from functools import wraps
from pathlib import Path
from random import choice
from typing import List, Callable, Optional

from telegram import Update, TelegramError, Chat, ParseMode, Bot, BotCommandScopeAllPrivateChats, BotCommand, User, \
    InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, BotCommandScopeAllChatAdministrators
from telegram.error import BadRequest
from telegram.ext import Updater, CallbackContext, Filters, MessageHandler, CallbackQueryHandler, MessageFilter, \
    CommandHandler

from emojis import Emojis, EmojiButton
from images import CaptchaImage
import utilities
from mwt import MWT
from config import config

emojis = Emojis(max_codepoints=1)
updater = Updater(
    config.telegram.token,
    workers=1,
    persistence=None,  # disable persistence for now
)


class StandardPermission:
    MUTED: ChatPermissions = ChatPermissions(can_send_messages=False)
    UNLOCK_ALL: ChatPermissions = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_pin_messages=True,
        can_change_info=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
        can_send_polls=True
    )


class NewGroup(MessageFilter):
    def filter(self, message):
        if message.new_chat_members:
            member: User
            for member in message.new_chat_members:
                if member.id == updater.bot.id:
                    return True


def load_logging_config(file_name='logging.json'):
    with open(file_name, 'r') as f:
        logging_config = json.load(f)

    logging.config.dictConfig(logging_config)


load_logging_config("logging.json")

logger = logging.getLogger(__name__)


@MWT(timeout=60 * 60)
def get_admin_ids(bot: Bot, chat_id: int):
    return [admin.user.id for admin in bot.get_chat_administrators(chat_id)]


def administrators(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id not in get_admin_ids(context.bot, update.effective_chat.id):
            logger.debug("admin check failed for callback <%s>", func.__name__)
            return

        return func(update, context, *args, **kwargs)

    return wrapped


def superadmin(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id not in config.telegram.admins:
            logger.debug("superadmin check failed for callback <%s>", func.__name__)
            return

        return func(update, context, *args, **kwargs)

    return wrapped


def users(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id in get_admin_ids(context.bot, update.effective_chat.id):
            logger.debug("user check failed")
            return

        return func(update, context, *args, **kwargs)

    return wrapped


def fail_with_message(answer_to_message=True):
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            try:
                return func(update, context, *args, **kwargs)
            except Exception as e:
                error_str = str(e)
                logger.error('error while running callback: %s', error_str, exc_info=True)
                if answer_to_message:
                    update.message.reply_html(
                        f"Error while executing callback <code>{func.__name__}</code>: <code>{error_str}</code>",
                        disable_web_page_preview=True
                    )

        return wrapped
    return real_decorator


def get_captcha():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            # logger.debug("%s", update.callback_query.data)
            target_user_id = int(context.match[2])
            if target_user_id != update.effective_user.id:
                update.callback_query.answer("Questo test è destinato ad un altro utente", show_alert=True, cache_time=60*60*24)
                return

            if update.effective_user.id not in context.chat_data or "captcha" not in context.chat_data[update.effective_user.id]:
                update.callback_query.answer("Questo test non è più valido")
                utilities.safe_delete(update.callback_query.message)

                context.chat_data.pop(update.effective_user.id, None)
                return

            captcha = context.chat_data[update.effective_user.id]["captcha"]

            result_captcha = func(update, context, captcha, *args, **kwargs)
            if result_captcha:
                result_captcha.updated_on = utilities.now_utc()
                context.chat_data[update.effective_user.id]["captcha"] = result_captcha

        return wrapped
    return real_decorator


def run_and_log(func: Callable, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except (TelegramError, BadRequest) as e:
        error_str = str(e)
        logger.error("error while executing function <%s>: %s", func.__name__, error_str)


class EmojiCaptcha:
    MIN_BUTTONS = 2
    MIN_CORRECT_EMOJIS = 1
    MAX_SINGLE_ROW_EMOJIS = 4

    def __init__(
            self,
            user: User,
            chat: Chat,
            service_message_id: int,
            correct_emojis_number: int,  # number of emojis in the image (that are marked as correct in the keyboard)
            correct_emojis_threshold: Optional[int] = None,  # minimum number of correct emojis to select to pass the captcha
            number_of_buttons: int = 8,  # number of keyboard buttons
            allowed_errors: int = 2  # number of errors the user is allowed to do
    ):
        if number_of_buttons < self.MIN_BUTTONS:
            raise ValueError(f"the captcha must have at least {self.MIN_BUTTONS} buttons")

        if correct_emojis_number < 1:
            raise ValueError(f"there must be at least {self.MIN_CORRECT_EMOJIS} correct emoji")

        if number_of_buttons > self.MAX_SINGLE_ROW_EMOJIS and number_of_buttons % 2 != 0:
            raise ValueError(f"the number of buttons must be even, or lower than {self.MAX_SINGLE_ROW_EMOJIS + 1}")

        if number_of_buttons <= correct_emojis_number:
            raise ValueError(f"the number of buttons ({number_of_buttons}) must be greater than the number of correct emojis ({correct_emojis_number})")

        if correct_emojis_threshold and correct_emojis_threshold > correct_emojis_number:
            raise ValueError(f"the number of correct emojis to pass the test ({correct_emojis_threshold}) cannot be greater than the number of emojis on the image ({correct_emojis_number})")

        self.user = user
        self.chat_id = chat.id
        self.message_id = None  # captcha message_id
        self.correct_emojis_number = correct_emojis_number
        self.correct_emojis_threshold = correct_emojis_threshold or self.correct_emojis_number
        self.number_of_buttons = number_of_buttons
        self.errors = 0
        self.allowed_errors = allowed_errors
        self.service_message_id = service_message_id

        now = utilities.now_utc()
        self.created_on = now
        self.updated_on = now

        self.emojis: List[EmojiButton] = []

        self.gen_emojis()

    def gen_emojis(self):
        random_emojis = emojis.random(count=self.number_of_buttons)

        self.emojis = [EmojiButton.convert(e) for e in random_emojis]

        for i in range(self.correct_emojis_number):
            # mark the first emojis as correct, we will shuffle the list later
            self.emojis[i].correct = True

        random.shuffle(self.emojis)

    def get_reply_markup(self, rows=2):
        if not self.emojis:
            self.gen_emojis()

        if self.number_of_buttons <= self.MAX_SINGLE_ROW_EMOJIS:
            rows = 1

        keyboard = []
        # max two lines of emojis
        buttons_per_row = self.number_of_buttons if self.number_of_buttons <= self.MAX_SINGLE_ROW_EMOJIS else int(self.number_of_buttons / rows)
        i = 0
        for row_number in range(rows):
            buttons_row = []
            for column_number in range(buttons_per_row):
                emoji = self.emojis[i]
                button = InlineKeyboardButton(emoji.unicode, callback_data=emoji.user_callback_data(self.user.id))

                buttons_row.append(button)
                i += 1

            keyboard.append(buttons_row)

        return InlineKeyboardMarkup(keyboard)

    def add_error(self, errors_to_add=1):
        self.errors += errors_to_add
        return self.errors

    def remaining_attempts(self):
        return self.allowed_errors - self.errors

    def get_correct_emojis(self):
        return [e for e in self.emojis if e.correct]

    def get_correct_and_selected_count(self):
        return len([e for e in self.emojis if e.correct and e.already_selected])

    def get_still_to_guess(self):
        return self.correct_emojis_threshold - self.correct_answers_count()

    def correct_answers_count(self):
        return sum(e.already_selected and e.correct for e in self.emojis)

    def get_emoji(self, emoji_id):
        for emoji in self.emojis:
            if emoji.id == emoji_id:
                return emoji

        raise ValueError(f"emoji_id (hex codepoints) not found: {emoji_id}")

    def mark_as_selected(self, emoji_id):
        for i, emoji in enumerate(self.emojis):
            if emoji.id == emoji_id:
                self.emojis[i].already_selected = True
                return

        raise ValueError(f"emoji_id (hex codepoints) not found: {emoji_id}")

    def __str__(self):
        emojis_list = [f"{type(e).__name__}(id={e.id})" for e in self.emojis]
        emojis_string = "\n\t".join(emojis_list)

        return f"{type(self).__name__}(\n\t{emojis_string}\n)"


@fail_with_message()
def on_forced_captcha_command(update: Update, context: CallbackContext):
    logger.debug("forced captcha for %d (%d)", update.effective_user.id, update.message.message_id)

    return on_new_member(update, context)


@fail_with_message(answer_to_message=True)
@superadmin
def on_unrestrict_command(update: Update, context: CallbackContext):
    logger.debug("!unrestrict from %d", update.effective_user.id)
    if not update.message.reply_to_message:
        return update.message.reply_html("Reply to a message")

    update.effective_chat.restrict_member(
        update.message.reply_to_message.from_user.id,
        permissions=StandardPermission.UNLOCK_ALL
    )

    deleted = utilities.safe_delete(update.message)
    if not deleted:
        update.message.reply_html("Unrestricted")


def get_chat_background_path(chat_id: int) -> Path:
    chat_id_str = str(chat_id).replace("-100", "")
    file_name = f"background_{chat_id_str}.jpg"

    return Path("backgrounds") / file_name


def get_background_path(chat_id: int, default_file_path: str) -> Path:
    image_path = get_chat_background_path(chat_id)
    if not image_path.exists():
        return Path(default_file_path)

    return image_path


@fail_with_message()
@administrators
def on_setphoto_command(update: Update, context: CallbackContext):
    logger.debug("/setphoto from %d -> %d", update.effective_user.id, update.effective_chat.id)

    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        return update.message.reply_html("Rispondi ad una foto con <code>/setphoto</code> per utilizzarla come sfondo")

    file_path = get_chat_background_path(update.effective_chat.id)
    photo_file = update.message.reply_to_message.photo[-1].get_file()
    photo_file.download(file_path)

    text = f"Questa foto verrà utilizzata come sfondo per il captcha"
    if config.captcha.image_max_side:
        text = f"{text} (verrà ridimensionata a <code>{config.captcha.image_max_side}px</code>)"

    update.message.reply_to_message.reply_html(text)


@fail_with_message()
def on_new_member(update: Update, context: CallbackContext):
    logger.debug("new member in %d: %d", update.effective_chat.id, update.effective_user.id)
    if update.message.new_chat_members and update.effective_user.id != update.message.new_chat_members[0].id:
        # allow people to add other people without captchas
        return

    if update.effective_user.id not in get_admin_ids(context.bot, update.effective_chat.id):
        # testing: do not restrict if the user is an admin
        update.effective_chat.restrict_member(update.effective_user.id, permissions=StandardPermission.MUTED)

    captcha = EmojiCaptcha(
        update.effective_user,
        update.effective_chat,
        service_message_id=update.message.message_id,
        correct_emojis_number=config.captcha.image_emojis,
        correct_emojis_threshold=config.captcha.image_emojis_correct_threshold,
        number_of_buttons=config.captcha.image_buttons,
        allowed_errors=config.captcha.allowed_errors
    )

    captcha_image = CaptchaImage(
        background_path=get_background_path(update.effective_chat.id, config.captcha.image_path),
        emojis_list=captcha.get_correct_emojis(),
        max_side=config.captcha.image_max_side,
        scale_factor=config.captcha.image_scale_factor
    )
    file_path = f"tmp/{update.effective_chat.id}_{update.message.message_id}.png"
    captcha_image.generate_capctha_image(file_path)

    caption_emojis_to_select = captcha.correct_emojis_threshold if captcha.correct_emojis_threshold > 1 else "una"
    caption = f"Ciao {utilities.mention_escaped(update.effective_user)}, benvenuto/a!" \
              f"\nPer poter parlare in questa chat, <b>devi dimostrare di non essere un bot!</b> " \
              f"Seleziona {caption_emojis_to_select} delle emoji che vedi nell'immagine utilizzando i " \
              f"tasti qui sotto." \
              f"\nTi sono concessi {captcha.remaining_attempts()} errori e {config.captcha.timeout} minuti di tempo"

    with open(file_path, "rb") as f:
        sent_message = update.message.reply_photo(
            f,
            caption=caption,
            reply_markup=captcha.get_reply_markup(),
            parse_mode=ParseMode.HTML,
            quote=False
        )

    captcha_image.delete_generated_image()

    captcha.message_id = sent_message.message_id

    context.chat_data[update.effective_user.id] = {"captcha": captcha}


@fail_with_message(answer_to_message=False)
@get_captcha()
def on_already_selected_button(update: Update, context: CallbackContext, captcha: EmojiCaptcha):
    update.callback_query.answer("Hai già selezionato questa emoji in precedenza", cache_time=60*60*24)


@fail_with_message(answer_to_message=False)
@get_captcha()
def on_button(update: Update, context: CallbackContext, captcha: EmojiCaptcha):
    emoji_id = context.match[1]
    logger.info("user selected emoji: %s", emoji_id)

    emoji = captcha.get_emoji(emoji_id)
    logger.debug("emoji: %s (correct: %s)", emoji.codepoints_hex, emoji.correct)

    captcha.mark_as_selected(emoji.id)

    new_caption = ""
    if emoji.correct:
        still_to_guess = captcha.get_still_to_guess()
        if still_to_guess != 0:
            if still_to_guess == 1:
                alert_text = f"Ottimo lavoro! Ne rimane ancora una"
            else:
                alert_text = f"Ottimo lavoro! Ne rimangono ancora {still_to_guess}"
            update.callback_query.answer(alert_text)
        else:
            logger.debug("captcha completed, cleaning up and lifting restrictions...")
            context.chat_data.pop(update.effective_user.id, None)

            utilities.safe_delete(update.callback_query.message)
            if config.captcha.delete_service_message:
                utilities.safe_delete_by_id(context.bot, update.effective_chat.id, captcha.service_message_id)

            run_and_log(
                context.bot.restrict_chat_member,
                update.effective_chat.id,
                update.effective_user.id,
                permissions=StandardPermission.UNLOCK_ALL
            )  # maybe the user has already been unrestricted
            return
    else:
        errors = captcha.add_error()
        if errors > captcha.allowed_errors:
            logger.debug("captcha failed, cleaning up...")
            context.chat_data.pop(update.effective_user.id, None)

            utilities.safe_delete(update.callback_query.message)
            if config.captcha.delete_service_message:
                utilities.safe_delete_by_id(context.bot, update.effective_chat.id, captcha.service_message_id)

            if config.captcha.send_message_on_fail:
                user_mention = utilities.mention_escaped(update.effective_user)
                context.bot.send_message(
                    update.effective_chat.id,
                    f"{user_mention} non è riuscito/a a verificarsi a causa dei troppi errori ({errors}), "
                    f"è ancora membro di questo gruppo ma non portà parlare [#mute #u{update.effective_user.id}]",
                    parse_mode=ParseMode.HTML
                )

            return
        elif captcha.remaining_attempts() == 0:
            update.callback_query.answer("\U000026a0\U0000fe0f Emoji errata! Non ti è più permesso fare errori!",
                                         show_alert=True)
        else:
            remaining_attempts = captcha.remaining_attempts()
            if remaining_attempts == 1:
                alert_text = f"\U000026a0\U0000fe0f Emoji errata! Ti è ancora concesso un solo errore"
            else:
                alert_text = f"Emoji errata! Ti sono ancora concessi {captcha.remaining_attempts()} errori"

            update.callback_query.answer(alert_text)

    reply_markup = captcha.get_reply_markup()
    update.callback_query.edit_message_reply_markup(reply_markup=reply_markup)

    return captcha


@fail_with_message()
def on_new_group_chat(update: Update, _):
    logger.info("new group chat: %s", update.effective_chat.title)

    if config.telegram.exit_unknown_groups and update.effective_user.id not in config.telegram.admins:
        logger.info("unauthorized: leaving...")
        update.effective_chat.leave()
        return


def cleanup_and_ban(context: CallbackContext):
    for chat_id, chat_data in context.dispatcher.chat_data.items():
        user_id_to_pop = []
        for user_id, user_data in chat_data.items():
            if "captcha" not in user_data:
                continue

            captcha: EmojiCaptcha = user_data["captcha"]

            now = utilities.now_utc()
            diff_seconds = (now - captcha.created_on).total_seconds()
            if diff_seconds <= config.captcha.timeout * 60:
                continue

            logger.info("cleaning up user %d data from chat %d: diff of %d seconds", user_id, chat_id, diff_seconds)

            utilities.safe_delete_by_id(context.bot, chat_id, captcha.message_id, log_error=True)
            if config.captcha.delete_service_message:
                utilities.safe_delete_by_id(context.bot, chat_id, captcha.service_message_id)

            ban_success = False
            try:
                context.bot.ban_chat_member(chat_id, captcha.user.id, revoke_messages=True)
                ban_success = True
            except (TelegramError, BadRequest) as e:
                logger.error("error while banning user: %s", str(e))

            if ban_success and config.captcha.send_message_on_fail:
                try:
                    user_mention = utilities.mention_escaped(captcha.user)
                    context.bot.send_message(
                        chat_id,
                        f"{user_mention} non ha completato il test nei {config.captcha.timeout} minuti previsti, "
                        f"è stato/a bloccato/a, {captcha.get_correct_and_selected_count()} emoji corrette su "
                        f"{captcha.correct_emojis_threshold} [#ban #u{captcha.user.id}]",
                        parse_mode=ParseMode.HTML
                    )
                except (TelegramError, BadRequest) as e:
                    logger.error("error while sending the message in the group: %s", str(e))

            user_id_to_pop.append(user_id)

        if user_id_to_pop:
            logger.debug("popping %d users from %d", len(user_id_to_pop), chat_id)
            for user_id in user_id_to_pop:
                logger.debug("popping %d", user_id)
                chat_data.pop(user_id, None)


def main():
    dispatcher = updater.dispatcher

    new_group_filter = NewGroup()
    dispatcher.add_handler(MessageHandler(new_group_filter, on_new_group_chat))
    dispatcher.add_handler(CommandHandler(["setphoto"], on_setphoto_command, filters=Filters.chat_type.supergroup))
    dispatcher.add_handler(CommandHandler(["testc"], on_forced_captcha_command, filters=Filters.chat_type.supergroup))
    dispatcher.add_handler(MessageHandler(Filters.chat_type.supergroup & Filters.regex(r"^!(?:ur|unrestrict)"), on_unrestrict_command))
    dispatcher.add_handler(MessageHandler(Filters.status_update.new_chat_members & ~new_group_filter, on_new_member))

    dispatcher.add_handler(CallbackQueryHandler(on_already_selected_button, pattern=r'^button:already_(solved|error):user(\d+)$'))
    dispatcher.add_handler(CallbackQueryHandler(on_button, pattern=r'^button:(.*):user(\d+)$'))

    updater.job_queue.run_repeating(cleanup_and_ban, interval=60, first=60)

    updater.bot.set_my_commands([])  # make sure the bot doesn't have any command set...
    updater.bot.set_my_commands(  # ...then set the scope for private chats
        [],
        scope=BotCommandScopeAllPrivateChats()
    )
    updater.bot.set_my_commands(  # ...then set the scope for group administrators
        [BotCommand("setphoto", "imposta una foto come background del captcha")],
        scope=BotCommandScopeAllChatAdministrators()
    )

    allowed_updates = ["message", "callback_query"]  # https://core.telegram.org/bots/api#getupdates

    logger.info("running as @%s, allowed updates: %s", updater.bot.username, allowed_updates)
    updater.start_polling(drop_pending_updates=True, allowed_updates=allowed_updates)
    updater.idle()


if __name__ == '__main__':
    main()
