from asyncio import sleep
from base64 import b64decode
from json import loads as _json_loads
from logging import getLogger
from re import finditer, search, sub

from aiohttp import ClientSession
from curl_cffi import Session as CurlSession

from ....core.config_manager import Config
from ...ext_utils.bot_utils import sync_to_async

_LOGGER = getLogger(__name__)

_ANILIST_GRAPHQL = "https://graphql.anilist.co"
_TMDB_BASE = "https://api.themoviedb.org/3"
_TMDB_IMG = "https://image.tmdb.org/t/p"

ANIWATCH_BASE = "https://aniwatch.co.at"
MEGACLOUD_EMBED = "https://embed.megastatics.com"
MEGACLOUD_API = "https://megacloud.tv"
MEGAPLAY_API = "https://megaplay.buzz"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_REST_HEADERS = {
    **_HEADERS,
    "Referer": ANIWATCH_BASE,
    "Cache-Control": "no-cache",
}

_SERVER_PRIORITY = ["megaplay", "1anime", "vidsrc", "megacloud", "vidstreaming", "vidcloud"]


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
    __slots__ = (
        "url", "headers", "subtitles", "intro_skip", "outro_skip",
        "resolution", "bandwidth", "frame_rate",
    )

    def __init__(
        self, url, headers=None, subtitles=None, intro_skip=None, outro_skip=None,
        resolution="", bandwidth=0, frame_rate="",
    ):
        self.url = url
        self.headers = headers or {}
        self.subtitles = subtitles or []
        self.intro_skip = intro_skip
        self.outro_skip = outro_skip
        self.resolution = resolution
        self.bandwidth = bandwidth
        self.frame_rate = frame_rate

    async def parse_master_playlist(self, sm):
        """Parse master.m3u8 to extract resolution variants."""
        resp = await sm.fetch(
            self.url,
            headers={
                **self.headers,
                "Accept": "*/*",
            },
        )
        if not resp:
            return

        lines = resp.strip().splitlines()
        best = None
        variants = []
        for i, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF:"):
                continue
            info = line[len("#EXT-X-STREAM-INF:"):]
            attrs = {}
            for part in info.split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    attrs[k.strip()] = v.strip()

            res = attrs.get("RESOLUTION", "")
            bw = int(attrs.get("BANDWIDTH", 0))
            fps = attrs.get("FRAME-RATE", "")
            codecs = attrs.get("CODECS", "")

            if i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                variant_url = lines[i + 1].strip()
            else:
                continue

            variant = {
                "url": variant_url,
                "resolution": res,
                "bandwidth": bw,
                "frame_rate": fps,
                "codecs": codecs,
            }
            variants.append(variant)
            if best is None or bw > best["bandwidth"]:
                best = variant

        if variants:
            _LOGGER.info(
                "m3u8 variants: %d (best=%s, %d bps)",
                len(variants),
                best["resolution"],
                best["bandwidth"],
            )

        if best:
            self.resolution = best["resolution"]
            self.bandwidth = best["bandwidth"]
            self.frame_rate = best["frame_rate"]
            base = self.url.rsplit("/", 1)[0] + "/"
            self.url = base + best["url"]


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
    """Scraper for aniwatch.co.at WordPress REST API."""

    def __init__(self):
        self._sm = SessionManager()
        self._base = Config.ANIWATCH_BASE or ANIWATCH_BASE

    async def search(self, query):
        url = f"{self._base}/wp-json/hianime/v1/search/suggestions?keyword={query}"
        data = await self._sm.fetch(url, headers=_REST_HEADERS, as_json=True)
        if not data or not data.get("success") or not data.get("html"):
            return []

        html = data["html"]
        results = []

        for match in finditer(
            r'<a\s+href="([^"]*?/anime/([^/]+)/)"[^>]*>.*?'
            r'<h3[^>]*class="[^"]*dynamic-name[^"]*"[^>]*>([^<]+)</h3>',
            html,
            16,
        ):
            anime_url = match.group(1)
            slug = match.group(2)
            title = match.group(3).strip()

            region = html[max(0, match.start() - 200) : match.end() + 500]
            poster_match = search(r'data-src="([^"]+)"', region)
            poster = poster_match.group(1) if poster_match else ""

            sub = bool(search(r'tick-sub', region))
            dub = bool(search(r'tick-dub', region))
            eps_match = search(r'tick-eps[^>]*>(\d+)', region)
            total_eps = int(eps_match.group(1)) if eps_match else 0

            results.append(
                AnimeSearchResult(title, slug, poster, sub, dub, total_eps, slug)
            )
            if len(results) >= 10:
                break

        if not results:
            for match in finditer(
                r'href="[^"]*?/anime/([^/]+)/"[^>]*>\s*'
                r'<div[^>]*>.*?</div>\s*'
                r'<div[^>]*>\s*<h3[^>]*>([^<]+)</h3>',
                html,
                16,
            ):
                slug = match.group(1)
                title = match.group(2).strip()
                results.append(
                    AnimeSearchResult(title, slug, "", True, True, 0, slug)
                )
                if len(results) >= 10:
                    break

        return results

    async def get_anime_details(self, slug):
        url = f"{self._base}/anime/{slug}/"
        html = await self._sm.fetch(url, headers=_HEADERS)
        if not html:
            return None

        anime_id = None
        id_match = search(r'data-animeid="(\d+)"', html)
        if id_match:
            anime_id = id_match.group(1)
        else:
            id_match = search(r'data-anime-id="(\d+)"', html)
            if id_match:
                anime_id = id_match.group(1)

        sub = bool(search(r'tick-sub', html))
        dub = bool(search(r'tick-dub', html))
        eps_match = search(r'tick-eps[^>]*>(\d+)', html)
        total_eps = int(eps_match.group(1)) if eps_match else 0

        return {
            "anime_id": anime_id,
            "sub": sub,
            "dub": dub,
            "total_eps": total_eps,
        }

    async def get_episodes(self, anime_id):
        url = f"{self._base}/wp-json/hianime/v1/episode/list/{anime_id}"
        data = await self._sm.fetch(url, headers=_REST_HEADERS, as_json=True)
        if not data or not data.get("status") or not data.get("html"):
            _LOGGER.warning("Episode list API returned no data for %s: %s", anime_id, data)
            return []

        html = data["html"]
        episodes = []

        for match in finditer(r'<a\s[^>]*?class="[^"]*ep-item[^"]*"[^>]*>', html):
            tag = match.group(0)
            id_match = search(r'data-id="(\d+)"', tag)
            num_match = search(r'data-number="(\d+)"', tag)
            title_match = search(r'title="([^"]+)"', tag)
            if id_match and num_match:
                ep_id = id_match.group(1)
                number = int(num_match.group(1))
                ep_text = title_match.group(1).strip() if title_match else f"EP{number:02d}"
                filler = "filler" in ep_text.lower()
                episodes.append(AnimeEpisode(ep_id, number, ep_text, filler))

        if not episodes:
            for match in finditer(r'data-id="(\d+)"', html):
                ep_id = match.group(1)
                num_match = search(r'data-number="(\d+)"', html[match.start():match.end() + 200])
                number = int(num_match.group(1)) if num_match else 0
                if number:
                    episodes.append(AnimeEpisode(ep_id, number, f"EP{number:02d}"))

        episodes.sort(key=lambda e: e.number)
        _LOGGER.info("Found %d episodes for anime %s", len(episodes), anime_id)
        return episodes

    async def get_servers(self, episode_id):
        url = f"{self._base}/wp-json/hianime/v1/episode/servers/{episode_id}"
        data = await self._sm.fetch(url, headers=_REST_HEADERS, as_json=True)
        if not data or not data.get("status") or not data.get("html"):
            _LOGGER.warning("Episode servers API returned no data for %s: %s", episode_id, data)
            return []

        html = data["html"]
        servers = []

        for match in finditer(
            r'data-type="(\w+)"[^>]*data-server-name="([^"]+)"[^>]*data-hash="([^"]+)"', html
        ):
            category = match.group(1)
            server_name = match.group(2)
            embed_hash = match.group(3)
            try:
                embed_url = b64decode(embed_hash).decode("utf-8")
            except Exception:
                continue
                servers.append(
                    {"category": category, "name": server_name, "embed_url": embed_url}
                )

        if not servers:
            for match in finditer(
                r'data-server-name="([^"]+)"[^>]*data-hash="([^"]+)"', html
            ):
                server_name = match.group(1)
                embed_hash = match.group(2)
                try:
                    embed_url = b64decode(embed_hash).decode("utf-8")
                except Exception:
                    continue
                servers.append(
                    {"category": "sub", "name": server_name, "embed_url": embed_url}
                )

        for s in servers:
            _LOGGER.info(
                "Server: name=%s category=%s embed_url=%s",
                s["name"], s["category"], s["embed_url"][:120],
            )
        _LOGGER.info("Found %d servers for episode %s", len(servers), episode_id)
        return servers

    async def get_streaming_source(self, embed_url):
        _LOGGER.info("get_streaming_source: embed_url=%s", embed_url[:200] if embed_url else "None")
        if "megaplay" in embed_url:
            return await self._extract_megaplay(embed_url)
        if "megaflix" in embed_url:
            return await self._extract_megaflix(embed_url)
        if "1anime.site" in embed_url:
            return await self._extract_1anime(embed_url)
        _LOGGER.warning("No known embed pattern matched, falling back to megacloud for: %s", embed_url[:200])
        return await self._extract_megacloud(embed_url)

    async def _extract_megaplay(self, embed_url):
        ep_id_match = search(r'/s-2/(\d+)', embed_url)
        if not ep_id_match:
            _LOGGER.error("Could not extract episode ID from megaplay URL: %s", embed_url)
            return None

        ep_id = ep_id_match.group(1)
        api_url = f"{MEGAPLAY_API}/stream/getSources?id={ep_id}"

        source_data = await self._sm.fetch(
            api_url,
            headers={
                **_HEADERS,
                "Referer": f"{embed_url}",
                "Origin": MEGAPLAY_API,
                "X-Requested-With": "XMLHttpRequest",
            },
            as_json=True,
        )

        if not source_data or "sources" not in source_data:
            _LOGGER.error("Megaplay API returned no sources for %s: %s", ep_id, source_data)
            return None

        sources = source_data["sources"]
        m3u8_url = sources["file"] if isinstance(sources, dict) else sources[0]["file"]
        if not m3u8_url:
            return None

        subtitles = []
        for track in source_data.get("tracks", []):
            if track.get("kind") == "captions":
                subtitles.append(
                    {
                        "url": track["file"],
                        "label": track.get("label", "English"),
                        "lang": track.get("srclang", "en"),
                    }
                )

        intro = source_data.get("intro", {})
        outro = source_data.get("outro", {})
        intro_skip = (intro.get("end", 0) - intro.get("start", 0)) if intro else None
        outro_skip = (outro.get("end", 0) - outro.get("start", 0)) if outro else None

        _LOGGER.info("Megaplay source resolved: %s", m3u8_url[:80])

        source = EpisodeSource(
            url=m3u8_url,
            headers={
                "Referer": f"{MEGAPLAY_API}/",
                "User-Agent": _HEADERS["User-Agent"],
            },
            subtitles=subtitles,
            intro_skip=intro_skip,
            outro_skip=outro_skip,
        )
        await source.parse_master_playlist(self._sm)
        return source

    async def _extract_megaflix(self, embed_url):
        html = await self._sm.fetch(
            embed_url,
            headers={
                **_HEADERS,
                "Referer": f"{ANIWATCH_BASE}/",
                "Origin": ANIWATCH_BASE,
            },
        )
        if not html:
            return None

        payload_match = search(
            r'<script[^>]*id="player-payload"[^>]*>\s*({[^<]+})\s*</script>',
            html,
            16,
        )
        if not payload_match:
            payload_match = search(
                r'"sourceUrl"\s*:\s*"([^"]+)"',
                html,
            )
            if payload_match:
                source_url = payload_match.group(1)
            else:
                _LOGGER.error("Could not find source URL in megaflix embed")
                return None
        else:
            try:
                payload = _json_loads(payload_match.group(1))
                source_url = payload.get("sourceUrl", "")
            except Exception:
                _LOGGER.error("Could not parse megaflix player payload JSON")
                return None

        if not source_url:
            _LOGGER.error("Empty sourceUrl in megaflix embed")
            return None

        base = f"https://megaflix.buzz"
        source_api = source_url if source_url.startswith("http") else base + source_url

        source_data = await self._sm.fetch(
            source_api,
            headers={
                **_HEADERS,
                "Referer": f"{embed_url}",
                "Origin": base,
                "Accept": "application/json",
            },
            as_json=True,
        )

        if not source_data or not source_data.get("source"):
            _LOGGER.error("Megaflix source API returned no data: %s", source_data)
            return None

        m3u8_url = source_data["source"]
        if not m3u8_url:
            return None

        subtitles = []
        for track in source_data.get("tracks", []):
            if track.get("kind") == "captions":
                subtitles.append(
                    {
                        "url": track["file"],
                        "label": track.get("label", "English"),
                        "lang": track.get("srclang", "en"),
                    }
                )

        _LOGGER.info("Megaflix source resolved: %s", m3u8_url[:80])

        source = EpisodeSource(
            url=m3u8_url,
            headers={
                "Referer": f"{base}/",
                "User-Agent": _HEADERS["User-Agent"],
            },
            subtitles=subtitles,
        )
        await source.parse_master_playlist(self._sm)
        return source

    async def _extract_1anime(self, embed_url):
        """Extract direct MP4 URL from my.1anime.site embed pages."""
        html = await self._sm.fetch(
            embed_url,
            headers={
                **_HEADERS,
                "Referer": f"{ANIWATCH_BASE}/",
                "Origin": ANIWATCH_BASE,
            },
        )
        if not html:
            _LOGGER.error("Failed to fetch 1anime embed page: %s", embed_url)
            return None

        source_match = search(r'<source\s+src="([^"]+)"', html)
        if not source_match:
            token_match = search(r'VIDEO_TOKEN\s*=\s*"([^"]+)"', html)
            if token_match:
                token = token_match.group(1)
                video_url = f"https://my.1anime.site/stream/{token}"
            else:
                _LOGGER.error("Could not extract video URL from 1anime embed: %s", embed_url)
                return None
        else:
            video_url = source_match.group(1)
            if video_url.startswith("//"):
                video_url = "https:" + video_url

        _LOGGER.info("1anime source resolved: %s", video_url[:120])

        source = EpisodeSource(
            url=video_url,
            headers={
                "Referer": "https://my.1anime.site/",
                "User-Agent": _HEADERS["User-Agent"],
            },
        )
        return source

    async def _extract_megacloud(self, embed_url):
        embed_url = sub(r"megacloud\.blog|megacloud\.tv", "embed.megastatics.com", embed_url)

        html = await self._sm.fetch(
            embed_url,
            headers={
                **_HEADERS,
                "Referer": f"{self._base}/",
                "Origin": self._base,
            },
        )
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
        source_data = await self._sm.fetch(
            api_url,
            headers={
                **_HEADERS,
                "Referer": embed_url,
                "Origin": MEGACLOUD_EMBED,
            },
            as_json=True,
        )

        if not source_data or "sources" not in source_data:
            return None

        m3u8_url = source_data["sources"][0]["file"] if source_data["sources"] else None
        if not m3u8_url:
            return None

        subtitles = []
        for track in source_data.get("tracks", []):
            if track.get("kind") == "captions":
                subtitles.append(
                    {
                        "url": track["file"],
                        "label": track.get("label", "English"),
                        "lang": track.get("srclang", "en"),
                    }
                )

        intro = source_data.get("intro", {})
        outro = source_data.get("outro", {})
        intro_skip = (intro.get("end", 0) - intro.get("start", 0)) if intro else None
        outro_skip = (outro.get("end", 0) - outro.get("start", 0)) if outro else None

        source = EpisodeSource(
            url=m3u8_url,
            headers={
                "Referer": f"{MEGACLOUD_EMBED}/",
                "User-Agent": _HEADERS["User-Agent"],
            },
            subtitles=subtitles,
            intro_skip=intro_skip,
            outro_skip=outro_skip,
        )
        await source.parse_master_playlist(self._sm)
        return source

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
                if priority_name in server.get("name", "").lower():
                    source = await self.get_streaming_source(server["embed_url"])
                    if source:
                        return source
                    await sleep(0.5)

        for server in category_servers:
            source = await self.get_streaming_source(server["embed_url"])
            if source:
                return source
            await sleep(0.5)

        return None

    async def close(self):
        await self._sm.close()


