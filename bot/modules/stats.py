from asyncio import gather, sleep, wait_for, TimeoutError
from pyrogram.enums import ButtonStyle
from platform import platform, version
from re import search as research
from time import time

from aiofiles.os import path as aiopath
from psutil import (
    Process,
    boot_time,
    cpu_count,
    cpu_freq,
    cpu_percent,
    disk_io_counters,
    disk_usage,
    getloadavg,
    net_io_counters,
    swap_memory,
    virtual_memory,
    process_iter,
    NoSuchProcess,
    AccessDenied,
)

from .. import LOGGER, bot_cache, bot_start_time, bot_loop
from ..core.config_manager import Config, BinConfig
from ..helper.ext_utils.bot_lock import get_system_resources_cached
from ..helper.ext_utils.bot_utils import (
    cmd_exec,
    compare_versions,
    git_info,
    new_task,
)
from ..helper.ext_utils.status_utils import (
    get_progress_bar_string,
    get_readable_file_size,
    get_readable_time,
)
from ..helper.telegram_helper.filters import CustomFilters
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)
from ..version import get_version

commands = {
    "aria2": ([BinConfig.ARIA2_NAME, "--version"], r"aria2 version ([\d.]+)"),
    "qBittorrent": ([BinConfig.QBIT_NAME, "--version"], r"qBittorrent v([\d.]+)"),
    "SABnzbd+": (
        [BinConfig.SABNZBD_NAME, "--version"],
        rf"{BinConfig.SABNZBD_NAME}-([\d.]+)",
    ),
    "python": (["python3", "--version"], r"Python ([\d.]+)"),
    "rclone": ([BinConfig.RCLONE_NAME, "--version"], r"rclone v([\d.]+)"),
    "yt-dlp": (["yt-dlp", "--version"], r"([\d.]+)"),
    "ffmpeg": (
        [BinConfig.FFMPEG_NAME, "-version"],
        r"ffmpeg version ([\d.]+(-\w+)?).*",
    ),
    "7z": (["7z", "i"], r"7-Zip ([\d.]+)"),
    "aiohttp": (["uv", "pip", "show", "aiohttp"], r"Version: ([\d.]+)"),
    "wzgram": (["uv", "pip", "show", "wzgram"], r"Version: ([\d.]+)"),
    "gapi": (["uv", "pip", "show", "google-api-python-client"], r"Version: ([\d.]+)"),
    "mega": (
        [
            "python3",
            "-c",
            "from mega import MegaApi; print(MegaApi('test').getVersion())",
        ],
        r"v?([\d.]+)",
    ),
}


