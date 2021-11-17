import datetime
import json
import logging
import logging.config
import os
import random
import re
import time
from functools import wraps
from pathlib import Path
from random import choice
from typing import List, Callable, Optional

from telegram import Update, TelegramError, Chat, ParseMode, Bot, BotCommandScopeAllPrivateChats, BotCommand, User, \
    InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, BotCommandScopeAllChatAdministrators
from telegram.error import BadRequest
from telegram.ext import Updater, CallbackContext, Filters, MessageHandler, CallbackQueryHandler, MessageFilter, \
    CommandHandler, ExtBot, Defaults
from telegram.utils.request import Request

import keyboards
import utilities
from emojis import Emoji
from santa import SecretSanta
from mwt import MWT
from config import config

ACTIVE_SECRET_SANTA_KEY = "active_secret_santa"

EMPTY_SECRET_SANTA_STR = f'{Emoji.SANTA}{Emoji.TREE} Nobody joined this Secret Santa yet! Use the "<b>join</b>" button below to join'


updater = Updater(
    bot=ExtBot(
        token=config.telegram.token,
        defaults=Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True),
        # https://github.com/python-telegram-bot/python-telegram-bot/blob/8531a7a40c322e3b06eb943325e819b37ee542e7/telegram/ext/updater.py#L267
        request=Request(con_pool_size=config.telegram.get('workers', 1) + 4)
    ),
    workers=1,
    persistence=utilities.persistence_object()
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


def get_secret_santa():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):

            if ACTIVE_SECRET_SANTA_KEY not in context.chat_data:
                santa = None
            else:
                santa = SecretSanta.from_dict(context.chat_data[ACTIVE_SECRET_SANTA_KEY])

            result_santa = func(update, context, santa, *args, **kwargs)
            if result_santa and isinstance(result_santa, SecretSanta):
                # print(result_santa)
                # time.sleep(5)
                # result_santa.add(update.effective_user)
                # print(result_santa)
                context.chat_data[ACTIVE_SECRET_SANTA_KEY] = result_santa.dict()

        return wrapped
    return real_decorator


def gen_santa_link(santa: SecretSanta):
    link = ""
    if utilities.is_supergroup(santa.chat_id):
        link = utilities.message_link(santa.chat_id, santa.santa_message_id, force_private=True)

    return link


