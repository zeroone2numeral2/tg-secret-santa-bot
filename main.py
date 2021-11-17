import datetime
import itertools
import json
import logging
import logging.config
import os
import random
import re
import threading
import time
from functools import wraps
from pathlib import Path
from random import choice
from typing import List, Callable, Optional

from telegram import Update, TelegramError, Chat, ParseMode, Bot, BotCommandScopeAllPrivateChats, BotCommand, User, \
    BotCommandScopeAllChatAdministrators, ChatAction
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

                error_str_message = f"Error during callback <code>{func.__name__}()</code> execution: <code>{error_str}</code>"
                if answer_to_message and update.message:
                    update.message.reply_html(error_str_message)
                elif answer_to_message and update.callback_query:
                    update.effective_message.reply_html(error_str_message)

                if config.telegram.log_chat:
                    context.bot.send_message(config.telegram.log_chat, f"#santa_bot {error_str_message}")

        return wrapped
    return real_decorator


def fail_with_message_job(func):
    @wraps(func)
    def wrapped(context: CallbackContext, *args, **kwargs):
        try:
            return func(context, *args, **kwargs)
        except Exception as e:
            error_str = str(e)
            logger.error('error while running job: %s', error_str, exc_info=True)

            error_str_message = f"Error during job callback <code>{func.__name__}()</code> execution: <code>{error_str}</code>"
            if config.telegram.log_chat:
                context.bot.send_message(config.telegram.log_chat, f"#santa_bot {error_str_message}")

    return wrapped


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
                context.chat_data[ACTIVE_SECRET_SANTA_KEY] = result_santa.dict()

        return wrapped
    return real_decorator


def gen_participants_list(participants: dict):
    participants_list = []
    i = 1
    for participant_id, participant in participants.items():
        string = f'<b>{i}</b>. {utilities.mention_escaped_by_id(participant_id, participant["name"])}'
        participants_list.append(string)
        i += 1

    return participants_list


def update_secret_santa_message(context: CallbackContext, santa: SecretSanta):
    participants_count = santa.get_participants_count()
    if not participants_count:
        text = EMPTY_SECRET_SANTA_STR
        reply_markup = keyboards.secret_santa(
            santa.chat_id,
            context.bot.username,
            participants_count=participants_count
        )
    elif santa.started:
        participants_list = gen_participants_list(santa.participants)

        base_text = '{santa} This Secret Santa has been started and everyone received their match!\n' \
                    'Participants list:\n\n' \
                    '{participants}\n\n' \
                    '{creator} still has some time to cancel it'
        text = base_text.format(
            santa=Emoji.SANTA,
            participants="\n".join(participants_list),
            creator=santa.creator_name_escaped,
        )
        reply_markup = keyboards.revoke()
    else:
        participants_list = gen_participants_list(santa.participants)

        min_participants_text = ""
        if santa.get_missing_count():
            min_participants_text = f". Other <b>{santa.get_missing_count()}</b> people are needed to start it"

        base_text = '{santa} Oh-oh! A new Secret Santa!\nParticipants list:\n\n{participants}\n\n' \
                    'To join, use the "<b>join</b>" button below and then tap on "<b>start </b>".\n' \
                    'Only {creator} can start this Secret Santa{min_participants}'

        text = base_text.format(
            santa=Emoji.SANTA,
            participants="\n".join(participants_list),
            creator=santa.creator_name_escaped,
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
        text_message_exists = f"👆 There is already an <a href=\"{secret_santa.link()}\">active Secret Santa</a> in " \
                              f"this chat! " \
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
    logger.debug("join command from %d: %d", update.effective_user.id, santa_chat_id)

    santa = find_santa(context.dispatcher.chat_data, santa_chat_id)
    if not santa:
        raise ValueError(f"user tried to join, but no secret santa is active in {santa_chat_id}")

    santa.add(update.effective_user)
    context.dispatcher.chat_data[santa_chat_id][ACTIVE_SECRET_SANTA_KEY] = santa.dict()

    if santa.creator_id == update.effective_user.id:
        wait_for_start_text = f"\nYou can start it anytime using the \"<b>start</b>\" button in the group, once " \
                              f"at least {config.santa.min_participants} people have joined"
    else:
        wait_for_start_text = f"Now wait for {santa.creator_name_escaped} to start it"

    reply_markup = keyboards.joined_message(santa_chat_id)
    sent_message = update.message.reply_html(
        f"{Emoji.TREE} You joined {santa.chat_title_escaped}'s {santa.inline_link('Secret Santa')}!\n"
        f"{wait_for_start_text}. You will receive your match here, in your chat",
        reply_markup=reply_markup
    )

    santa.set_user_join_message_id(update.effective_user, sent_message.message_id)

    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
@get_secret_santa()
def on_leave_button_group(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("leave button in group: %d", update.effective_chat.id)

    if not santa.is_participant(update.effective_user):
        update.callback_query.answer(f"{Emoji.FREEZE} You haven't joined this Secret Santa!", show_alert=True)
        return

    # we need this for later
    last_join_message_id = santa.get_user_join_message_id(update.effective_user)

    santa.remove(update.effective_user)
    update_secret_santa_message(context, santa)

    update.callback_query.answer(f"You have been removed from this Secret Santa")

    logger.debug("removing keyboard from last join message in private...")
    context.bot.edit_message_reply_markup(update.effective_user.id, last_join_message_id, reply_markup=None)

    return santa


@fail_with_message(answer_to_message=False)
@get_secret_santa()
def on_match_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("start button: %d", update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} Only {santa.creator_name} can use this button and start the Secret Santa",
            show_alert=True,
            cache_time=60*60*24
        )
        return

    if santa.get_participants_count() % 2 != 0:
        logger.debug("number of participants not even")
        update.callback_query.answer(
            f"{Emoji.CROSS} The number of participants must be even! Right now {santa.get_participants_count()} "
            f"people have joined",
            show_alert=True
        )
        return

    sent_message = update.effective_message.reply_html(f'{Emoji.HOURGLASS} <i>Matching users...</i>')

    blocked_by = []
    for user_id, user_data in santa.participants.items():
        try:
            context.bot.send_chat_action(user_id, ChatAction.TYPING)
        except (TelegramError, BadRequest) as e:
            if "bot was blocked by the user" in str(e).lower():
                logger.debug("%d blocked the bot", user_id)
            else:
                # what to do?
                logger.debug("can't send chat action to %d: %s", user_id, str(e))

            blocked_by.append(utilities.mention_escaped_by_id(user_id, user_data["name"]))

    if blocked_by:
        users_list = ", ".join(blocked_by)
        text = f"I can't start the Secret Santa because some users ({users_list}) have blocked me {Emoji.SAD}\n" \
               f"They need to unblock me so I can send them their match"
        sent_message.edit_text(text)
        return

    matches = utilities.draft(list(santa.participants.keys()))
    for receiver_id, match_id in matches:
        match_name = santa.get_user_name(match_id)
        match_mention = utilities.mention_escaped_by_id(match_id, match_name)

        text = f"{Emoji.SANTA}{Emoji.PRESENT} Your <a href=\"{santa.link()}\">Secret Santa</a> match is " \
               f"{match_mention}!"

        match_message = context.bot.send_message(receiver_id, text)
        santa.set_user_match_message_id(receiver_id, match_message.message_id)

    text = f"Everyone has received their match in their <b>private chats</b>!"
    sent_message.edit_text(text)

    santa.started = True
    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
@get_secret_santa()
def on_cancel_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("cancel button: %d", update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} Only {santa.creator_name} can use this button. Administrators can use /cancel "
            f"to cancel any active secret Santa",
            show_alert=True,
            cache_time=60*60*24
        )
        return

    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    text = "<i>This Secret Santa has been canceled by its creator</i>"
    update.callback_query.edit_message_text(text, reply_markup=None)


