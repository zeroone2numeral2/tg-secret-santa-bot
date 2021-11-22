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
from typing import List, Callable, Optional, Union

from telegram import Update, TelegramError, Chat, ParseMode, Bot, BotCommandScopeAllPrivateChats, BotCommand, User, \
    BotCommandScopeAllChatAdministrators, ChatAction, ChatMemberLeft, ChatMemberUpdated, ChatMemberMember, \
    BotCommandScopeChatAdministrators, ChatMember
from telegram.error import BadRequest
from telegram.ext import Updater, CallbackContext, Filters, MessageHandler, CallbackQueryHandler, MessageFilter, \
    CommandHandler, ExtBot, Defaults, ChatMemberHandler
from telegram.utils.request import Request

import keyboards
import utilities
from emojis import Emoji
from santa import SecretSanta
from mwt import MWT
from config import config

ACTIVE_SECRET_SANTA_KEY = "active_secret_santa"
MUTED_KEY = "muted"
BLOCKED_KEY = "blocked"
RECENTLY_LEFT_KEY = "recently_left"

EMPTY_SECRET_SANTA_STR = f'{Emoji.SANTA}{Emoji.TREE} Nobody joined this Secret Santa yet! Use the "<b>join</b>" button below to join'


class Time:
    WEEK_4 = 60 * 60 * 24 * 7 * 4
    WEEK_1 = 60 * 60 * 24 * 7
    DAY_3 = 60 * 60 * 24 * 3
    DAY_1 = 60 * 60 * 24
    HOUR_48 = 60 * 60 * 48
    HOUR_12 = 60 * 60 * 12
    HOUR_6 = 60 * 60 * 6
    HOUR_1 = 60 * 60
    MINUTE_30 = 60 * 30
    MINUTE_1 = 60


class Error:
    SEND_MESSAGE_DISABLED = "have no rights to send a message"
    REMOVED_FROM_GROUP = "bot was kicked from the"  # it might continue with "group chat" or "supergroup chat"
    CANT_EDIT = "chat_write_forbidden"  # we receive this when we try to edit a message/answer a callback query but we are muted
    MESSAGE_TO_EDIT_NOT_FOUND = "message to edit not found"


class Commands:
    PRIVATE = [BotCommand("help", "welcome message")]
    GROUP_ADMINISTRATORS = [
        BotCommand("newsanta", "create a new Secret Santa in this chat"),
        BotCommand("cancel", "cancel any ongoing Secret Santa"),
        BotCommand("hidecommands", "hide these commands"),
    ]


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

BOT_LINK = f"https://t.me/{updater.bot.username}"


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


def bot_restricted_check():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            if MUTED_KEY in context.chat_data:
                logger.info("received an update from chat %d, but we are muted", update.effective_chat.id)
                return

            try:
                return func(update, context, *args, **kwargs)
            except (TelegramError, BadRequest) as e:
                error_str = str(e).lower()
                if Error.REMOVED_FROM_GROUP in error_str:
                    # we shouldn't receive these ever since we handle my_chat_member updates
                    logger.info("removed from chat chat %d: cleaning up", update.effective_chat.id)
                    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)
                elif Error.SEND_MESSAGE_DISABLED in error_str or Error.CANT_EDIT in error_str:
                    logger.info("can't send messages in chat %d: marking as muted", update.effective_chat.id)
                    context.chat_data[MUTED_KEY] = True

                    # can't edit messages if muted
                    # cancel_because_cant_send_messages(context, santa)
                else:
                    raise e

        return wrapped
    return real_decorator