ANIMETOKI_BASE = "https://animetoki.com"
ANIMETOKI_WORKERS = "https://ongoing-at.25002.workers.dev"


class AnimeTokiScraper:
    """Scraper for animetoki.com — self-hosted CF Workers."""

    def __init__(self):
        self._sm = SessionManager()

    async def search(self, query):
        url = f"{ANIMETOKI_BASE}/?s={query}"
        html = await self._sm.fetch(url, headers=_HEADERS)
        if not html:
            return []

        results = []
        for match in finditer(
            r'<h2[^>]*class="post-title[^"]*"[^>]*>\s*<a\s+href="([^"]+)"[^>]*>([^<]+)</a>',
            html,
        ):
            href = match.group(1)
            title = match.group(2).strip()
            if "/episode/" in href or not title:
                continue
            slug = href.rstrip("/").split("/")[-1]
            results.append(
                AnimeSearchResult(title, slug, "", True, True, 0, slug)
            )
            if len(results) >= 10:
                break

        if not results:
            for match in finditer(
                r'<a\s+href="([^"]*(?:animetoki\.com/[^/]+(?:/[^/]+)?))"[^>]*>'
                r'([^<]{3,80})</a>',
                html,
            ):
                href = match.group(1)
                title = match.group(2).strip()
                if "/episode/" in href or not title:
                    continue
                slug = href.rstrip("/").split("/")[-1]
                if slug in ("", "tag", "category", "page"):
                    continue
                results.append(
                    AnimeSearchResult(title, slug, "", True, True, 0, slug)
                )
                if len(results) >= 10:
                    break

        return results

    async def get_episodes(self, slug):
        url = f"{ANIMETOKI_BASE}/{slug}/"
        html = await self._sm.fetch(url, headers=_HEADERS)
        if not html:
            return []

        episodes = []
        for match in finditer(
            r'<a[^>]*href="([^"]*?/episode/([^"]+))"[^>]*>\s*'
            r'(?:<span[^>]*>)?Ep(?:isode)?\s*(\d+)',
            html,
        ):
            ep_url = match.group(1)
            ep_slug = match.group(2)
            number = int(match.group(3))
            episodes.append(
                AnimeEpisode(
                    ep_id=ep_url,
                    number=number,
                    title=ep_slug,
                )
            )

        if not episodes:
            for match in finditer(
                r'href="([^"]*?/episode/([^"]*?episode[- ](\d+)[^"]*))"',
                html,
            ):
                ep_url = match.group(1)
                ep_slug = match.group(2)
                number = int(match.group(3))
                episodes.append(
                    AnimeEpisode(ep_id=ep_url, number=number, title=ep_slug)
                )

        if not episodes:
            title_from_slug = slug.replace("-", " ").replace("download all seasons", "").strip()
            search_url = f"{ANIMETOKI_BASE}/?s={title_from_slug}+english"
            _LOGGER.info("No episodes on page, searching: %s", search_url)
            search_html = await self._sm.fetch(search_url, headers=_HEADERS)
            if search_html:
                for match in finditer(
                    r'<a\s+href="([^"]+)"[^>]*>\s*'
                    r'<h2[^>]*class="post-title[^"]*"[^>]*>\s*([^<]+)</h2>',
                    search_html,
                    16,
                ):
                    href = match.group(1)
                    title = match.group(2).strip()
                    if "/episode/" not in href:
                        continue
                    ep_match = search(r'episode[- ](\d+)', href, 1)
                    if not ep_match:
                        ep_match = search(r'Episode\s+(\d+)', title, 1)
                    if ep_match:
                        number = int(ep_match.group(1))
                        full_url = href if href.startswith("http") else f"{ANIMETOKI_BASE}/{href.lstrip('/')}"
                        episodes.append(
                            AnimeEpisode(ep_id=full_url, number=number, title=title)
                        )

                if not episodes:
                    for match in finditer(
                        r'href="([^"]*episode[^"]*)"[^>]*>\s*([^<]+)</a>',
                        search_html,
                        16,
                    ):
                        href = match.group(1)
                        title = match.group(2).strip()
                        if "/episode/" not in href:
                            continue
                        ep_match = search(r'episode[- ](\d+)', href, 1)
                        if not ep_match:
                            ep_match = search(r'Episode\s+(\d+)', title, 1)
                        if ep_match:
                            number = int(ep_match.group(1))
                            full_url = href if href.startswith("http") else f"{ANIMETOKI_BASE}/{href.lstrip('/')}"
                            episodes.append(
                                AnimeEpisode(ep_id=full_url, number=number, title=title)
                            )

        seen = set()
        unique = []
        for ep in episodes:
            if ep.number not in seen:
                seen.add(ep.number)
                unique.append(ep)
        episodes = unique

        episodes.sort(key=lambda e: e.number)
        _LOGGER.info("Found %d episodes for animetoki %s", len(episodes), slug)
        return episodes

    async def get_source(self, episode_url):
        if not episode_url.startswith("http"):
            episode_url = f"{ANIMETOKI_BASE}/{episode_url.lstrip('/')}"

        html = await self._sm.fetch(episode_url, headers=_HEADERS)
        if not html:
            return None

        video_match = search(
            r'<video[^>]*>.*?'
            r'<source\s+src="([^"]+)"',
            html,
            16,
        )
        if not video_match:
            video_match = search(
                r'<source\s+src="(//[^"]+\.(?:mkv|mp4|webm)[^"]*)"',
                html,
            )
        if not video_match:
            video_match = search(
                r'<source\s+src="(https?://[^"]+\.(?:mkv|mp4|webm)[^"]*)"',
                html,
            )
        if not video_match:
            video_match = search(
                r'"(https?://[^"]+\.(?:mkv|mp4|webm))"',
                html,
            )
        if not video_match:
            _LOGGER.error("No video source found on %s", episode_url)
            return None

        video_url = video_match.group(1)
        if video_url.startswith("//"):
            video_url = "https:" + video_url

        sub_match = search(
            r'<track[^>]*src="([^"]+\.vtt[^"]*)"',
            html,
        )
        subtitles = []
        if sub_match:
            sub_url = sub_match.group(1)
            if sub_url.startswith("//"):
                sub_url = "https:" + sub_url
            subtitles.append({
                "url": sub_url,
                "label": "English",
                "lang": "en",
            })

        _LOGGER.info("Animetoki source resolved: %s", video_url[:100])

        return EpisodeSource(
            url=video_url,
            headers={"Referer": f"{ANIMETOKI_BASE}/"},
            subtitles=subtitles,
            resolution="1920x1080",
        )

    async def close(self):
        await self._sm.close()


