from asyncio import create_subprocess_exec
from json import loads
from logging import getLogger
from os import path as ospath
from re import IGNORECASE, compile, search

from aiofiles.os import rename as aiorename
from aioshutil import move as aiomove

_LOGGER = getLogger(__name__)

# Common resolution labels
_RES_MAP = {
    2160: "2160p",
    1440: "1440p",
    1080: "1080p",
    720: "720p",
    480: "480p",
    360: "360p",
}

# Scene/source tags
_SOURCE_TAGS = {
    "nf": "NF",
    "netflix": "NF",
    "amzn": "AMZN",
    "amazon": "AMZN",
    "dsnp": "DSNP",
    "disney": "DSNP",
    "hmax": "HMAX",
    "hbo": "HMAX",
    "hulu": "HULU",
    "atvp": "ATVP",
    "apple": "ATVP",
    "pmtp": "PMTP",
    "paramount": "PMTP",
    "stkr": "STKR",
    "stalker": "STKR",
    "web": "WEB-DL",
    "web-dl": "WEB-DL",
    "webrip": "WEBRip",
    "web-rip": "WEBRip",
    "bluray": "BluRay",
    "blu-ray": "BluRay",
    "bdrip": "BDRip",
    "brrip": "BRRip",
    "dvdrip": "DVDRip",
    "hdtv": "HDTV",
    "hdrip": "HDRip",
    "cam": "CAM",
    "ts": "TS",
    "tc": "TC",
}

# Audio tags
_AUDIO_TAGS = {
    "ddp5.1": "DDP5.1",
    "ddp": "DDP5.1",
    "dd+5.1": "DDP5.1",
    "dd+": "DDP",
    "dd5.1": "DD5.1",
    "dd": "DD",
    "aac": "AAC",
    "dts": "DTS",
    "dts-hd": "DTS-HD",
    "truehd": "TrueHD",
    "atmos": "Atmos",
    "opus": "OPUS",
    "flac": "FLAC",
    "mp3": "MP3",
    "ac3": "AC3",
}

# Codec tags
_CODEC_TAGS = {
    "h.265": "H.265",
    "h265": "H.265",
    "x265": "H.265",
    "hevc": "H.265",
    "h.264": "H.264",
    "h264": "H.264",
    "x264": "H.264",
    "avc": "H.264",
    "av1": "AV1",
    "vp9": "VP9",
}

# Image/overlay codecs to skip when scanning for video streams
_IMAGE_CODECS = {"png", "mjpeg", "bmp", "gif", "tiff", "webp", "apng"}

# Junk words to strip from titles
_JUNK = compile(
    r"\b(1080p|720p|480p|2160p|4k|hd|sd|web-dl|webrip|bluray|blu-ray|"
    r"bdrip|brrip|dvdrip|hdtv|hdrip|aac|dts|ddp5?\.1|dd\+?5?\.1|"
    r"atmos|truehd|flac|opus|ac3|mp3|"
    r"h\.?264|h\.?265|x264|x265|hevc|avc|av1|vp9|"
    r"hdr|hdr10|hdr10\+|dolby.?vision|dv|"
    r"enGLISH|hindi|eng|hin|jpn|jap|spa|fre|ger|ita|por|rus|chi|kor|"
    r"multi|dubbed|dub|subbed|sub|"
    r"nf|amzn|dsnp|hmax|hulu|atvp|pmtp|stkr|"
    r"web-dl|webrip|bluray|blu-ray|bdrip|brrip|dvdrip|hdtv|hdrip|"
    r"cam|ts|tc|"
    r"repack|proper|extended|unrated|directors.?cut|imax|"
    r"internal|limited|ntsc|pal|"
    r"10bit|10-bit|8bit|8-bit|"
    r"dual.?audio|dual.?audio|dual.?audio|"
    r"DDP?5?\.?1|DDP?|atmos|truehd|dts|flac|aac|"
    r"NF|AMZN|DSNP|HMAX|ATVP|HULU|PMTP|WEB-DL|WEBRip|BluRay|DVDRip|HDTV)",
    IGNORECASE,
)

# Filename number pattern (for natural sort)
_NUMPAT = compile(r"(\d+)")


def _natural_sort_key(s):
    """Natural sort key for filenames (handles episode numbers)."""
    return [
        int(c) if c.isdigit() else c.lower()
        for c in _NUMPAT.split(s)
    ]


def _match_tag(word, tag_map):
    """Look up a word in a tag map (case-insensitive)."""
    return tag_map.get(word.lower().strip("."), None)


