from asyncio import Event, wait_for
from functools import partial
from time import time

from pyrogram.filters import regex, user
from pyrogram.handlers import CallbackQueryHandler

from .. import DOWNLOAD_DIR, LOGGER
from ..core.config_manager import Config
from ..helper.listeners.task_listener import TaskListener
from ..helper.ext_utils.bot_utils import new_task
from ..helper.ext_utils.task_manager import pre_task_check
from ..helper.mirror_leech_utils.download_utils.anime_scraper import (
    AniWatchScraper,
    anilist_episode_info,
)
from ..helper.mirror_leech_utils.download_utils.yt_dlp_download import YoutubeDLHelper
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    auto_delete_message,
    delete_links,
    edit_message,
    send_message,
)

_anime_scraper = AniWatchScraper()

_user_sessions = {}


class AnimeSession:
    def __init__(self, message, user_id):
        self.message = message
        self.user_id = user_id
        self.results = []
        self.anime_slug = ""
        self.anime_title = ""
        self.anime_id = ""
        self.episodes = []
        self.selected_eps = []
        self.category = "sub"
        self.event = Event()
        self.step = "search"
        self._reply_to = None
        self._timeout = 180
        self._time = time()

    async def _event_handler(self):
        pfunc = partial(anime_callback, obj=self)
        handler = self.message._client.add_handler(
            CallbackQueryHandler(
                pfunc, filters=regex("^anime") & user(self.user_id)
            ),
            group=-1,
        )
        try:
            await wait_for(self.event.wait(), timeout=self._timeout)
        except Exception:
            await edit_message(self._reply_to, "Timed out. Anime session cancelled.")
            self.event.set()
        finally:
            self.message._client.remove_handler(*handler)


class AnimeTask(TaskListener):
    def __init__(self, client, message):
        self.message = message
        self.client = client
        super().__init__()
        self.is_leech = False
        self.is_cancelled = False
        self._is_anime = True
        self.is_ytdlp = True
        self.hybrid_leech = False
        self.bot_trans = False
        self.user_trans = False
        self.db_metadata = None
        self.rename = Config.RENAME_TEMPLATE
        self.mode = ("#ytdlp", "#Leech" if self.is_leech else "#GDrive")


@new_task
async def anime_callback(_, query, obj):
    data = query.data.split()
    message = query.message
    await query.answer()

    action = data[1]

    if action == "cancel":
        await edit_message(message, "Anime search cancelled.")
        obj.event.set()
        return

    if action == "select":
        idx = int(data[2])
        if 0 <= idx < len(obj.results):
            result = obj.results[idx]
            obj.anime_slug = result.slug
            obj.anime_title = result.title
            obj.step = "episodes"
            await edit_message(message, f"Fetching details for **{result.title}**...")
            try:
                details = await _anime_scraper.get_anime_details(result.slug)
                if details and details.get("anime_id"):
                    obj.anime_id = details["anime_id"]
                    result.sub = details.get("sub", result.sub)
                    result.dub = details.get("dub", result.dub)
                    result.total_eps = details.get("total_eps", result.total_eps)
                else:
                    await edit_message(message, "Failed to fetch anime details.")
                    obj.event.set()
                    return
                obj.episodes = await _anime_scraper.get_episodes(obj.anime_id)
            except Exception as e:
                LOGGER.error("Failed to fetch episodes: %s", e)
                await edit_message(message, f"Failed to fetch episodes: {e}")
                obj.event.set()
                return
            if not obj.episodes:
                await edit_message(message, "No episodes found.")
                obj.event.set()
                return
            await _show_episodes(obj, message)
        return

    if action == "cat":
        obj.category = data[2]
        await _show_episode_selection(obj, message)
        return

    if action == "range":
        range_str = data[2]
        if "-" in range_str:
            start, end = map(int, range_str.split("-"))
            obj.selected_eps = [
                ep for ep in obj.episodes if start <= ep.number <= end
            ]
        elif range_str == "all":
            obj.selected_eps = list(obj.episodes)
        else:
            num = int(range_str)
            obj.selected_eps = [ep for ep in obj.episodes if ep.number == num]

        if obj.selected_eps:
            obj.step = "download"
            obj.event.set()
        return

    if action == "custom":
        await edit_message(message, "Send episode range (e.g. `100-120` or `5`):")
        obj.step = "custom_input"
        return


async def _show_episodes(obj, message):
    text = (
        f"**{obj.anime_title}**\n"
        f"Episodes: {len(obj.episodes)}\n\n"
        f"Select audio:\n"
    )

    buttons = ButtonMaker()
    buttons.data_button("Sub", "anime cat sub")
    buttons.data_button("Dub", "anime cat dub")

    if len(obj.episodes) <= 12:
        ep_text = ", ".join(str(ep.number) for ep in obj.episodes)
        text += f"Episodes: {ep_text}\n\n"
        for ep in obj.episodes:
            buttons.data_button(str(ep.number), f"anime range {ep.number}")
    else:
        batch_size = 20
        for i in range(0, len(obj.episodes), batch_size):
            start = obj.episodes[i].number
            end = obj.episodes[min(i + batch_size - 1, len(obj.episodes) - 1)].number
            buttons.data_button(f"{start}-{end}", f"anime range {start}-{end}")
        buttons.data_button("All", "anime range all")

    buttons.data_button("Custom", "anime custom")
    buttons.data_button("Cancel", "anime cancel", "footer")

    obj._reply_to = await edit_message(message, text, buttons.build_menu(2))


