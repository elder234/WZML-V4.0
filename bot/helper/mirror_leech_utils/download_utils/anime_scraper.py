from asyncio import sleep
from logging import getLogger
from re import finditer, search, sub

from aiohttp import ClientSession
from curl_cffi import Session as CurlSession

from ....core.config_manager import Config
from ...ext_utils.bot_utils import sync_to_async

_LOGGER = getLogger(__name__)

ANIWATCH_BASE = "https://aniwatch.co.at"
MEGACLOUD_EMBED = "https://megacloud.blog"
MEGACLOUD_API = "https://megacloud.tv"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_AJAX_HEADERS = {
    **_HEADERS,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": ANIWATCH_BASE,
}

_SERVER_PRIORITY = ["megacloud", "vidstreaming", "vidcloud"]


class AnimeSearchResult:
    __slots__ = ("title", "slug", "poster", "sub", "dub", "total_eps", "anime_id")

    def __init__(self, title, slug, poster, sub, dub, total_eps, anime_id):
        self.title = title
        self.slug = slug
        self.poster = poster
        self.sub = sub
        self.dub = dub
        self.total_eps = total_eps
        self.anime_id = anime_id

    def __str__(self):
        flags = []
        if self.sub:
            flags.append("Sub")
        if self.dub:
            flags.append("Dub")
        return f"{self.title} ({', '.join(flags)}) — {self.total_eps} eps"


class AnimeEpisode:
    __slots__ = ("ep_id", "number", "title", "filler")

    def __init__(self, ep_id, number, title, filler=False):
        self.ep_id = ep_id
        self.number = number
        self.title = title
        self.filler = filler


class EpisodeSource:
    __slots__ = ("url", "headers", "subtitles", "intro_skip", "outro_skip")

    def __init__(self, url, headers=None, subtitles=None, intro_skip=None, outro_skip=None):
        self.url = url
        self.headers = headers or {}
        self.subtitles = subtitles or []
        self.intro_skip = intro_skip
        self.outro_skip = outro_skip


class SessionManager:
    """HTTP session with auto-escalating Cloudflare bypass."""

    def __init__(self):
        self._curl: CurlSession | None = None

    def _get_curl(self):
        if self._curl is None:
            self._curl = CurlSession(impersonate="chrome124")
        return self._curl

    async def fetch(self, url, headers=None, as_json=False):
        hdrs = headers or _HEADERS
        try:
            session = self._get_curl()
            resp = await sync_to_async(session.get, url, headers=hdrs, timeout=15)
            if resp.status_code == 200:
                return resp.json() if as_json else resp.text
        except Exception as e:
            _LOGGER.warning("curl_cffi failed for %s: %s", url, e)

        try:
            async with ClientSession() as s:
                async with s.get(url, headers=hdrs, timeout=15) as resp:
                    if resp.status == 200:
                        return await resp.json() if as_json else await resp.text()
        except Exception as e:
            _LOGGER.warning("aiohttp fallback failed for %s: %s", url, e)

        return None

    async def close(self):
        if self._curl:
            await sync_to_async(self._curl.close)
            self._curl = None