def fail_with_message(answer_to_message=True):
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            try:
                return func(update, context, *args, **kwargs)
            except Exception as e:
                error_str = str(e)
                logger.error('error while running callback: %s', error_str, exc_info=True)

                error_str_message = f"Error during callback <code>{func.__name__}()</code> execution: <code>{utilities.escape(error_str)}</code>"
                if answer_to_message and update.message:
                    update.message.reply_html(error_str_message)
                elif answer_to_message and update.callback_query:
                    update.effective_message.reply_html(error_str_message)

                if config.telegram.log_chat:
                    context.bot.send_message(config.telegram.log_chat, f"#{context.bot.username} {error_str_message}")

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

            error_str_message = f"Error during job callback <code>{func.__name__}()</code> execution: <code>{utilities.escape(error_str)}</code>"
            if config.telegram.log_chat:
                context.bot.send_message(config.telegram.log_chat, f"#{context.bot.username} {error_str_message}")

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


def gen_participants_list(participants: dict, join_by: Optional[str] = None):
    participants_list = []
    i = 1
    for participant_id, participant in participants.items():
        string = f'<b>{i}</b>. {utilities.mention_escaped_by_id(participant_id, participant["name"])}'
        participants_list.append(string)
        i += 1

    if isinstance(join_by, str):
        return join_by.join(participants_list)

    return participants_list


def cancel_because_cant_send_messages(context: CallbackContext, santa: SecretSanta):
    text = "<i>This Secret Santa was canceled because I can't send messages in this group</i>"
    if santa.get_participants_count():
        participants_list = gen_participants_list(santa.participants, join_by="\n")
        text = f"{text}\nParticipants:\n\n{participants_list}"

    return context.bot.edit_message_text(
        chat_id=santa.chat_id,
        message_id=santa.santa_message_id,
        text=text,
        reply_markup=None
    )


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

        base_text = '{santa} This Secret Santa has been started and everyone ' \
                    '<a href="{bot_link}">received their match</a>!\n' \
                    'Participants list:\n\n' \
                    '{participants}'

        if config.santa.allow_revoke:
            base_text = base_text + "\n\n{creator} still has some time to cancel it"

        text = base_text.format(
            santa=Emoji.SANTA,
            bot_link=BOT_LINK,
            participants="\n".join(participants_list),
            creator=santa.creator_name_escaped,
        )
        reply_markup = keyboards.revoke() if config.santa.allow_revoke else None
    else:
        participants_list = gen_participants_list(santa.participants)

        min_participants_text = ""
        if santa.get_missing_count() > 0:
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