async def _show_episode_selection(obj, message):
    text = (
        f"**{obj.anime_title}** | {obj.category.upper()}\n"
        f"Episodes: {len(obj.episodes)}\n\n"
        f"Select episodes to download:\n"
    )

    buttons = ButtonMaker()

    if len(obj.episodes) <= 12:
        for ep in obj.episodes:
            buttons.data_button(str(ep.number), f"anime range {ep.number}")
    else:
        batch_size = 12
        for i in range(0, len(obj.episodes), batch_size):
            start = obj.episodes[i].number
            end = obj.episodes[min(i + batch_size - 1, len(obj.episodes) - 1)].number
            buttons.data_button(f"{start}-{end}", f"anime range {start}-{end}")
        buttons.data_button("All", "anime range all")

    buttons.data_button("Custom", "anime custom")
    buttons.data_button("Cancel", "anime cancel", "footer")

    obj._reply_to = await edit_message(message, text, buttons.build_menu(3))


@new_task
async def anime_search(client, message):
    text = message.text.split(maxsplit=1)
    if len(text) < 2:
        await send_message(
            message,
            "**Anime Search**\n\n"
            "Usage: `/anime <query>`\n"
            "Example: `/anime naruto`\n\n"
            "Then select from results to download episodes.",
        )
        return

    query = text[1].strip()

    check_msg, check_button = await pre_task_check(message)
    if check_msg:
        await delete_links(message)
        await auto_delete_message(
            await send_message(message, check_msg, check_button)
        )
        return

    searching_msg = await send_message(message, f"Searching for **{query}**...")

    try:
        results = await _anime_scraper.search(query)
    except Exception as e:
        LOGGER.error("Anime search failed: %s", e)
        await edit_message(searching_msg, f"Search failed: {e}")
        return

    if not results:
        await edit_message(searching_msg, f"No results found for **{query}**.")
        return

    user_id = message.from_user.id
    session = AnimeSession(message, user_id)
    session.results = results
    session._reply_to = searching_msg

    result_text = f"**Search Results for:** `{query}`\n\n"
    buttons = ButtonMaker()

    for i, result in enumerate(results):
        flags = []
        if result.sub:
            flags.append("Sub")
        if result.dub:
            flags.append("Dub")
        label = f"{result.title} ({', '.join(flags)}) — {result.total_eps} eps"
        buttons.data_button(label, f"anime select {i}")

    buttons.data_button("Cancel", "anime cancel", "footer")

    _user_sessions[user_id] = session

    await edit_message(searching_msg, result_text, buttons.build_menu(1))
    await session._event_handler()

    if session.step == "download" and session.selected_eps:
        await _start_anime_download(session)


async def _start_anime_download(session):
    for ep in session.selected_eps:
        try:
            source = await _anime_scraper.get_episode_source(
                ep.ep_id, session.category
            )
            if not source:
                LOGGER.warning("No source for EP%s, skipping", ep.number)
                continue

            await _download_episode(session, ep, source)

        except Exception as e:
            LOGGER.error("Failed to download EP%s: %s", ep.number, e)

    await send_message(
        session.message,
        f"Finished processing **{session.anime_title}** "
        f"({len(session.selected_eps)} episodes).",
    )

    if session.user_id in _user_sessions:
        del _user_sessions[session.user_id]


async def _download_episode(session, ep, source):
    ep_name = f"{session.anime_title} - EP{ep.number:02d}"

    listener = AnimeTask(session.message._client, session.message)
    listener.link = source.url
    listener.name = ep_name
    listener.is_leech = True
    listener.is_cancelled = False
    listener.source_url = source.url
    listener._set_mode_engine()

    db_metadata = {
        "title": session.anime_title,
        "season": "",
        "episode": f"E{ep.number:02d}",
    }
    if source.resolution:
        db_metadata["resolution"] = source.resolution

    try:
        info = await anilist_episode_info(
            int(session.anime_id) if session.anime_id.isdigit() else 0,
            ep.number,
        )
        if info:
            db_metadata["title"] = info.get("title") or session.anime_title
            db_metadata["episode_title"] = info.get("episode_title", "")
            LOGGER.info(
                "AniList EP%s: title=%s, ep_title=%s",
                ep.number, db_metadata["title"], db_metadata["episode_title"],
            )
    except Exception as e:
        LOGGER.warning("AniList lookup failed for EP%s: %s", ep.number, e)

    listener.db_metadata = db_metadata

    try:
        await listener.before_start()
    except ValueError as e:
        await listener.on_download_error(str(e))
        return

    path = f"{DOWNLOAD_DIR}{listener.mid}/"

    ydl = YoutubeDLHelper(listener)
    ydl.opts["http_headers"] = source.headers
    ydl.opts["format"] = "best"
    ydl.opts["outtmpl"] = {"default": f"{path}/{ep_name}.%(ext)s"}
    ydl.opts["writethumbnail"] = False
    ydl.opts["downloader"] = "ffmpeg"
    ydl.opts["downloader_args"] = {"ffmpeg": ["-hls_use_mpegts", ""]}

    if source.resolution:
        LOGGER.info("EP%s resolution: %s", ep.number, source.resolution)

    try:
        await ydl.add_download(path, "best", False, {})
    except Exception as e:
        LOGGER.error("YT-DLP download failed for EP%s: %s", ep.number, e)
        await listener.on_download_error(str(e))
