import datetime
import logging
import os
import pickle
import re
from html import escape

# noinspection PyPackageRequirements
from typing import Union

from telegram import Message, User, Bot, Chat
# noinspection PyPackageRequirements
from telegram.ext import PicklePersistence

logger = logging.getLogger(__name__)


def now_utc():
    return datetime.datetime.utcnow()


def now():
    return datetime.datetime.now()


def html_escape(string: str):
    return escape(string)


def mention_escaped(user: User, label="", full_name=False):
    if not label:
        label = user.first_name if not full_name else user.full_name

    return user.mention_html(html_escape(label))


def mention_escaped_by_id(user_id: int, name: str):
    return f'<a href="tg://user?id={user_id}">{html_escape(name)}</a>'


def first_dict_item(origin_dict: dict):
    for _, val in origin_dict.items():
        return val


def is_supergroup(chat: Union[Chat, int]):
    chat_id = chat
    if isinstance(chat, Chat):
        chat_id = chat.id

    return str(chat_id).startswith("-100")


def chat_id_link(chat_id: int):
    return int(re.sub(r"^-?100", "", str(chat_id)))


def message_link(chat: Union[Chat, int], message_id: int, force_private=False):
    chat_id = chat.id if isinstance(chat, Chat) else chat

    if isinstance(chat, Chat) and not force_private and chat.username:
        return f"https://t.me/{chat.username}/{message_id}"

    return f"https://t.me/c/{chat_id_link(chat_id)}/{message_id}"


def safe_delete(message: Message):
    # noinspection PyBroadException
    try:
        message.delete()
        return True
    except Exception:
        return False


def safe_delete_by_id(bot: Bot, chat_id: int, message_id: int, log_error=True):
    # noinspection PyBroadException
    try:
        bot.delete_message(chat_id, message_id)
        return True
    except Exception as e:
        if log_error:
            logger.error("error while deleting message %d from chat %d: %s", message_id, chat_id, str(e))
        return False


def persistence_object(file_path='persistence/data.pickle'):
    logger.info('unpickling persistence: %s', file_path)
    try:
        # try to load the file
        try:
            with open(file_path, "rb") as f:
                pickle.load(f)
        except FileNotFoundError:
            pass
    except (pickle.UnpicklingError, EOFError):
        logger.warning('deserialization failed: removing persistence file and trying again')
        os.remove(file_path)

    return PicklePersistence(
        filename=file_path,
        store_chat_data=True,
        store_user_data=True,
        store_bot_data=False
    )