async def get_stats(event, key="home"):
    user_id = event.from_user.id
    btns = ButtonMaker()
    if key == "home":
        btns = ButtonMaker()
        btns.data_button("Bot Stats", f"stats {user_id} stbot")
        btns.data_button("OS Stats", f"stats {user_id} stsys")
        btns.data_button("Repo Stats", f"stats {user_id} strepo")
        btns.data_button("Pkgs Stats", f"stats {user_id} stpkgs")
        btns.data_button("Task Limits", f"stats {user_id} tlimits")
        btns.data_button("Sys Tasks", f"stats {user_id} systasks")
        msg = "📊 <b>Bot & OS Statistics</b>"
    elif key == "stbot":
        total, used, free, disk = disk_usage("/")
        swap = swap_memory()
        memory = virtual_memory()
        disk_io = disk_io_counters()
        res = get_system_resources_cached()
        bot_ram_mb = res["ram_mb"]
        bot_ram_total = bot_ram_mb * 1024 * 1024
        user = Process().username()
        bot_ram_used = sum(
            p.memory_info().rss for p in process_iter() if p.username() == user
        )
        bot_ram_free = max(0, bot_ram_total - bot_ram_used)
        bot_ram_pct = (
            round((bot_ram_used / bot_ram_total * 100), 2) if bot_ram_total > 0 else 0
        )
        instance_cpu = res["cpu_count"]
        sys_cpu = cpu_count(logical=True)
        p_cores = cpu_count(logical=False)
        v_cores = (sys_cpu or 0) - (p_cores or 0)
        msg = f"""📊 <b>Bot Statistics</b>
⏱ Uptime: {get_readable_time(time() - bot_start_time)}

🧠 <b>Instance RAM</b>
{get_progress_bar_string(bot_ram_pct)} {bot_ram_pct}%
{get_readable_file_size(bot_ram_used)} used  •  {get_readable_file_size(bot_ram_free)} free  •  {get_readable_file_size(bot_ram_total)} total

💾 <b>System RAM</b>
{get_progress_bar_string(memory.percent)} {memory.percent}%
{get_readable_file_size(memory.used)} used  •  {get_readable_file_size(memory.available)} free  •  {get_readable_file_size(memory.total)} total

🔄 <b>Swap</b>
{get_progress_bar_string(swap.percent)} {swap.percent}%
{get_readable_file_size(swap.used)} used  •  {get_readable_file_size(swap.free)} free  •  {get_readable_file_size(swap.total)} total

⚙️ <b>Instance CPU</b>
Cores: {instance_cpu}  •  Total: {sys_cpu}  •  P: {p_cores}  •  V: {v_cores}
Usable: {len(Process().cpu_affinity())}

💿 <b>Disk</b>
{get_progress_bar_string(disk)} {disk}%
📖 Read: {f"{get_readable_file_size(disk_io.read_bytes)} ({get_readable_time(disk_io.read_time / 1000)})" if disk_io else "N/A"}
📝 Write: {f"{get_readable_file_size(disk_io.write_bytes)} ({get_readable_time(disk_io.write_time / 1000)})" if disk_io else "N/A"}
{get_readable_file_size(used)} used  •  {get_readable_file_size(free)} free  •  {get_readable_file_size(total)} total
"""
    elif key == "stsys":
        cpu_usage = cpu_percent(interval=0.5)
        sys_cpu = cpu_count(logical=True)
        p_cores = cpu_count(logical=False)
        v_cores = (sys_cpu or 0) - (p_cores or 0)
        msg = f"""🖥 <b>System OS</b>
⏱ Uptime: {get_readable_time(time() - boot_time())}
🏷 Version: {version()}
🔧 Arch: {platform()}

🌐 <b>Network</b>
⬆ Upload: {get_readable_file_size(net_io_counters().bytes_sent)}
⬇ Download: {get_readable_file_size(net_io_counters().bytes_recv)}
📦 Pkts: {str(net_io_counters().packets_sent)[:-3]}k sent  •  {str(net_io_counters().packets_recv)[:-3]}k recv
📊 Total I/O: {get_readable_file_size(net_io_counters().bytes_recv + net_io_counters().bytes_sent)}

⚙️ <b>CPU</b>
{get_progress_bar_string(cpu_usage)} {cpu_usage}%
🔄 Freq: {f"{cpu_freq().current / 1000:.2f} GHz" if cpu_freq() else "N/A"}
📈 Avg Load: {"%, ".join(str(round((x / (cpu_count() or 1) * 100), 2)) for x in getloadavg())}% (1m, 5m, 15m)
Cores: P {p_cores}  •  V {v_cores}  •  Total {sys_cpu}  •  Usable {len(Process().cpu_affinity())}
"""
    elif key == "strepo":
        last_commit = git_info.commit_date() or "No Data"
        changelog = git_info.commit_msg() or "N/A"
        if git_info.commit_hash() != "unknown":
            changelog += f" | <code>{git_info.commit_hash()}</code>"
        official_v = (
            await cmd_exec(
                f"curl -o latestversion.py https://raw.githubusercontent.com/SilentDemonSD/WZML-X/{Config.UPSTREAM_BRANCH}/bot/version.py -s && python3 latestversion.py && rm latestversion.py",
                True,
            )
        )[0]
        msg = f"""📦 <b>Repo Statistics</b>

🕐 Updated: {last_commit}
🏷 Current: {get_version()}
🆕 Latest: {official_v}
📝 Changelog: {changelog}

<code>{compare_versions(get_version(), official_v)}</code>
    """
    elif key == "stpkgs":
        ver = bot_cache.get("eng_versions", {})
        msg = f"""📦 <b>Packages</b>

🐍 python v{ver.get("python", "N/A")}
⬇ aria2 v{ver.get("aria2", "N/A")}
🧲 qBittorrent v{ver.get("qBittorrent", "N/A")}
📰 SABnzbd+ v{ver.get("SABnzbd+", "N/A")}
☁ rclone v{ver.get("rclone", "N/A")}
▶ yt-dlp v{ver.get("yt-dlp", "N/A")}
🎬 ffmpeg v{ver.get("ffmpeg", "N/A")}
🗜 7z v{ver.get("7z", "N/A")}
🌐 Aiohttp v{ver.get("aiohttp", "N/A")}
📱 WzGram v{ver.get("wzgram", "N/A")}
📁 Google API v{ver.get("gapi", "N/A")}
💎 MegaSDK v{ver.get("mega", "N/A")}
"""
    elif key == "tlimits":
        msg = f"""🔒 <b>Task Limits</b>

📥 Direct: {Config.DIRECT_LIMIT or "∞"} GB
🧲 Torrent: {Config.TORRENT_LIMIT or "∞"} GB
📁 GDrive DL: {Config.GD_DL_LIMIT or "∞"} GB
☁ RClone DL: {Config.RC_DL_LIMIT or "∞"} GB
📋 Clone: {Config.CLONE_LIMIT or "∞"} GB
⬇ JDown: {Config.JD_LIMIT or "∞"} GB
📰 NZB: {Config.NZB_LIMIT or "∞"} GB
▶ YT-DLP: {Config.YTDLP_LIMIT or "∞"} GB
🎵 Playlist: {Config.PLAYLIST_LIMIT or "∞"}
💎 Mega: {Config.MEGA_LIMIT or "∞"} GB
📤 Leech: {Config.LEECH_LIMIT or "∞"} GB
🗜 Archive: {Config.ARCHIVE_LIMIT or "∞"} GB
📂 Extract: {Config.EXTRACT_LIMIT or "∞"} GB
💿 Storage Threshold: {Config.STORAGE_LIMIT or "∞"} GB

⏳ Token Validity: {get_readable_time(Config.VERIFY_TIMEOUT) if Config.VERIFY_TIMEOUT else "Disabled"}
🕐 User Cooldown: {Config.USER_TIME_INTERVAL or "0"}s/task
👤 User Max Tasks: {Config.USER_MAX_TASKS or "∞"}
🤖 Bot Max Tasks: {Config.BOT_MAX_TASKS or "∞"}
    """

    elif key == "systasks":
        try:
            processes = []
            for proc in process_iter(
                ["pid", "name", "cpu_percent", "memory_percent", "username"]
            ):
                try:
                    info = proc.info
                    if (
                        info.get("cpu_percent", 0) > 1.0
                        or info.get("memory_percent", 0) > 1.0
                    ):
                        processes.append(info)
                except (NoSuchProcess, AccessDenied):
                    continue
            processes.sort(
                key=lambda x: x.get("cpu_percent", 0) + x.get("memory_percent", 0),
                reverse=True,
            )
            processes = processes[:15]
        except Exception:
            processes = []

        msg = "🔥 <b>System Tasks (High Usage)</b>\n\n"

        if processes:
            for i, proc in enumerate(processes, 1):
                name = proc.get("name", "Unknown")[:20]
                cpu = proc.get("cpu_percent", 0)
                mem = proc.get("memory_percent", 0)
                user = proc.get("username", "Unknown")[:10]
                msg += f"<b>{i}.</b> <code>{name}</code>\n   ⚙️ CPU {cpu:.1f}%  •  💾 MEM {mem:.1f}%  •  👤 {user}  •  PID {proc['pid']}\n"
                btns.data_button(f"{i}", f"stats {user_id} killproc {proc['pid']}")
            msg += "\n<i>Click number to terminate process</i>"
        else:
            msg += "<i>No high usage processes found</i>"

        btns.data_button("🔄 Refresh", f"stats {user_id} systasks", "header")

    btns.data_button("Back", f"stats {user_id} home", "footer")
    btns.data_button(
        "Close", f"stats {user_id} close", "footer", style=ButtonStyle.DANGER
    )
    return msg, btns.build_menu(8 if key == "systasks" else 2)