class AniWatchScraper:
    """Scraper for aniwatch.co.at with Cloudflare bypass."""

    def __init__(self):
        self._sm = SessionManager()
        self._base = Config.ANIWATCH_BASE or ANIWATCH_BASE

    async def search(self, query, page=1):
        url = f"{self._base}/search?keyword={query}&page={page}"
        html = await self._sm.fetch(url, headers=_AJAX_HEADERS)
        if not html:
            return []

        results = []
        card_pattern = (
            r'<div class="film-detail".*?'
            r'<h2 class="film-name">\s*<a href="/([^"]+)"[^>]*>([^<]+)</a>'
        )
        for match in finditer(card_pattern, html, 16):
            slug = match.group(1)
            title = match.group(2).strip()
            region = html[max(0, match.start() - 1000):match.end() + 1000]
            sub = bool(search(r'class="tick-item tick-sub"', region))
            dub = bool(search(r'class="tick-item tick-dub"', region))
            eps_match = search(r'class="tick-item tick-eps"[^>]*>(\d+)', region)
            total_eps = int(eps_match.group(1)) if eps_match else 0
            poster_match = search(r'data-src="([^"]+)"', region)
            poster = poster_match.group(1) if poster_match else ""
            id_match = search(r'-(\d+)$', slug)
            anime_id = id_match.group(1) if id_match else slug
            results.append(AnimeSearchResult(title, slug, poster, sub, dub, total_eps, anime_id))
            if len(results) >= 10:
                break

        return results

    async def get_episodes(self, playlist_id):
        url = f"{self._base}/ajax/v2/episode/list/{playlist_id}"
        data = await self._sm.fetch(url, headers=_AJAX_HEADERS, as_json=True)
        if not data or "html" not in data:
            return []

        html = data["html"]
        episodes = []
        for match in finditer(r'data-id="(\d+)"[^>]*>([^<]*)<', html):
            ep_id = match.group(1)
            ep_text = match.group(2).strip()
            ep_num_match = search(r'EP\s*(\d+)', ep_text, 2)
            number = int(ep_num_match.group(1)) if ep_num_match else 0
            filler = "filler" in ep_text.lower()
            episodes.append(AnimeEpisode(ep_id, number, ep_text, filler))

        episodes.sort(key=lambda e: e.number)
        return episodes

    async def get_servers(self, episode_id):
        url = f"{self._base}/ajax/v2/episode/servers?episodeId={episode_id}"
        data = await self._sm.fetch(url, headers=_AJAX_HEADERS, as_json=True)
        if not data or "html" not in data:
            return []

        html = data["html"]
        servers = []
        for match in finditer(r'data-type="(\w+)"[^>]*data-id="(\d+)"', html):
            servers.append({"category": match.group(1), "server_id": match.group(2)})

        return servers

    async def get_streaming_source(self, server_id):
        url = f"{self._base}/ajax/v2/episode/sources?id={server_id}"
        data = await self._sm.fetch(url, headers=_AJAX_HEADERS, as_json=True)
        if not data or "link" not in data:
            return None

        embed_url = data["link"]
        return await self._extract_megacloud(embed_url)

    async def _extract_megacloud(self, embed_url):
        embed_url = sub(r"megacloud\.blog", "megacloud.tv", embed_url)

        html = await self._sm.fetch(embed_url, headers={
            **_HEADERS,
            "Referer": f"{self._base}/",
            "Origin": self._base,
        })
        if not html:
            return None

        source_id = ""
        id_match = search(r'/getSources\?id=([a-zA-Z0-9]+)', html)
        if id_match:
            source_id = id_match.group(1)
        else:
            id_match = search(r'data-id="([^"]+)"', html)
            if id_match:
                source_id = id_match.group(1)

        if not source_id:
            _LOGGER.error("Could not extract source ID from megacloud embed")
            return None

        client_key = self._extract_client_key(html)
        if not client_key:
            _LOGGER.error("Could not extract client key from megacloud embed")
            return None

        api_url = f"{MEGACLOUD_API}/embed-2/v3/e-1/getSources?id={source_id}&_k={client_key}"
        source_data = await self._sm.fetch(api_url, headers={
            **_HEADERS,
            "Referer": embed_url,
            "Origin": MEGACLOUD_EMBED,
        }, as_json=True)

        if not source_data or "sources" not in source_data:
            return None

        m3u8_url = source_data["sources"][0]["file"] if source_data["sources"] else None
        if not m3u8_url:
            return None

        subtitles = []
        for track in source_data.get("tracks", []):
            if track.get("kind") == "captions":
                subtitles.append({
                    "url": track["file"],
                    "label": track.get("label", "English"),
                    "lang": track.get("srclang", "en"),
                })

        intro = source_data.get("intro", {})
        outro = source_data.get("outro", {})
        intro_skip = (intro.get("end", 0) - intro.get("start", 0)) if intro else None
        outro_skip = (outro.get("end", 0) - outro.get("start", 0)) if outro else None

        return EpisodeSource(
            url=m3u8_url,
            headers={
                "Referer": f"{MEGACLOUD_EMBED}/",
                "User-Agent": _HEADERS["User-Agent"],
            },
            subtitles=subtitles,
            intro_skip=intro_skip,
            outro_skip=outro_skip,
        )

    def _extract_client_key(self, html):
        match = search(r'(?:CLIENT_KEY|_k)\s*[:=]\s*["\']([a-zA-Z0-9]{48})["\']', html)
        if match:
            return match.group(1)

        match = search(
            r'x\s*:\s*["\']([a-zA-Z0-9]{16})["\']'
            r'.*?y\s*:\s*["\']([a-zA-Z0-9]{16})["\']'
            r'.*?z\s*:\s*["\']([a-zA-Z0-9]{16})["\']',
            html,
        )
        if match:
            return match.group(1) + match.group(2) + match.group(3)

        match = search(r'([a-zA-Z0-9]{48})', html)
        return match.group(1) if match else None

    async def get_episode_source(self, episode_id, category="sub"):
        servers = await self.get_servers(episode_id)
        if not servers:
            return None

        category_servers = [s for s in servers if s["category"] == category]
        if not category_servers:
            category_servers = servers

        for priority_name in _SERVER_PRIORITY:
            for server in category_servers:
                if priority_name in server.get("server_id", ""):
                    source = await self.get_streaming_source(server["server_id"])
                    if source:
                        return source
                    await sleep(0.5)

        for server in category_servers:
            source = await self.get_streaming_source(server["server_id"])
            if source:
                return source
            await sleep(0.5)

        return None

    async def close(self):
        await self._sm.close()
