"""TheDiscDB (https://thediscdb.com) lookup: search its GraphQL API, match
ripped files to disc titles by length, and suggest names.

Nothing here touches Qt or the network; the dialog sends SEARCH_QUERY and
hands the JSON response to parse_search().
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .makemkv import duration_seconds

ENDPOINT = "https://thediscdb.com/graphql"

# Fields come straight from each release disc: the older nested
# "discs { disc { ... } }" form still has the titles but leaves the disc's
# index, name and slug empty, so every disc looked like "disc 1".
_MEDIA_FIELDS = """
    nodes {
      title year type slug
      releases {
        title slug year
        discs {
          index name slug format
          titles {
            index duration size sourceFile itemType description season episode
            item { title type season episode }
          }
        }
      }
    }
"""

SEARCH_QUERY = """
query Search($text: String!) {
  mediaItems(first: 10, where: { title: { contains: $text } }) {%s}
}
""" % _MEDIA_FIELDS

SLUG_QUERY = """
query BySlug($slug: String!) {
  mediaItems(first: 1, where: { slug: { eq: $slug } }) {%s}
}
""" % _MEDIA_FIELDS

MATCH_TOLERANCE = 2.0  # seconds; TheDiscDB stores lengths to the second


@dataclass
class DbTitle:
    index: int
    seconds: int
    size: int
    source: str
    kind: str       # MainMovie, Episode, Extra, DeletedScene, Trailer, ...
    name: str
    season: int | None = None
    episode: int | None = None

    @property
    def is_main(self) -> bool:
        return self.kind.lower() in ("mainmovie", "movie")

    @property
    def is_episode(self) -> bool:
        return self.kind.lower() == "episode" or (self.season is not None and self.episode is not None)

    def label(self) -> str:
        kind = {"mainmovie": "Main movie"}.get(self.kind.lower(), self.kind or "Title")
        if self.is_episode and self.season is not None and self.episode is not None:
            kind = f"S{self.season:02}E{self.episode:02}"
        length = f"{self.seconds // 3600}:{self.seconds // 60 % 60:02}:{self.seconds % 60:02}"
        return f"{kind} · {self.name} · {length}"


@dataclass
class DbDisc:
    media_title: str
    year: int | None
    media_type: str      # Movie / Series
    release: str
    disc_index: int
    disc_name: str
    format: str
    titles: list[DbTitle] = field(default_factory=list)
    release_slug: str = ""
    disc_slug: str = ""

    @property
    def base_name(self) -> str:
        return f"{self.media_title} ({self.year})" if self.year else self.media_title

    def label(self) -> str:
        disc = f"“{self.disc_name}”" if self.disc_name else f"disc {self.disc_index}" if self.disc_index else "disc"
        return f"{self.base_name} · {self.release} · {disc} · {self.format}"


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_search(response: dict) -> list[DbDisc]:
    """Discs from a SEARCH_QUERY response. Only titles that TheDiscDB names
    are kept (unnamed playlists can't be used for renaming)."""
    discs = []
    nodes = ((response.get("data") or {}).get("mediaItems") or {}).get("nodes") or []
    for media in nodes:
        for release in media.get("releases") or []:
            for release_disc in release.get("discs") or []:
                disc = release_disc.get("disc") or release_disc  # see _MEDIA_FIELDS
                titles = []
                for title in disc.get("titles") or []:
                    item = title.get("item") or {}
                    name = item.get("title") or title.get("description") or ""
                    if not item or not name or not title.get("duration"):
                        continue
                    titles.append(DbTitle(
                        index=title.get("index") or 0,
                        seconds=duration_seconds(title["duration"]),
                        size=title.get("size") or 0,
                        source=title.get("sourceFile") or "",
                        kind=title.get("itemType") or item.get("type") or "",
                        name=name,
                        season=_int(title.get("season") or item.get("season")),
                        episode=_int(title.get("episode") or item.get("episode")),
                    ))
                if titles:
                    discs.append(DbDisc(media.get("title") or "", media.get("year"), media.get("type") or "",
                                        release.get("title") or "", disc.get("index") or 0,
                                        disc.get("name") or "", disc.get("format") or "", titles,
                                        release.get("slug") or "", disc.get("slug") or ""))
    return discs


_LINK = re.compile(r"(?:https?://)?(?:www\.)?thediscdb\.com/(?:movie|series|[a-z]+)/([^/?#\s]+)"
                   r"(?:/releases/([^/?#\s]+)(?:/discs/([^/?#\s]+))?)?", re.IGNORECASE)


def parse_link(text: str) -> tuple[str, str, str] | None:
    """(media slug, release slug, disc slug) from a thediscdb.com link, e.g.
    https://thediscdb.com/movie/<media>/releases/<release>/discs/<disc>.
    The release and disc parts are "" when the link stops before them."""
    found = _LINK.search(text.strip())
    return (found.group(1), found.group(2) or "", found.group(3) or "") if found else None


def discs_for_link(discs: list[DbDisc], release_slug: str, disc_slug: str) -> list[DbDisc]:
    """The discs a link points to: one disc, one release, or the whole title."""
    return [d for d in discs if (not release_slug or d.release_slug == release_slug)
            and (not disc_slug or d.disc_slug == disc_slug)]


@dataclass
class LocalFile:
    path: Path
    seconds: float


def match_files(files: list[LocalFile], disc: DbDisc, tolerance: float = MATCH_TOLERANCE) -> dict[Path, DbTitle]:
    """Pair files with disc titles whose length is within tolerance, each
    title used once, closest lengths first."""
    pairs = sorted(
        (abs(f.seconds - t.seconds), i, j)
        for i, f in enumerate(files) for j, t in enumerate(disc.titles)
        if abs(f.seconds - t.seconds) <= tolerance
    )
    used_files, used_titles, result = set(), set(), {}
    for _delta, i, j in pairs:
        if i in used_files or j in used_titles:
            continue
        used_files.add(i)
        used_titles.add(j)
        result[files[i].path] = disc.titles[j]
    return result


def rank_discs(files: list[LocalFile], discs: list[DbDisc]) -> list[tuple[DbDisc, dict[Path, DbTitle]]]:
    """Discs with their matches, most matched files first (main movie matches break ties)."""
    ranked = [(disc, match_files(files, disc)) for disc in discs]
    ranked.sort(key=lambda r: (len(r[1]), sum(t.is_main for t in r[1].values())), reverse=True)
    return ranked


_UNSAFE = str.maketrans({"/": "-", "\\": "-", "?": "", "*": "", "\"": "'", "<": "", ">": "", "|": "-"})


def safe_name(text: str) -> str:
    """A file/folder name that also works on the Windows/Samba NAS: no path
    separators or reserved characters, ':' becomes ' -'."""
    text = re.sub(r"\s*:\s*", " - ", text).translate(_UNSAFE)
    return re.sub(r"\s+", " ", text).strip().rstrip(".") or "Untitled"


def suggest_names(matches: dict[Path, DbTitle], disc: DbDisc) -> dict[Path, str]:
    """Suggested path (relative to the folder, without extension) per file,
    in the Jellyfin layout the library uses:
        main movie  -> "Title (Year)"   (several versions get " - <name>")
        episode     -> "Season 01/Title S01E02 - Name"
        anything else -> "extras/<name>"
    Duplicate names get " (2)", " (3)"."""
    base = safe_name(disc.base_name)
    mains = [t for t in matches.values() if t.is_main]
    names: dict[Path, str] = {}
    for path, title in matches.items():
        if title.is_main:
            name = base
            if len(mains) > 1 and safe_name(title.name) != safe_name(disc.media_title):
                name += f" - {safe_name(title.name)}"
        elif title.is_episode and title.season is not None and title.episode is not None:
            name = f"Season {title.season:02}/{safe_name(disc.media_title)} S{title.season:02}E{title.episode:02}"
            if title.name and title.name != disc.media_title:
                name += f" - {safe_name(title.name)}"
        else:
            name = f"extras/{safe_name(title.name)}"
        names[path] = name
    seen: dict[str, int] = {}
    for path in sorted(names, key=str):
        key = names[path].casefold()
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            names[path] = f"{names[path]} ({seen[key]})"
    return names


def search_text(folder_name: str) -> str:
    """A search term from a rip folder name: drop "(2)"-style suffixes,
    years in brackets and underscores."""
    text = re.sub(r"\(\d+\)|\[.*?\]", " ", folder_name).replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()