async def anilist_search(query):
    """Search AniList for anime by title. Returns dict of first match."""
    gql = """
    query ($search: String) {
        Media(search: $search, type: ANIME) {
            id
            title { romaji english }
            season
            seasonYear
            episodes
            status
        }
    }
    """
    async with ClientSession() as s:
        try:
            async with s.post(
                _ANILIST_GRAPHQL,
                json={"query": gql, "variables": {"search": query}},
                headers={"Content-Type": "application/json"},
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return {}
                result = await resp.json()
                media = result.get("data", {}).get("Media")
                if not media:
                    return {}
                title = media["title"]
                return {
                    "id": media["id"],
                    "title": title.get("english") or title.get("romaji", ""),
                    "romaji": title.get("romaji", ""),
                    "season": media.get("season", ""),
                    "season_year": media.get("seasonYear", 0),
                    "total_episodes": media.get("episodes", 0),
                    "status": media.get("status", ""),
                }
        except Exception as e:
            _LOGGER.warning("AniList query failed: %s", e)
            return {}


async def anilist_search_multi(query):
    """Search AniList for multiple anime matches."""
    gql = """
    query ($search: String) {
        Page(perPage: 5) {
            media(search: $search, type: ANIME) {
                id
                title { romaji english }
                season
                seasonYear
                episodes
            }
        }
    }
    """
    async with ClientSession() as s:
        try:
            async with s.post(
                _ANILIST_GRAPHQL,
                json={"query": gql, "variables": {"search": query}},
                headers={"Content-Type": "application/json"},
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return []
                result = await resp.json()
                return result.get("data", {}).get("Page", {}).get("media", [])
        except Exception as e:
            _LOGGER.warning("AniList multi search failed: %s", e)
            return []


async def anilist_episode_info(anilist_id, episode):
    """Get episode title from AniList by anime ID and episode number."""
    gql = """
    query ($id: Int, $episode: Int) {
        Media(id: $id, type: ANIME) {
            title { english romaji }
            episodes
            streamingEpisodes {
                title
                thumbnail
                url
            }
        }
    }
    """
    async with ClientSession() as s:
        try:
            async with s.post(
                _ANILIST_GRAPHQL,
                json={"query": gql, "variables": {"id": anilist_id, "episode": episode}},
                headers={"Content-Type": "application/json"},
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return {}
                result = await resp.json()
                media = result.get("data", {}).get("Media", {})
                title = media.get("title", {})
                ep_title = ""
                eps = media.get("streamingEpisodes", [])
                for ep in eps:
                    t = ep.get("title", "")
                    if f"Episode {episode}" in t or f"Ep {episode}" in t:
                        ep_title = t.split("-")[-1].strip() if "-" in t else t
                        break
                return {
                    "title": title.get("english") or title.get("romaji", ""),
                    "romaji": title.get("romaji", ""),
                    "total_episodes": media.get("episodes", 0),
                    "episode_title": ep_title,
                }
        except Exception as e:
            _LOGGER.warning("AniList episode info failed: %s", e)
            return {}


async def tmdb_search_tv(query):
    """Search TMDb for TV shows. Returns first match or None."""
    api_key = Config.TMDB_API_KEY
    if not api_key:
        return None
    async with ClientSession() as s:
        try:
            async with s.get(
                f"{_TMDB_BASE}/search/tv",
                params={"api_key": api_key, "query": query, "language": "en-US"},
                headers={"Accept": "application/json"},
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    return None
                best = results[0]
                return {
                    "id": best["id"],
                    "title": best.get("name", ""),
                    "year": (best.get("first_air_date") or "")[:4],
                    "total_seasons": best.get("number_of_seasons", 0),
                    "total_episodes": best.get("number_of_episodes", 0),
                }
        except Exception as e:
            _LOGGER.warning("TMDb search failed: %s", e)
            return None


async def tmdb_search_movie(query):
    """Search TMDb for movies. Returns first match or None."""
    api_key = Config.TMDB_API_KEY
    if not api_key:
        return None
    async with ClientSession() as s:
        try:
            async with s.get(
                f"{_TMDB_BASE}/search/movie",
                params={"api_key": api_key, "query": query, "language": "en-US"},
                headers={"Accept": "application/json"},
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                results = data.get("results", [])
                if not results:
                    return None
                best = results[0]
                return {
                    "id": best["id"],
                    "title": best.get("title", ""),
                    "year": (best.get("release_date") or "")[:4],
                }
        except Exception as e:
            _LOGGER.warning("TMDb movie search failed: %s", e)
            return None


async def tmdb_episode_info(tmdb_id, season, episode):
    """Get episode title from TMDb."""
    api_key = Config.TMDB_API_KEY
    if not api_key:
        return {}
    async with ClientSession() as s:
        try:
            async with s.get(
                f"{_TMDB_BASE}/tv/{tmdb_id}/season/{season}/episode/{episode}",
                params={"api_key": api_key, "language": "en-US"},
                headers={"Accept": "application/json"},
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                return {
                    "episode_title": data.get("name", ""),
                    "air_date": data.get("air_date", ""),
                    "overview": data.get("overview", ""),
                }
        except Exception as e:
            _LOGGER.warning("TMDb episode info failed: %s", e)
            return {}


async def tmdb_season_info(tmdb_id, season):
    """Get season info from TMDb."""
    api_key = Config.TMDB_API_KEY
    if not api_key:
        return {}
    async with ClientSession() as s:
        try:
            async with s.get(
                f"{_TMDB_BASE}/tv/{tmdb_id}/season/{season}",
                params={"api_key": api_key, "language": "en-US"},
                headers={"Accept": "application/json"},
                timeout=10,
            ) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                return {
                    "season_title": data.get("name", ""),
                    "total_episodes": data.get("episodes", [])[-1].get("episode_number", 0) if data.get("episodes") else 0,
                }
        except Exception as e:
            _LOGGER.warning("TMDb season info failed: %s", e)
            return {}