class FileRenamer:
    """Parse, enrich with metadata, and rename media files."""

    def __init__(self, uploader="", template=""):
        self.uploader = uploader
        self.template = template or "{title}.{season}{episode}.{resolution}.{source}.{audio}.{codec}.{ext}"

    @staticmethod
    async def get_video_metadata(filepath):
        """Use ffprobe to get resolution, video codec, audio codec."""
        result = {"resolution": "", "codec": "", "audio": ""}
        try:
            proc = await create_subprocess_exec(
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                filepath,
                stdout=-1,
                stderr=-1,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return result
            data = loads(stdout.decode().strip())
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video" and not result["resolution"]:
                    codec = stream.get("codec_name", "")
                    if codec.lower() in _IMAGE_CODECS:
                        continue
                    height = int(stream.get("height", 0))
                    if height < 100:
                        continue
                    if height in _RES_MAP:
                        result["resolution"] = _RES_MAP[height]
                    elif height > 0:
                        closest = min(_RES_MAP.keys(), key=lambda r: abs(r - height))
                        if abs(closest - height) < 100:
                            result["resolution"] = _RES_MAP[closest]
                        else:
                            result["resolution"] = f"{height}p"
                    if codec:
                        result["codec"] = _match_tag(codec, _CODEC_TAGS) or codec.upper()
                elif stream.get("codec_type") == "audio" and not result["audio"]:
                    codec = stream.get("codec_name", "")
                    channels = stream.get("channels", 0)
                    if channels >= 6:
                        if codec in ("eac3", "ec-3"):
                            result["audio"] = "DDP5.1"
                        elif codec == "ac3":
                            result["audio"] = "DD5.1"
                        elif codec == "dts":
                            result["audio"] = "DTS"
                        else:
                            result["audio"] = codec.upper()
                    else:
                        result["audio"] = codec.upper() if codec else ""
        except Exception as e:
            _LOGGER.warning(f"ffprobe metadata failed: {e}")
        return result

    @staticmethod
    def parse_filename(filename):
        """Parse a media filename into components.

        Returns dict with keys: title, year, season, episode, source,
        language, audio, codec, group, ext
        """
        name, ext = ospath.splitext(filename)
        result = {
            "title": "",
            "year": "",
            "resolution": "",
            "season": "",
            "episode": "",
            "source": "",
            "language": "",
            "audio": "",
            "codec": "",
            "group": "",
            "ext": ext.lstrip("."),
        }

        # Extract resolution from original name before junk stripping
        res_match = search(r"(2160p|1440p|1080p|720p|480p|360p|4k)", name, IGNORECASE)
        if res_match:
            result["resolution"] = res_match.group(1).upper() if res_match.group(1).upper() != "4K" else "2160p"

        # Strip common tags from the name for cleaner title extraction
        clean = _JUNK.sub(" ", name)
        # Collapse multiple dots/spaces
        clean = clean.replace(".", " ").replace("_", " ")
        clean = " ".join(clean.split())

        # Try to extract year (4 digits between 1900-2099)
        year_match = search(r"\b(19\d{2}|20\d{2})\b", clean)
        if year_match:
            result["year"] = year_match.group(1)

        # Try to extract season/episode
        # Patterns: S01E05, S01E05E06, S01, E05, - 05, x05
        se_match = search(r"(?:S(\d{1,2}))?(?:E(\d{1,3}))", clean, IGNORECASE)
        if se_match:
            if se_match.group(1):
                result["season"] = f"S{int(se_match.group(1)):02d}"
            if se_match.group(2):
                result["episode"] = f"E{int(se_match.group(2)):02d}"
        else:
            # Try: - 05, EP05, EP.05
            ep_match = search(r"(?:[-\s]|\b)(?:EP\.?|Episode\s*)(\d{1,3})", clean, IGNORECASE)
            if ep_match:
                result["episode"] = f"E{int(ep_match.group(1)):02d}"
            else:
                # Try: standalone number (e.g. "06" in "Morfeusz 06 1080p")
                num_match = search(r"(?:[-.\s]|^)(\d{1,4})(?:[-.\s]|$)", clean)
                if num_match and 1 <= int(num_match.group(1)) <= 999:
                    result["episode"] = f"E{int(num_match.group(1)):02d}"

        # Extract source/audio/codec from original name before junk stripping
        orig_words = name.replace(".", " ").replace("_", " ").replace("-", " ").split()
        for word in orig_words:
            tag = _match_tag(word, _SOURCE_TAGS)
            if tag:
                result["source"] = tag
                break

        for word in orig_words:
            tag = _match_tag(word, _AUDIO_TAGS)
            if tag:
                result["audio"] = tag
                break

        for word in orig_words:
            tag = _match_tag(word, _CODEC_TAGS)
            if tag:
                result["codec"] = tag
                break

        # Extract the group (last tag after -)
        group_match = search(r"[-@]([A-Za-z0-9][\w\s.-]*?)$", name)
        if group_match:
            result["group"] = group_match.group(1).strip()

        # Extract title: everything before year or season/episode
        # Split on dots and take meaningful parts
        parts = name.split(".")
        title_parts = []
        for part in parts:
            # Stop at year
            if result["year"] and part == result["year"]:
                break
            # Stop at season
            if result["season"] and part.upper().startswith("S"):
                break
            # Stop at resolution-like patterns
            if search(r"^\d{3,4}p$", part):
                break
            # Stop at known tags
            if _match_tag(part, _SOURCE_TAGS) or _match_tag(part, _CODEC_TAGS):
                break
            title_parts.append(part)

        result["title"] = " ".join(title_parts).replace("_", " ").strip(" .-")

        return result

    def build_new_name(self, parsed, metadata=None, db_metadata=None):
        """Build new filename from parsed info and metadata.

        metadata dict from get_video_metadata: resolution, codec, audio
        db_metadata dict from AniList/TMDb: title, season, episode, episode_title
        """
        meta = metadata or {}
        db = db_metadata or {}
        # Use metadata as fallback for filename-parsed values
        resolution = parsed.get("resolution") or db.get("resolution") or meta.get("resolution", "")
        codec = parsed.get("codec") or meta.get("codec", "")
        audio = parsed.get("audio") or meta.get("audio", "")

        # Database overrides for title, season, episode
        title = db.get("title") or parsed.get("title", "Unknown")
        season = db.get("season") or parsed.get("season", "")
        episode = db.get("episode") or parsed.get("episode", "")
        episode_title = db.get("episode_title", "")
        year = db.get("year") or parsed.get("year", "")

        values = {
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
            "episode_title": episode_title,
            "resolution": resolution,
            "source": parsed.get("source", ""),
            "language": parsed.get("language", ""),
            "audio": audio,
            "codec": codec,
            "uploader": self.uploader,
            "group": parsed.get("group", ""),
            "ext": parsed.get("ext", "mkv"),
        }

        # Build template, skip empty optional fields
        name = self.template
        for key, val in values.items():
            placeholder = "{" + key + "}"
            if placeholder in name:
                if val:
                    name = name.replace(placeholder, str(val))
                else:
                    # Remove the placeholder and any adjacent dots/dashes
                    name = name.replace(placeholder, "")
                    name = name.replace("..", ".")
                    name = name.replace("--", "-")
                    name = name.replace(".-", "-")
                    name = name.replace("-.", "-")

        # Clean up triple+ dots/dashes
        while ".." in name:
            name = name.replace("..", ".")
        while "--" in name:
            name = name.replace("--", "-")
        # Remove leading/trailing dots/dashes from name part (before ext)
        name = name.strip(".-")
        # Ensure ext is present
        if "." not in name.split("/")[-1]:
            name = f"{name}.{values['ext']}"

        return name

    async def rename(self, filepath, db_metadata=None):
        """Parse filename, get metadata, build new name, rename file.

        db_metadata from AniList/TMDb: title, season, episode, episode_title
        Returns (new_filepath, new_name) or (None, None) on failure.
        """
        if not ospath.exists(filepath):
            _LOGGER.error(f"File not found: {filepath}")
            return None, None

        filename = ospath.basename(filepath)
        parsed = self.parse_filename(filename)
        metadata = await self.get_video_metadata(filepath)

        _LOGGER.info(f"Renamer: parsed={parsed}, metadata={metadata}, db={db_metadata}")

        new_name = self.build_new_name(parsed, metadata, db_metadata)
        if new_name == filename:
            _LOGGER.info(f"Renamer: no rename needed for {filename}")
            return filepath, filename

        dirpath = ospath.dirname(filepath)
        new_path = ospath.join(dirpath, new_name)

        # Avoid collision
        if ospath.exists(new_path) and new_path != filepath:
            base, ext = ospath.splitext(new_name)
            new_name = f"{base}_renamed{ext}"
            new_path = ospath.join(dirpath, new_name)

        try:
            await aiomove(filepath, new_path)
            _LOGGER.info(f"Renamer: {filename} -> {new_name}")
            return new_path, new_name
        except Exception as e:
            _LOGGER.error(f"Renamer failed: {e}")
            return None, None