def create_new_secret_santa(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    if santa:
        text_message_exists = f"👆 There is already an <a href=\"{santa.link()}\">active Secret Santa</a> in " \
                              f"this chat! " \
                              f"You can ask {santa.creator_name_escaped} to cancel it using the message's " \
                              f"buttons"
        try:
            context.bot.send_message(
                update.effective_chat.id,
                text_message_exists,
                reply_to_message_id=santa.santa_message_id,
                allow_sending_without_reply=False
            )
        except (TelegramError, BadRequest) as e:
            if str(e).lower() != "replied message not found":
                raise e

            update.message.reply_html(f"{Emoji.SANTA} There is already an active Secret Santa"
                                      f" in this chat! You can ask {santa.creator_name_escaped} "
                                      f"(or an administrator) to cancel it using <code>/cancel</code>")

        return

    new_secret_santa = SecretSanta(
        origin_message_id=update.effective_message.message_id,
        user_id=update.effective_user.id,
        user_name=update.effective_user.first_name,
        chat_id=update.effective_chat.id,
        chat_title=update.effective_chat.title,
    )

    reply_markup = keyboards.secret_santa(update.effective_chat.id, context.bot.username)
    if update.callback_query:
        update.callback_query.edit_message_text(EMPTY_SECRET_SANTA_STR, reply_markup=reply_markup)
        santa_message_id = update.effective_message.message_id
    else:
        sent_message = update.message.reply_html(
            EMPTY_SECRET_SANTA_STR,
            reply_markup=reply_markup
        )
        santa_message_id = sent_message.message_id

    new_secret_santa.santa_message_id = santa_message_id

    return new_secret_santa


@fail_with_message()
@bot_restricted_check()
@get_secret_santa()
def on_new_secret_santa_command(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.info("/newsanta command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    return create_new_secret_santa(update, context, santa)


@fail_with_message()
@bot_restricted_check()
@get_secret_santa()
def on_new_secret_santa_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.info("new secret santa button: %d -> %d", update.effective_user.id, update.effective_chat.id)

    return create_new_secret_santa(update, context, santa)


def find_key(dispatcher_user_data: dict, target_chat_id: int, key_to_find: Union[int, str]) -> bool:
    for chat_data_chat_id, chat_data in dispatcher_user_data.items():
        if chat_data_chat_id != target_chat_id:
            continue

        return key_to_find in chat_data


def find_santa(dispatcher_chat_data: dict, santa_chat_id: int):
    for chat_data_chat_id, chat_data in dispatcher_chat_data.items():
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
    logger.info("join command from %d, chat id: %d", update.effective_user.id, santa_chat_id)

    if find_key(context.dispatcher.chat_data, santa_chat_id, MUTED_KEY):
        update.message.reply_html(f"It looks like I can't send messages in that group. I can't let "
                                  f"new participants join until I can send messages there, I'm sorry {Emoji.SAD}")
        return

    santa = find_santa(context.dispatcher.chat_data, santa_chat_id)
    if not santa:
        # this might happen if the bot was removed from the group: the "join" button is still there
        # we should check if the chat is in the recently left chats in context.bot_data
        if RECENTLY_LEFT_KEY in context.bot_data and santa_chat_id in context.bot_data[RECENTLY_LEFT_KEY]:
            update.message.reply_html(f"It looks like I've been removed from this Secret Santa's group {Emoji.SAD}")
            return
        else:
            raise ValueError(f"user tried to join, but no secret santa is active in {santa_chat_id}")

    if config.santa.max_participants and santa.get_participants_count() >= config.santa.max_participants:
        text = f"I'm sorry, unfortunately {santa.inline_link('this Secret Santa')} has already reached the " \
               f"max number of participants {Emoji.SAD}"
        update.message.reply_html(text)
        return

    santa.add(update.effective_user)
    context.dispatcher.chat_data[santa_chat_id][ACTIVE_SECRET_SANTA_KEY] = santa.dict()

    if santa.creator_id == update.effective_user.id:
        wait_for_start_text = f"\nYou can start it anytime using the \"<b>start match</b>\" button in the group, " \
                              f"once at least {config.santa.min_participants} people have joined"
    else:
        wait_for_start_text = f"Now wait for {santa.creator_name_escaped} to start it"

    reply_markup = keyboards.joined_message(santa_chat_id)
    sent_message = update.message.reply_html(
        f"{Emoji.TREE} You joined {santa.chat_title_escaped}'s {santa.inline_link('Secret Santa')}!\n"
        f"{wait_for_start_text}. You will receive your match here, in this chat",
        reply_markup=reply_markup
    )

    santa.set_user_join_message_id(update.effective_user, sent_message.message_id)

    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_leave_button_group(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("leave button in group: %d -> %d", update.effective_user.id, update.effective_chat.id)

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
@bot_restricted_check()
@get_secret_santa()
def on_match_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("start match button: %d -> %d", update.effective_user.id, update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} Only {santa.creator_name} can use this button and start the Secret Santa match",
            show_alert=True,
            cache_time=Time.DAY_3
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

        text = f"{Emoji.SANTA}{Emoji.PRESENT} You are {match_mention}'s <a href=\"{santa.link()}\">Secret Santa</a>!"

        match_message = context.bot.send_message(receiver_id, text)
        santa.set_user_match_message_id(receiver_id, match_message.message_id)

    text = f"Everyone has received their match in their <a href=\"{BOT_LINK}\">private chats</a>!"
    sent_message.edit_text(text)

    if not config.santa.allow_revoke:
        logger.debug("revoke ability is disabled: removing active secret santa from chat_data")
        context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    santa.start()
    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_cancel_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("cancel button: %d -> %d", update.effective_user.id, update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} Only {santa.creator_name} can use this button. Administrators can use /cancel "
            f"to cancel any active secret Santa",
            show_alert=True,
            cache_time=Time.DAY_3
        )
        return

    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    text = "<i>This Secret Santa has been canceled by its creator</i>"
    update.callback_query.edit_message_text(text, reply_markup=None)


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_revoke_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("revoke button: %d -> %d", update.effective_user.id, update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} Only {santa.creator_name} can use this button",
            show_alert=True,
            cache_time=Time.DAY_3
        )
        return

    return update.callback_query.answer(
        f"{Emoji.WARN} The ability to revoke already-sent matches has been temporarily suspended",
        show_alert=True,
        cache_time=Time.DAY_1
    )

    couldnt_notify = []
    text = f"{Emoji.WARN} <a href=\"{santa.link()}\">This Secret Santa</a> has been canceled by its creator. " \
           f"<b>Please ignore this match</b>, as it is no longer valid"
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
@bot_restricted_check()
def on_hide_commands_command(update: Update, context: CallbackContext):
    logger.debug("/hidecommands command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    context.bot.set_my_commands(
        commands=[],
        scope=BotCommandScopeChatAdministrators(chat_id=update.effective_chat.id)
    )
    update.message.reply_html("Done. It might take some time for them to disappear. "
                              "You can use <code>/showcommands</code> if you want the group admins to be able to "
                              "see them again")


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
def on_show_commands_command(update: Update, context: CallbackContext):
    logger.debug("/showcommands command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    context.bot.set_my_commands(
        commands=Commands.GROUP_ADMINISTRATORS,
        scope=BotCommandScopeChatAdministrators(chat_id=update.effective_chat.id)
    )
    update.message.reply_html("Done. It might take some time for them to appear")


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_cancel_command(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("/cancel command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    if not santa:
        update.message.reply_html("<i>There is no active Secret Santa</i>")
        return

    user_id = update.effective_user.id
    if not santa.creator_id != user_id and user_id not in get_admin_ids(context.bot, update.effective_chat.id):
        logger.debug("user is not admin nor the creator of the secret santa")
        return

    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    try:
        context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=santa.santa_message_id,
            text="<i>This Secret Santa has been canceled by its creator or by an administrator</i>",
            reply_markup=None
        )
    except (TelegramError, BadRequest) as e:
        logger.warning("error while editing canceled secret santa message: %s", str(e))
        if Error.MESSAGE_TO_EDIT_NOT_FOUND not in str(e).lower():
            raise e

    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="<i>This chat's Secret Santa has ben canceled</i>",
        reply_to_message_id=santa.santa_message_id,
        allow_sending_without_reply=True,  # send the message anyway even if the secret santa message has been deleted
    )


def private_chat_button():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            santa_chat_id = int(context.matches[0].group(1))
            logger.debug("private chat button, chat_id: %d", santa_chat_id)

            santa = find_santa(context.dispatcher.chat_data, santa_chat_id)
            if not santa:
                # we do not edit or delete this message when a Secrt Santa is started, so the buttons are still there
                logger.debug("user tapped on a private chat button, but there is no active secret santa for that chat")
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


@fail_with_message(answer_to_message=True)
@private_chat_button()
def on_update_name_button_private(update: Update, context: CallbackContext, santa: SecretSanta):
    logger.debug("update name button in private: %d", update.effective_user.id)

    name = update.effective_user.first_name
    name_updated = False

    if name != santa.get_user_name(update.effective_user):
        santa.set_user_name(update.effective_user, name)
        name_updated = True

    update.callback_query.answer(f"Your name has been updated to: {name}\n\nThis option allows you to change your "
                                 f"Telegram name and update it in the list (helpful if there are participants with "
                                 f"similar names)", show_alert=True)

    if name_updated:
        update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=True)
@private_chat_button()
def on_leave_button_private(update: Update, context: CallbackContext, santa: SecretSanta):
    logger.debug("leave button in private: %d", update.effective_user.id)

    santa.remove(update.effective_user)
    update_secret_santa_message(context, santa)

    text = f"{Emoji.FREEZE} You have been removed from {santa.chat_title_escaped}'s " \
           f"<a href=\"{santa.link()}\">Secret Santa</a>"
    update.callback_query.edit_message_text(text, reply_markup=None)
    # update.callback_query.answer(f"You have been removed from this Secret Santa")

    return santa


@fail_with_message(answer_to_message=False)
def on_new_group_chat(update: Update, _):
    logger.info("new group chat: %d", update.effective_chat.id)

    if config.telegram.exit_unknown_groups and update.effective_user.id not in config.telegram.admins:
        logger.info("unauthorized: leaving...")
        update.effective_chat.leave()
        return

    if not config.santa.start_button_on_new_group:
        return

    text = f"Hello everyone! I'm a bot that helps group chats to organize their " \
           f"Secret Santas {Emoji.SANTA}{Emoji.SHH}\n" \
           f"Anyone can use the button below to start a new one. Alternatively, the <code>/newsanta</code> command " \
           f"can be used"

    update.message.reply_html(
        text,
        reply_markup=keyboards.new_santa(),
        quote=False,
    )


@fail_with_message()
def on_help(update: Update, _):
    logger.info("/start or /help from: %s", update.effective_user.id)

    source_code = "https://github.com/zeroone2numeral2/tg-secret-santa-bot"
    text = f"Hello {utilities.html_escape(update.effective_user.first_name)}!" \
           f"\nI can help you organize a Secret Santa 🤫🎅🏼🎁 in your group chats :)\n" \
           f"Just add me to a chat and use <code>/newsanta</code> to start a new Secret Santa." \
           f"\n\nSource code <a href=\"{source_code}\">here</a>"

    update.message.reply_html(text)


@fail_with_message()
@superadmin
def admin_ongoing_command(update: Update, context: CallbackContext):
    logger.info("/ongoing from %d", update.effective_user.id)

    santa_count = 0
    participants_count = 0
    for chat_data_chat_id, chat_data in context.dispatcher.chat_data.items():
        if ACTIVE_SECRET_SANTA_KEY not in chat_data:
            continue

        santa_count += 1
        santa = SecretSanta.from_dict(chat_data[ACTIVE_SECRET_SANTA_KEY])
        participants_count += santa.get_participants_count()

    update.message.reply_html(f"There are {santa_count} ongoing secret santas, "
                              f"with a total of {participants_count} participants")


def allowed(permission: Optional[bool]):
    if permission is None:
        # None means it's enabled
        return True

    return permission


def was_muted(chat_member_update: ChatMemberUpdated):
    could_send_messages = allowed(chat_member_update.old_chat_member.can_send_messages)
    can_send_messages = allowed(chat_member_update.new_chat_member.can_send_messages)
    if could_send_messages and not can_send_messages:
        return True
    return False


def was_unmuted(chat_member_update: ChatMemberUpdated):
    could_send_messages = allowed(chat_member_update.old_chat_member.can_send_messages)
    can_send_messages = allowed(chat_member_update.new_chat_member.can_send_messages)
    if not could_send_messages and can_send_messages:
        return True
    return False


@fail_with_message(answer_to_message=False)
def on_my_chat_member_update(update: Update, context: CallbackContext):
    logger.debug("my_chat_member update in %d", update.my_chat_member.chat.id)
    my_chat_member = update.my_chat_member

    if my_chat_member.chat.id > 0:
        # status == ChatMemberLeft -> bot was blocked
        # status == ChatMemberMember-> bot was unblocked
        if my_chat_member.new_chat_member.status == ChatMember.LEFT:
            context.user_data[BLOCKED_KEY] = True
        elif my_chat_member.new_chat_member.status == ChatMember.MEMBER:
            context.user_data.pop(BLOCKED_KEY, None)

        return

    # from pprint import pprint
    # pprint(update.to_dict())

    if my_chat_member.new_chat_member.status == ChatMember.LEFT:
        logger.debug("bot removed from %d, removing chat_data...", my_chat_member.chat.id)
        context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)
        context.chat_data.pop(MUTED_KEY, None)
        if RECENTLY_LEFT_KEY not in context.bot_data:
            context.bot_data[RECENTLY_LEFT_KEY] = {}
        context.bot_data[RECENTLY_LEFT_KEY][my_chat_member.chat.id] = utilities.now()
    elif was_muted(my_chat_member):
        logger.debug("bot muted in %d", my_chat_member.chat.id)
        context.chat_data[MUTED_KEY] = True

        # muted -> can't edit messages either
        #
        # if ongoing_secret_santa:
        #     logger.debug("ongoing secret santa: editing message...")
        #     santa = SecretSanta.from_dict(ongoing_secret_santa)
        #     cancel_because_cant_send_messages(context, santa)
    elif was_unmuted(my_chat_member):
        logger.debug("bot unmuted in %d", my_chat_member.chat.id)
        context.chat_data.pop(MUTED_KEY, None)
    else:
        logger.debug("no relevant change happened: %s", my_chat_member)


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

    dispatcher.add_handler(CommandHandler(["ongoing"], admin_ongoing_command, filters=Filters.chat_type.private))

    dispatcher.add_handler(CommandHandler(["new", "newsanta", "santa"], on_new_secret_santa_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(CommandHandler(["cancel"], on_cancel_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(MessageHandler(Filters.chat_type.private & Filters.regex(r"^/start (-?\d+)"), on_join_command))
    dispatcher.add_handler(CommandHandler(["start", "help"], on_help, filters=Filters.chat_type.private))
    dispatcher.add_handler(CommandHandler(["hidecommands"], on_hide_commands_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(CommandHandler(["showcommands"], on_show_commands_command, filters=Filters.chat_type.groups))

    dispatcher.add_handler(CallbackQueryHandler(on_new_secret_santa_button, pattern=r'^newsanta$'))
    dispatcher.add_handler(CallbackQueryHandler(on_match_button, pattern=r'^match$'))
    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_group, pattern=r'^leave$'))
    dispatcher.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r'^cancel$'))
    dispatcher.add_handler(CallbackQueryHandler(on_revoke_button, pattern=r'^revoke$'))

    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_private, pattern=r'^private:leave:(-\d+)$'))
    dispatcher.add_handler(CallbackQueryHandler(on_update_name_button_private, pattern=r'^private:updatename:(-\d+)$'))

    dispatcher.add_handler(ChatMemberHandler(on_my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    updater.job_queue.run_repeating(cleanup, interval=Time.HOUR_1, first=Time.MINUTE_1)

    updater.bot.set_my_commands([])  # make sure the bot doesn't have any command set...
    updater.bot.set_my_commands(  # ...then set the scope for private chats
        commands=Commands.PRIVATE,
        scope=BotCommandScopeAllPrivateChats()
    )
    updater.bot.set_my_commands(  # ...then set the scope for group administrators
        commands=Commands.GROUP_ADMINISTRATORS,
        scope=BotCommandScopeAllChatAdministrators()
    )

    allowed_updates = ["message", "callback_query", "my_chat_member"]  # https://core.telegram.org/bots/api#getupdates

    logger.info("running as @%s, allowed updates: %s", updater.bot.username, allowed_updates)
    updater.start_polling(drop_pending_updates=True, allowed_updates=allowed_updates)
    updater.idle()


if __name__ == '__main__':
    main()
