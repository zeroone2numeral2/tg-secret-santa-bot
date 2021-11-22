import datetime
import logging
import os
import pickle
import random
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


def draft(items_list: list):
    # logging.basicConfig(format='[%(levelname)s] %(message)s', level=logging.DEBUG)
    logger = logging.getLogger("draft")

    items_list.sort()

    retry_raffle = True
    while retry_raffle:
        retry_raffle = False  # we will set it to true if there's an issue while creating pairs

        result_pairs = []
        yet_to_match = items_list[:]
        invalid_picks_count = 0
        invalid_picks_threshold = 300

        for i, item in enumerate(items_list):
            logger.debug(f"trying to match: {item}")

            picked_item = random.choice(yet_to_match)

            if picked_item == item and len(yet_to_match) == 1:
                # there is just one item left to match, but the only left item is the item itself
                # the raffle should be invalidated and we should repeat it
                logger.warning("the only item left to match is the item we are trying to match")
                retry_raffle = True
                break
            elif invalid_picks_count > invalid_picks_threshold:
                # sometimes the bot ends in a loop, we need to figure out why
                logger.warning(f"the number of invalid attempts is higher than {invalid_picks_threshold}")
                retry_raffle = True
                break

            while item == picked_item:
                logger.debug(f"invalid match {item} -> {picked_item}: item can't be matched with itself, trying again...")
                invalid_picks_count += 1
                picked_item = random.choice(yet_to_match)

            yet_to_match.remove(picked_item)

            result_pairs.append((item, picked_item))

            yet_to_match_str = ', '.join([f"{i}" for i in yet_to_match])
            logger.debug(f"valid match: {item} -> {picked_item}, yet to match: {yet_to_match_str}")

        if not retry_raffle:
            logger.debug("draft concluded with success")
            return result_pairs
        else:
            logger.warning("draft needs to be repeated!")


def draft_old(items_list: list):
    # logging.basicConfig(format='[%(levelname)s] %(message)s', level=logging.DEBUG)
    logger = logging.getLogger("draft")

    items_list.sort()
    number_of_items = len(items_list)

    retry_raffle = True
    while retry_raffle:
        retry_raffle = False  # we will set it to true if there's an issue while creating pairs
        
        result_pairs = []
        already_matched_to_someone = []
        invalid_picks_count = 0
        invalid_picks_threshold = 300

        for i, item in enumerate(items_list):
            logger.debug(f"trying to match: {item}")

            picked_item = random.choice(items_list)
            
            if picked_item == item and i + 1 == number_of_items:
                # there is just one item left to match, but the only left item is the item itself
                # the raffle should be invalidated and we should repeat it
                logger.warning("the only item left to match is the item we are trying to match")
                retry_raffle = True
                break
            elif invalid_picks_count > invalid_picks_threshold:
                # sometimes the bot ends in a loop, we need to figure this out
                logger.warning(f"the number of invalid attempts is higher than {invalid_picks_threshold}")
                retry_raffle = True
                break

            while item == picked_item or picked_item in already_matched_to_someone:
                if item == picked_item:
                    logger.debug(f"invalid match {item} -> {picked_item}: item can't be matched with itself, trying again...")
                elif picked_item in already_matched_to_someone:
                    logger.debug(f"invalid match {item} -> {picked_item}: item is already matched to someone else, trying again...")

                invalid_picks_count += 1
                picked_item = random.choice(items_list)

            result_pairs.append((item, picked_item))

            already_matched_to_someone.append(picked_item)

            yet_to_match = list(set(items_list) - set(already_matched_to_someone))
            yet_to_match_str = ', '.join([f"{i}" for i in sorted(yet_to_match)])
            logger.debug(f"valid match: {item} -> {picked_item}, yet to match: {yet_to_match_str}")

        if not retry_raffle:
            logger.debug("draft concluded with success")
            return result_pairs
        else:
            logger.warning("draft needs to be repeated!")


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
        store_bot_data=True
    )


if __name__ == "__main__":
    draft(["a", "b", "c", "d", "e", "f"])