@new_task
async def bot_stats(_, message):
    msg, btns = await get_stats(message)
    await send_message(message, msg, btns, photo="IMAGES")


@new_task
async def stats_pages(_, query):
    data = query.data.split()
    message = query.message
    user_id = query.from_user.id
    if user_id != int(data[1]):
        await query.answer("Not Yours!", show_alert=True)
    elif data[2] == "close":
        await query.answer()
        await delete_message(message, message.reply_to_message)
    elif data[2] == "killproc":
        if not await CustomFilters.owner(_, query):
            await query.answer("Sorry! You cannot Kill System Tasks!", show_alert=True)
            return
        pid = int(data[3])
        try:
            process = Process(pid)
            proc_name = process.name()
            process.terminate()
            await sleep(2)
            if process.is_running():
                process.kill()
                status = "🔥 Force killed"
            else:
                status = "✅ Terminated"
            await query.answer(f"{status}: {proc_name} (PID: {pid})", show_alert=True)
        except NoSuchProcess:
            await query.answer(
                "❌ Process not found or already terminated!", show_alert=True
            )
        except AccessDenied:
            await query.answer(
                "❌ Access denied! Cannot kill this process.", show_alert=True
            )
        except Exception as e:
            await query.answer(f"❌ Error: {str(e)}", show_alert=True)

        msg, btns = await get_stats(query, "systasks")
        await edit_message(message, msg, btns)
    else:
        if data[2] == "systasks" and not await CustomFilters.sudo(_, query):
            await query.answer("Sorry! You cannot open System Tasks!", show_alert=True)
            return
        await query.answer()
        msg, btns = await get_stats(query, data[2])
        await edit_message(message, msg, btns)