@fail_with_message(answer_to_message=False)
@get_secret_santa()
def on_revoke_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("revoke button: %d", update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} Only {santa.creator_name} can use this button",
            show_alert=True,
            cache_time=60*60*24
        )
        return

    couldnt_notify = []
    text = f"{Emoji.WARN} <a href=\"{santa.link()}\">This Secret Santa</a> has been canceled by its creator, " \
           f"<b>please ignore this match</b>, as it is no longer valid"
    for user_id, user_data in santa.participants.items():
        try:
            context.bot.send_message(user_id, text, reply_to_message_id=user_data["match_message_id"])
        except (BadRequest, TelegramError) as e:
            logger.error("could not send revoke notification to %d: %s", user_id, str(e))
            couldnt_notify.append(user_data["name"])

    santa.started = False
    update_secret_santa_message(context, santa)

    text = f"Participants have been notified, Secret Santa re-opened"
    if couldnt_notify:
        text = f"{text}. However, I've not been able to message {', '.join(couldnt_notify)}. " \
               f"You may want to notify them"

    update.callback_query.answer(text, show_alert=True)


@fail_with_message(answer_to_message=False)
@get_secret_santa()
def on_cancel_command(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("/cancel command: %d", update.effective_chat.id)

    if not santa:
        update.message.reply_html("<i>There is no active Secret Santa</i>")
        return

    user_id = update.effective_user.id
    if not santa.creator_id != user_id and user_id not in get_admin_ids(context.bot, update.effective_chat.id):
        logger.debug("user is not admin")
        return

    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    update.message.reply_html("<i>This chat's Secret Santa has ben canceled</i>")


def private_chat_button():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
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
                update.callback_query.answer(f"{Emoji.FREEZE} You are not participating in this Secret Santa!",
                                             show_alert=True)
                update.callback_query.edit_message_reply_markup(reply_markup=None)
                return

            return func(update, context, santa, *args, **kwargs)

        return wrapped
    return real_decorator


@fail_with_message(answer_to_message=False)
@private_chat_button()
def on_update_name_button_private(update: Update, context: CallbackContext, santa: SecretSanta):
    logger.debug("update name button in private: %d", update.effective_chat.id)

    name = update.effective_user.first_name
    santa.set_user_name(update.effective_user, name)

    update.callback_query.answer(f"Your name has been updated to: {name}\nThis option allows you to change your "
                                 f"Telegram name and update it the list (in case there are participants with a "
                                 f"similar name)", show_alert=True)

    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
def on_leave_button_private(update: Update, context: CallbackContext, santa: SecretSanta):
    logger.debug("leave button in private: %d", update.effective_chat.id)

    santa.remove(update.effective_user)
    update_secret_santa_message(context, santa)

    text = f"{Emoji.FREEZE} You have been removed from {santa.chat_title_escaped}'s " \
           f"<a href=\"{santa.link()}\">Secret Santa</a>"
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


@fail_with_message()
def on_help(update: Update, _):
    logger.info("/start or /help from: %s", update.effective_user.id)

    source_code = "https://github.com/zeroone2numeral2/tg-secret-santa-bot"
    text = f"Hello {utilities.html_escape(update.effective_user.first_name)}!" \
           f"\nI can help you organize a Secret Santa 🤫🎅🏼🎁 in your group chats :)\n" \
           f"Just add me to a chat and use <code>/newsanta</code> to start a new Secret Santa." \
           f"\n\nSource code <a href=\"{source_code}\">here</a>"

    update.message.reply_html(text)


def secret_santa_expired(context: CallbackContext, santa: SecretSanta):
    if not santa.started:
        text = f"<i>This Secret Santa expired ({config.santa.timeout} hours has passed from its creation)</i>"
    else:
        participants_list = gen_participants_list(santa.participants)
        text = '{hourglass} This Secret Santa has been closed. Participants list:\n\n{participants}'.format(
            hourglass=Emoji.HOURGLASS,
            participants="\n".join(participants_list)
        )

    try:
        edited_message = context.bot.edit_message_text(
            chat_id=santa.chat_id,
            message_id=santa.santa_message_id,
            text=text,
            reply_markup=None
        )
    except (BadRequest, TelegramError) as e:
        logger.error("exception while closing secret santa message (%d, %d): %s", santa.chat_id, santa.santa_message_id, str(e))
        return

    return edited_message


@fail_with_message_job
def cleanup(context: CallbackContext):
    logger.info("cleanup job...")

    for chat_id, chat_data in context.dispatcher.chat_data.items():
        if ACTIVE_SECRET_SANTA_KEY not in chat_data:
            continue

        santa = SecretSanta.from_dict(chat_data[ACTIVE_SECRET_SANTA_KEY])

        now = utilities.now_utc()
        diff_seconds = (now - santa.created_on).total_seconds()
        if diff_seconds <= config.santa.timeout * 3600:
            continue

        secret_santa_expired(context, santa)

        logger.debug("popping secret santa from chat %d", chat_id)
        chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    logger.info("...cleanup job end")


def main():
    dispatcher = updater.dispatcher

    new_group_filter = NewGroup()
    dispatcher.add_handler(MessageHandler(new_group_filter, on_new_group_chat))

    dispatcher.add_handler(CommandHandler(["new", "newsanta", "santa"], on_new_secret_santa_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(CommandHandler(["cancel"], on_cancel_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(MessageHandler(Filters.chat_type.private & Filters.regex(r"^/start (-?\d+)"), on_join_command))
    dispatcher.add_handler(CommandHandler(["start", "help"], on_help, filters=Filters.chat_type.private))

    dispatcher.add_handler(CallbackQueryHandler(on_match_button, pattern=r'^match'))
    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_group, pattern=r'^leave$'))
    dispatcher.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r'^cancel'))
    dispatcher.add_handler(CallbackQueryHandler(on_revoke_button, pattern=r'^revoke'))
    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_private, pattern=r'^private:leave:(-\d+)$'))
    dispatcher.add_handler(CallbackQueryHandler(on_update_name_button_private, pattern=r'^private:updatename:(-\d+)$'))

    updater.job_queue.run_repeating(cleanup, interval=60*30, first=60)

    updater.bot.set_my_commands([])  # make sure the bot doesn't have any command set...
    updater.bot.set_my_commands(  # ...then set the scope for private chats
        [BotCommand("help", "welcome message")],
        scope=BotCommandScopeAllPrivateChats()
    )
    updater.bot.set_my_commands(  # ...then set the scope for group administrators
        [BotCommand("newsanta", "create a new Secret Santa in this chat")],
        scope=BotCommandScopeAllChatAdministrators()
    )

    allowed_updates = ["message", "callback_query"]  # https://core.telegram.org/bots/api#getupdates

    logger.info("running as @%s, allowed updates: %s", updater.bot.username, allowed_updates)
    updater.start_polling(drop_pending_updates=True, allowed_updates=allowed_updates)
    updater.idle()


if __name__ == '__main__':
    main()