def update_secret_santa_message(context: CallbackContext, santa: SecretSanta):
    participants_count = santa.get_participants_count()
    if not participants_count:
        text = EMPTY_SECRET_SANTA_STR
    else:
        participants_list = []
        i = 1
        for participant_id, participant in santa.participants.items():
            string = f'<b>{i}</b>. {utilities.mention_escaped_by_id(participant_id, participant["name"])}'
            participants_list.append(string)
            i += 1

        min_participants_text = ""
        if santa.get_missing_count():
            min_participants_text = f". Other <b>{santa.get_missing_count()}</b> people are needed to start it"

        base_text = '{santa} Oh-oh! A new Secret Santa!\nParticipants list:\n\n{participants}\n\n' \
                    'Use the "<b>join</b>" button below to join!\n' \
                    'Only {creator} can start this Secret Santa{min_participants}'
        text = base_text.format(
            santa=Emoji.SANTA,
            participants="\n".join(participants_list),
            creator=santa.creator_name,
            min_participants=min_participants_text
        )

    reply_markup = keyboards.secret_santa(
        santa.chat_id,
        context.bot.username,
        participants_count=participants_count
    )

    try:
        edited_message = context.bot.edit_message_text(
            chat_id=santa.chat_id,
            message_id=santa.santa_message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    except (BadRequest, TelegramError) as e:
        logger.error("exception while editing secret santa message (%d, %d): %s", santa.chat_id, santa.santa_message_id, str(e))
        return

    return edited_message


@fail_with_message()
@get_secret_santa()
def on_new_secret_santa_command(update: Update, context: CallbackContext, secret_santa: Optional[SecretSanta] = None):
    logger.debug("/new from %d -> %d", update.effective_user.id, update.effective_chat.id)
    if secret_santa:
        santa_link = gen_santa_link(secret_santa)

        text_message_exists = f"👆 There is already an <a href=\"{santa_link}\">active Secret Santa</a> in this chat! " \
                              f"You can ask {secret_santa.creator_name_escaped} to cancel it using the message's " \
                              f"buttons"
        try:
            context.bot.send_message(
                update.effective_chat.id,
                text_message_exists,
                reply_to_message_id=secret_santa.santa_message_id,
                allow_sending_without_reply=False
            )
        except (TelegramError, BadRequest) as e:
            if str(e).lower() != "replied message not found":
                raise e

            update.message.reply_html(f"{Emoji.SANTA} There is already an active Secret Santa"
                                      f" in this chat! You can ask {secret_santa.creator_name_escaped} "
                                      f"(or an administrator) to cancel it using /cancel")

        return

    new_secret_santa = SecretSanta(
        origin_message_id=update.message.message_id,
        user_id=update.effective_user.id,
        user_name=update.effective_user.first_name,
        chat_id=update.effective_chat.id,
        chat_title=update.effective_chat.title,
    )

    reply_markup = keyboards.secret_santa(update.effective_chat.id, context.bot.username)
    sent_message = update.message.reply_html(
        EMPTY_SECRET_SANTA_STR,
        reply_markup=reply_markup
    )

    new_secret_santa.santa_message_id = sent_message.message_id

    return new_secret_santa


def find_santa(dispatcher_user_data: dict, santa_chat_id: int):
    for chat_data_chat_id, chat_data in dispatcher_user_data.items():
        if chat_data_chat_id != santa_chat_id:
            continue

        if ACTIVE_SECRET_SANTA_KEY not in chat_data:
            logger.debug("chat_data for chat %d exists, but there is no active secret santa", santa_chat_id)
            return

        santa_dict = chat_data[ACTIVE_SECRET_SANTA_KEY]
        return SecretSanta.from_dict(santa_dict)


@fail_with_message()
def on_join_command(update: Update, context: CallbackContext):
    santa_chat_id = int(context.matches[0].group(1))
    logger.debug("start command from %d: %d", update.effective_user.id, santa_chat_id)

    santa = find_santa(context.dispatcher.chat_data, santa_chat_id)
    if not santa:
        raise ValueError(f"chat {santa_chat_id} should allow users to join (no actve santa), but an user joined")

    santa.add(update.effective_user)
    context.dispatcher.chat_data[santa_chat_id][ACTIVE_SECRET_SANTA_KEY] = santa.dict()

    santa_link = gen_santa_link(santa)

    reply_markup = keyboards.leave_private(santa_chat_id)
    update.message.reply_html(
        f"{Emoji.TREE} You joined {utilities.escape(santa.chat_title)}'s <a href=\"{santa_link}\">Secret Santa</a>!",
        reply_markup=reply_markup
    )

    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
@get_secret_santa()
def on_leave_button_group(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("leave button in group: %d", update.effective_chat.id)

    if not santa.is_participant(update.effective_user):
        update.callback_query.answer(f"{Emoji.FREEZE} You haven't joined this Secret Santa!", show_alert=True)
        return

    santa.remove(update.effective_user)
    update_secret_santa_message(context, santa)

    update.callback_query.answer(f"You have been removed from this Secret Santa")

    return santa


@fail_with_message(answer_to_message=False)
def on_leave_button_private(update: Update, context: CallbackContext):
    logger.debug("leave button in private: %d", update.effective_chat.id)

    santa_chat_id = int(context.matches[0].group(1))
    logger.debug("chat_id: %d", santa_chat_id)

    santa = find_santa(context.dispatcher.chat_data, santa_chat_id)
    if not santa:
        # we do not edit or delete this message when a Secrt Santa is started, so we leave the button there
        update.callback_query.answer(f"This chat's Secret Santa is no longer valid", show_alert=True)
        update.callback_query.edit_message_reply_markup(reply_markup=None)
        return

    if not santa.is_participant(update.effective_user):
        # maybe the user left from the group's message
        update.callback_query.answer(f"{Emoji.FREEZE} You are not participating in this Secret Santa!", show_alert=True)
        update.callback_query.edit_message_reply_markup(reply_markup=None)
        return

    santa.remove(update.effective_user)
    update_secret_santa_message(context, santa)

    santa_link = gen_santa_link(santa)
    text = f"{Emoji.FREEZE} You have been removed from {santa.chat_title_escaped}'s <a href=\"{santa_link}\">Secret Santa</a>"
    update.callback_query.edit_message_text(text, reply_markup=None)
    # update.callback_query.answer(f"You have been removed from this Secret Santa")

    return santa


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

            now = utilities.now_utc()

        if user_id_to_pop:
            logger.debug("popping %d users from %d", len(user_id_to_pop), chat_id)
            for user_id in user_id_to_pop:
                logger.debug("popping %d", user_id)
                chat_data.pop(user_id, None)


def main():
    dispatcher = updater.dispatcher

    new_group_filter = NewGroup()
    dispatcher.add_handler(MessageHandler(new_group_filter, on_new_group_chat))

    dispatcher.add_handler(CommandHandler(["new"], on_new_secret_santa_command, filters=Filters.chat_type.supergroup))
    dispatcher.add_handler(MessageHandler(Filters.chat_type.private & Filters.regex(r"^/start (-?\d+)"), on_join_command))

    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_group, pattern=r'^leave$'))
    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_private, pattern=r'^private:leave:(-\d+)$'))

    updater.job_queue.run_repeating(cleanup_and_ban, interval=60, first=60)

    updater.bot.set_my_commands([])  # make sure the bot doesn't have any command set...
    updater.bot.set_my_commands(  # ...then set the scope for private chats
        [],
        scope=BotCommandScopeAllPrivateChats()
    )
    updater.bot.set_my_commands(  # ...then set the scope for group administrators
        [BotCommand("match", "create the Secret Santa's pairs")],
        scope=BotCommandScopeAllChatAdministrators()
    )

    allowed_updates = ["message", "callback_query"]  # https://core.telegram.org/bots/api#getupdates

    logger.info("running as @%s, allowed updates: %s", updater.bot.username, allowed_updates)
    updater.start_polling(drop_pending_updates=True, allowed_updates=allowed_updates)
    updater.idle()


if __name__ == '__main__':
    main()
