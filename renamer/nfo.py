"""
renamer.nfo
===========
Generates Kodi / Jellyfin / Emby compatible tvshow.nfo metadata files
for anime series folders, using TMDB metadata cross-referenced with
AniList and romanised titles.
"""

from __future__ import annotations

import dataclasses
import datetime
import html
from typing import TYPE_CHECKING, Any

from renamer.config import Config, get_logger
from renamer.providers.base import _get_shared_session
from renamer.romaniser import get_romaniser

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger(__name__)


@dataclasses.dataclass
class NfoActor:
    """Actor / cast member metadata for NFO."""

    name: str
    role: str
    type: str = "Actor"
    thumb: str = ""
    sortorder: int | None = None


@dataclasses.dataclass
class NfoData:
    """All metadata fields required to construct a tvshow.nfo file."""

    title: str  # Romaji series title
    originaltitle: str = ""  # English or original title
    plot: str = ""  # Show synopsis / overview
    outline: str = ""  # Show outline (defaults to plot)
    lockdata: bool = False
    dateadded: str = ""  # YYYY-MM-DD HH:MM:SS
    trailer: str = ""  # Trailer URL e.g. plugin://plugin.video.youtube/play/?video_id=...
    rating: float | int | None = None  # Rating / vote average
    year: str = ""  # Premiered year
    mpaa: str = ""  # Content rating e.g. TV-14
    imdb_id: str = ""
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    anilist_id: int | None = None
    premiered: str = ""  # YYYY-MM-DD
    releasedate: str = ""  # YYYY-MM-DD
    enddate: str = ""  # YYYY-MM-DD
    runtime: int | None = None  # Minutes
    genres: list[str] = dataclasses.field(default_factory=list)
    studios: list[str] = dataclasses.field(default_factory=list)
    tags: list[str] = dataclasses.field(default_factory=list)
    poster_path: str = ""  # Local folder.jpg or remote URL
    fanart_path: str = ""  # Local backdrop.jpg or remote URL
    actors: list[NfoActor] = dataclasses.field(default_factory=list)
    status: str = "Ended"  # Ended, Returning Series, etc.


