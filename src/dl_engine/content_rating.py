"""Conservative, explainable content-rating classification.

The source ``purity`` value remains the strongest signal.  Tags can raise an
image into a more restrictive bucket, but the absence of adult tags never
turns an unclassified image into SFW.  This keeps ``unknown`` honest while
providing deterministic NSFW and suggestive collections for navigation.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any


RATING_SFW = "sfw"
RATING_SUGGESTIVE = "suggestive"
RATING_NSFW = "nsfw"
RATING_UNKNOWN = "unknown"
CONTENT_RATINGS = (
    RATING_SFW,
    RATING_SUGGESTIVE,
    RATING_NSFW,
    RATING_UNKNOWN,
)

TAG_SEPARATOR = "\x1f"

# Terms here are deliberately conservative.  They require whole-token phrase
# matches after punctuation normalization, so e.g. ``class`` does not match
# ``ass``.  Provider purity still takes precedence when it is more restrictive.
EXPLICIT_TERMS: frozenset[str] = frozenset(
    {
        "adult content",
        "anal sex",
        "blowjob",
        "cum",
        "ejaculation",
        "explicit",
        "genitals",
        "hentai",
        "intercourse",
        "masturbation",
        "naked",
        "nipple",
        "nipples",
        "nude",
        "nudity",
        "oral sex",
        "penis",
        "porn",
        "pornographic",
        "pornography",
        "pussy",
        "semen",
        "sex",
        "sexual intercourse",
        "topless",
        "vagina",
        "vulva",
    }
)

SUGGESTIVE_TERMS: frozenset[str] = frozenset(
    {
        "ass",
        "bdsm",
        "bikini",
        "bondage",
        "boob",
        "boobs",
        "bra",
        "breast",
        "breasts",
        "butt",
        "cleavage",
        "ecchi",
        "erotic",
        "garter straps",
        "lingerie",
        "lewd",
        "panties",
        "see through clothes",
        "sexy",
        "spread legs",
        "suggestive",
        "swimsuit",
        "underwear",
    }
)

_SPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^\w]+", flags=re.UNICODE)


@dataclass(frozen=True)
class ContentRating:
    """One derived rating with enough evidence for UI explanation."""

    rating: str
    confidence: float
    basis: str
    reasons: tuple[str, ...] = ()


def normalize_label(value: object) -> str:
    """Normalize one provider label for whole-token phrase matching."""

    if value is None:
        return ""
    normalized = _NON_WORD_RE.sub(" ", str(value).casefold())
    return _SPACE_RE.sub(" ", normalized).strip()


def _tag_name(tag: object) -> str:
    if isinstance(tag, str):
        return tag
    if isinstance(tag, Mapping):
        value = tag.get("name")
        return "" if value is None else str(value)
    value = getattr(tag, "name", "")
    return "" if value is None else str(value)


def _matching_terms(labels: Iterable[str], terms: frozenset[str]) -> tuple[str, ...]:
    matches: set[str] = set()
    for label in labels:
        padded = f" {label} "
        for term in terms:
            if f" {term} " in padded:
                matches.add(term)
    return tuple(sorted(matches))


def classify_content(
    purity: object,
    tags: Iterable[Any] = (),
) -> ContentRating:
    """Derive an SFW/suggestive/NSFW/unknown classification.

    Explicit tag evidence is allowed to make a source label more restrictive.
    No tag evidence can infer SFW, so missing enrichment remains ``unknown``.
    """

    normalized_purity = normalize_label(purity)
    if normalized_purity in {"adult", "explicit", "nsfw", "unsafe"}:
        return ContentRating(RATING_NSFW, 1.0, "source-purity", (normalized_purity,))

    labels = tuple(
        label
        for label in (normalize_label(_tag_name(tag)) for tag in tags)
        if label
    )
    explicit = _matching_terms(labels, EXPLICIT_TERMS)
    if explicit:
        return ContentRating(RATING_NSFW, 0.9, "explicit-tag", explicit)

    if normalized_purity in {"questionable", "sketchy", "suggestive"}:
        return ContentRating(
            RATING_SUGGESTIVE,
            1.0,
            "source-purity",
            (normalized_purity,),
        )

    suggestive = _matching_terms(labels, SUGGESTIVE_TERMS)
    if suggestive:
        return ContentRating(RATING_SUGGESTIVE, 0.8, "suggestive-tag", suggestive)

    if normalized_purity in {"safe", "sfw"}:
        return ContentRating(RATING_SFW, 1.0, "source-purity", (normalized_purity,))

    return ContentRating(RATING_UNKNOWN, 0.0, "no-signal")


def classify_tag_blob(purity: object, tag_blob: object) -> str:
    """SQLite-friendly rating function using unit-separated tag names."""

    tags = [] if tag_blob is None else str(tag_blob).split(TAG_SEPARATOR)
    return classify_content(purity, tags).rating


def register_sqlite_function(conn: sqlite3.Connection) -> None:
    """Register the deterministic scalar used by content-rating queries."""

    conn.create_function(
        "wallpaper_content_rating",
        2,
        classify_tag_blob,
        deterministic=True,
    )