async def get_version_async(command, regex, timeout=5):
    try:
        out, err, code = await wait_for(cmd_exec(command), timeout=timeout)
        if code != 0:
            return f"Error: {err}"
        match = research(regex, out)
        return match.group(1) if match else "-"
    except TimeoutError:
        return "Timeout"
    except Exception as e:
        return f"Exception: {str(e)}"


async def retry_mega_version():
    await sleep(60)
    command, regex = commands["mega"]
    version = await get_version_async(command, regex, timeout=10)
    if version != "Timeout" and not version.startswith("Exception"):
        bot_cache["eng_versions"]["mega"] = version
        LOGGER.info(f"MegaSDK Version Fetched: {version}")
    else:
        LOGGER.warning(f"Failed to fetch MegaSDK Version: {version}")


@new_task
async def get_packages_version():
    tasks = [get_version_async(command, regex) for command, regex in commands.values()]
    versions = await gather(*tasks)
    bot_cache["eng_versions"] = {}
    for tool, ver in zip(commands.keys(), versions):
        bot_cache["eng_versions"][tool] = ver
    if await aiopath.exists(".git"):
        last_commit = await cmd_exec(
            "git log -1 --date=short --pretty=format:'%cd <b>From</b> %cr'", True
        )
        last_commit = last_commit[0]
    else:
        last_commit = "No UPSTREAM_REPO"
    bot_cache["commit"] = last_commit

    if bot_cache["eng_versions"]["mega"] in ["Timeout", "N/A"] or bot_cache[
        "eng_versions"
    ]["mega"].startswith("Exception"):
        bot_loop.create_task(retry_mega_version())

    LOGGER.info("Fetched Package Versions!")