class NfoGenerator:
    """
    Fetches show metadata from TMDB and AniList, resolves the Romaji title,
    and formats/writes tvshow.nfo files.
    """

    TMDB_BASE = "https://api.themoviedb.org/3"

    def __init__(self, cfg: Config | None = None) -> None:
        self.cfg = cfg or Config()
        self._session = _get_shared_session()

    def fetch_tmdb_show(self, tmdb_id: int) -> dict[str, Any] | None:
        """
        Fetch full TV show details from TMDB with appended extras:
        credits, external_ids, videos, keywords, content_ratings.
        """
        api_key = self.cfg.tmdb_api_key
        if not api_key:
            log.warning("TMDB API key is not configured — cannot fetch show %s", tmdb_id)
            return None

        url = f"{self.TMDB_BASE}/tv/{tmdb_id}"
        params = {
            "api_key": api_key,
            "append_to_response": "credits,external_ids,videos,keywords,content_ratings",
            "language": "en-US",
        }
        try:
            resp = self._session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            log.warning("TMDB API returned HTTP %s for tv id=%s", resp.status_code, tmdb_id)
        except Exception as exc:
            log.error("Failed to fetch TMDB show details for %s: %s", tmdb_id, exc)
        return None

    def fetch_tmdb_japanese_name(self, tmdb_id: int) -> str:
        """Fetch Japanese title from TMDB /tv/{id}?language=ja."""
        api_key = self.cfg.tmdb_api_key
        if not api_key:
            return ""
        url = f"{self.TMDB_BASE}/tv/{tmdb_id}"
        params = {"api_key": api_key, "language": "ja"}
        try:
            resp = self._session.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("name") or data.get("original_name") or ""
        except Exception as exc:
            log.debug("Failed to fetch Japanese title from TMDB for %s: %s", tmdb_id, exc)
        return ""

    def fetch_anilist_details(
        self,
        search_name: str,
        anilist_id: int | None = None,
    ) -> dict[str, Any] | None:
        """
        Query AniList for media info (romaji title, genres, tags, anilist_id).
        """
        from renamer.providers.anilist import AniListFetcher

        fetcher = AniListFetcher(token=getattr(self.cfg, "anilist_token", ""))
        query = """
        query ($id: Int, $search: String) {
          Media(id: $id, search: $search, type: ANIME) {
            id
            title {
              romaji
              english
              native
            }
            description(asHtml: false)
            genres
            tags {
              name
              isMediaSpoiler
            }
          }
        }
        """
        variables: dict[str, Any] = {}
        if anilist_id:
            variables["id"] = anilist_id
        elif search_name:
            variables["search"] = search_name
        else:
            return None

        try:
            return fetcher._gql(query, variables)
        except Exception as exc:
            log.debug("AniList details lookup failed: %s", exc)
            return None

    def build_nfo_data(
        self,
        tmdb_id: int,
        folder: Path | None = None,
        preferred_title: str | None = None,
        anilist_id: int | None = None,
    ) -> NfoData | None:
        """
        Build NfoData for a given TMDB series ID.
        """
        show = self.fetch_tmdb_show(tmdb_id)
        if not show:
            return None

        en_name = (show.get("name") or "").strip()
        orig_name = (show.get("original_name") or "").strip()
        ja_name = self.fetch_tmdb_japanese_name(tmdb_id) or orig_name or en_name

        # 1. Resolve Romaji title
        romaniser = get_romaniser()
        tmdb_romaji = romaniser.to_romaji(ja_name) if ja_name else en_name

        resolved_romaji = ""
        resolved_al_id = anilist_id

        # Cross-reference with AniList
        al_data = None
        try:
            from renamer.providers.romaji_resolver import RomajiResolver

            resolver = RomajiResolver()
            search_key = preferred_title or en_name or orig_name
            resolved_romaji = resolver.resolve(
                search_name=search_key,
                tmdb_romaji=tmdb_romaji,
                english_name=en_name,
            )
            # Also get AniList details
            al_data = self.fetch_anilist_details(
                search_name=preferred_title or en_name,
                anilist_id=resolved_al_id,
            )
            if al_data and not resolved_al_id:
                resolved_al_id = al_data.get("id")
        except Exception as exc:
            log.warning("Romaji resolution error for TMDB id=%d: %s", tmdb_id, exc)

        final_title = preferred_title or resolved_romaji or tmdb_romaji or en_name
        # Original title: en_name if different from title, or orig_name
        original_title = en_name if en_name != final_title else orig_name

        # 2. Synopsis / Plot
        plot = (show.get("overview") or "").strip()
        if not plot and al_data:
            plot = (al_data.get("description") or "").strip()

        # 3. Date added
        dateadded = ""
        if folder and folder.exists():
            try:
                mtime = folder.stat().st_mtime
                dateadded = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        if not dateadded:
            dateadded = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 4. Trailer
        trailer_url = ""
        for v in show.get("videos", {}).get("results", []):
            if v.get("site") == "YouTube" and v.get("type") in ("Trailer", "Teaser"):
                key = v.get("key")
                if key:
                    trailer_url = f"plugin://plugin.video.youtube/play/?video_id={key}"
                    break

        # 5. Rating & Year
        vote_avg = show.get("vote_average")
        rating = None
        if vote_avg is not None:
            # Format nicely as int or float
            rating = round(vote_avg) if abs(vote_avg - round(vote_avg)) < 0.05 else round(vote_avg, 1)

        first_air = show.get("first_air_date") or ""
        last_air = show.get("last_air_date") or ""
        year = first_air[:4] if len(first_air) >= 4 else ""

        # 6. MPAA Content Rating
        mpaa = ""
        content_ratings = show.get("content_ratings", {}).get("results", [])
        for cr in content_ratings:
            if cr.get("iso_3166_1") == "US" and cr.get("rating"):
                mpaa = cr["rating"]
                break
        if not mpaa and content_ratings:
            mpaa = content_ratings[0].get("rating", "")

        # 7. External IDs
        ext_ids = show.get("external_ids", {}) or {}
        imdb_id = ext_ids.get("imdb_id") or ""
        tvdb_id = ext_ids.get("tvdb_id")

        # 8. Runtime
        run_times = show.get("episode_run_time") or []
        runtime = run_times[0] if run_times else None

        # 9. Genres (TMDB + AniList)
        genres_set: list[str] = []
        for g in show.get("genres", []):
            name = g.get("name")
            if name:
                # Expand "Action & Adventure", "Sci-Fi & Fantasy"
                if "&" in name:
                    for part in name.split("&"):
                        p = part.strip()
                        if p and p not in genres_set:
                            genres_set.append(p)
                elif name not in genres_set:
                    genres_set.append(name)

        if al_data:
            for g in al_data.get("genres", []):
                if g and g not in genres_set:
                    genres_set.append(g)

        # Ensure "Anime" is included for Jellyfin/Kodi anime libraries
        if "Anime" not in genres_set:
            genres_set.append("Anime")

        genres_set.sort()

        # 10. Studios (networks + production companies)
        studios: list[str] = []
        for net in show.get("networks", []):
            n = net.get("name")
            if n and n not in studios:
                studios.append(n)
        for comp in show.get("production_companies", []):
            c = comp.get("name")
            if c and c not in studios:
                studios.append(c)

        # 11. Tags (TMDB keywords + AniList tags)
        tags_seen: set[str] = set()
        tags_list: list[str] = []

        for kw in show.get("keywords", {}).get("results", []):
            t_name = kw.get("name", "").strip()
            if t_name and t_name.lower() not in tags_seen:
                tags_seen.add(t_name.lower())
                tags_list.append(t_name)

        if al_data:
            for t in al_data.get("tags", []):
                # skip spoilers
                if t.get("isMediaSpoiler"):
                    continue
                t_name = t.get("name", "").strip()
                if t_name and t_name.lower() not in tags_seen:
                    tags_seen.add(t_name.lower())
                    tags_list.append(t_name)

        tags_list.sort(key=lambda s: s.lower())

        # 12. Art paths
        poster_path = ""
        fanart_path = ""
        if folder:
            # Check existing poster / folder files
            for p_name in ("folder.jpg", "poster.jpg", "folder.png"):
                if (folder / p_name).is_file():
                    poster_path = str((folder / p_name).resolve())
                    break
            if not poster_path:
                poster_path = str((folder / "folder.jpg").resolve())

            for f_name in ("backdrop.jpg", "fanart.jpg", "backdrop.png"):
                if (folder / f_name).is_file():
                    fanart_path = str((folder / f_name).resolve())
                    break
            if not fanart_path:
                fanart_path = str((folder / "backdrop.jpg").resolve())

        # 13. Actors / Cast
        actors: list[NfoActor] = []
        cast = show.get("credits", {}).get("cast", [])
        for member in cast:
            name = member.get("name", "").strip()
            character = member.get("character", "").strip()
            if not name:
                continue
            profile = member.get("profile_path") or ""
            thumb = f"https://image.tmdb.org/t/p/original{profile}" if profile else ""
            order = member.get("order")
            actors.append(
                NfoActor(
                    name=name,
                    role=character,
                    type="Actor",
                    thumb=thumb,
                    sortorder=order,
                )
            )

        # 14. Status
        status = show.get("status") or "Ended"

        return NfoData(
            title=final_title,
            originaltitle=original_title,
            plot=plot,
            outline=plot,
            lockdata=False,
            dateadded=dateadded,
            trailer=trailer_url,
            rating=rating,
            year=year,
            mpaa=mpaa,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            anilist_id=resolved_al_id,
            premiered=first_air,
            releasedate=first_air,
            enddate=last_air,
            runtime=runtime,
            genres=genres_set,
            studios=studios,
            tags=tags_list,
            poster_path=poster_path,
            fanart_path=fanart_path,
            actors=actors,
            status=status,
        )

    def render_xml(self, data: NfoData) -> str:
        """
        Generate the XML representation conforming to Kodi/Jellyfin tvshow.nfo.
        Matches the formatting and layout of tests/tvshow.nfo.
        """
        lines: list[str] = [
            '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
            "<tvshow>",
        ]

        def _tag(name: str, val: Any) -> None:
            if val is not None and val != "":
                text = html.escape(str(val))
                lines.append(f"  <{name}>{text}</{name}>")

        _tag("plot", data.plot)
        _tag("outline", data.outline)
        lines.append(f"  <lockdata>{'true' if data.lockdata else 'false'}</lockdata>")
        _tag("dateadded", data.dateadded)
        _tag("title", data.title)
        _tag("originaltitle", data.originaltitle)
        _tag("trailer", data.trailer)
        _tag("rating", data.rating)
        _tag("year", data.year)
        _tag("mpaa", data.mpaa)
        _tag("imdb_id", data.imdb_id)
        _tag("tmdbid", data.tmdb_id)
        _tag("premiered", data.premiered)
        _tag("releasedate", data.releasedate)
        _tag("enddate", data.enddate)
        _tag("runtime", data.runtime)

        for g in data.genres:
            _tag("genre", g)

        for s in data.studios:
            _tag("studio", s)

        for t in data.tags:
            _tag("tag", t)

        _tag("anilistid", data.anilist_id)
        _tag("tvdbid", data.tvdb_id)

        if data.poster_path or data.fanart_path:
            lines.append("  <art>")
            if data.poster_path:
                lines.append(f"    <poster>{html.escape(data.poster_path)}</poster>")
            if data.fanart_path:
                lines.append(f"    <fanart>{html.escape(data.fanart_path)}</fanart>")
            lines.append("  </art>")

        for act in data.actors:
            lines.append("  <actor>")
            lines.append(f"    <name>{html.escape(act.name)}</name>")
            lines.append(f"    <role>{html.escape(act.role)}</role>")
            lines.append(f"    <type>{html.escape(act.type)}</type>")
            if act.sortorder is not None:
                lines.append(f"    <sortorder>{act.sortorder}</sortorder>")
            if act.thumb:
                lines.append(f"    <thumb>{html.escape(act.thumb)}</thumb>")
            lines.append("  </actor>")

        if data.tvdb_id:
            _tag("id", data.tvdb_id)
            lines.append("  <episodeguide>")
            lines.append(
                f'    <url cache="{data.tvdb_id}.xml">http://www.thetvdb.com/api/1D62F2F90030C444/series/{data.tvdb_id}/all/en.zip</url>'
            )
            lines.append("  </episodeguide>")
        elif data.tmdb_id:
            _tag("id", data.tmdb_id)

        lines.append("  <season>-1</season>")
        lines.append("  <episode>-1</episode>")
        _tag("status", data.status)
        lines.append("</tvshow>\n")

        return "\n".join(lines)

    def write_nfo(
        self,
        folder: Path,
        data: NfoData,
        overwrite: bool = True,
    ) -> Path | None:
        """Write tvshow.nfo inside the specified folder."""
        if not folder.is_dir():
            log.error("Folder does not exist: %s", folder)
            return None

        nfo_path = folder / "tvshow.nfo"
        if nfo_path.exists() and not overwrite:
            log.info("NFO file already exists at %s — skipping (overwrite=False)", nfo_path)
            return nfo_path

        xml_text = self.render_xml(data)
        nfo_path.write_text(xml_text, encoding="utf-8")
        log.info("Created tvshow.nfo: %s", nfo_path)
        return nfo_path

    def process_folder(
        self,
        folder: Path,
        tmdb_id: int | None = None,
        preferred_title: str | None = None,
        anilist_id: int | None = None,
        overwrite: bool = True,
    ) -> Path | None:
        """
        Identify series for a single folder, fetch metadata, and write tvshow.nfo.
        """
        if not folder.is_dir():
            log.error("Cannot process non-directory: %s", folder)
            return None

        # Check existing
        nfo_path = folder / "tvshow.nfo"
        if nfo_path.exists() and not overwrite:
            log.info("tvshow.nfo already exists in %s", folder.name)
            return nfo_path

        # If tmdb_id not provided, try resolving from cache or search
        if not tmdb_id:
            from renamer.cache import SeriesCache
            from renamer.cli.multi_series import LibraryDBCache, auto_identify_series

            # 1. Per-folder cache
            cache = SeriesCache(folder).load()
            if cache:
                tmdb_id = cache.get("tmdb_id") or cache.get("tmdb_series_id")
                preferred_title = preferred_title or cache.get("series_name")
                anilist_id = anilist_id or cache.get("anilist_id")

            # 2. Library DB cache
            if not tmdb_id:
                db_cache = LibraryDBCache(folder.parent)
                db_hit = db_cache.get(folder.name)
                if db_hit:
                    tmdb_id = db_hit.get("tmdb_id")
                    preferred_title = preferred_title or db_hit.get("resolved_name")
                    anilist_id = anilist_id or db_hit.get("anilist_id")

            # 3. Auto-identify series via TMDB search
            if not tmdb_id:
                db_cache = LibraryDBCache(folder.parent)
                discovered = auto_identify_series(folder, self.cfg, db_cache)
                tmdb_id = discovered.tmdb_id
                preferred_title = preferred_title or discovered.resolved_name
                anilist_id = anilist_id or discovered.anilist_id

        if not tmdb_id:
            log.warning("Could not resolve TMDB ID for folder '%s'", folder.name)
            return None

        data = self.build_nfo_data(
            tmdb_id=tmdb_id,
            folder=folder,
            preferred_title=preferred_title,
            anilist_id=anilist_id,
        )
        if not data:
            log.warning("Failed to build NFO data for '%s' (TMDB %d)", folder.name, tmdb_id)
            return None

        return self.write_nfo(folder, data, overwrite=overwrite)
