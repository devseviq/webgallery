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

# ---------------------------------------------------------------------------
# NSFW subcategory (second axis). Only meaningful when the overall content
# rating is ``nsfw``. It splits the single ``nsfw`` bucket so the gallery can
# stop lumping every adult image together. Precedence is explicit > fetish >
# nudity > unspecified. ``unspecified`` is the triage bucket: provider-marked
# NSFW with no matching sub-tag, i.e. exactly the images that used to be
# indistinguishable inside the lump.
# ---------------------------------------------------------------------------
NSFW_SUB_NUDITY = "nudity"
NSFW_SUB_EXPLICIT = "explicit"
NSFW_SUB_FETISH = "fetish"
NSFW_SUB_UNSPECIFIED = "unspecified"
NSFW_SUBCATEGORIES = (
    NSFW_SUB_NUDITY,
    NSFW_SUB_EXPLICIT,
    NSFW_SUB_FETISH,
    NSFW_SUB_UNSPECIFIED,
)

# Bare bodies without a depicted sex act (solo / artistic / glamour nudity).
NSFW_NUDITY_TERMS: frozenset[str] = frozenset(
    {
        "areola",
        "artistic nudity",
        "bare breasts",
        "bottomless",
        "breast",
        "breasts",
        "completely naked",
        "naked",
        "nipple",
        "nipples",
        "no bra",
        "no clothes",
        "nude",
        "nudity",
        "topless",
        "undressed",
        "vagina",
        "vulva",
    }
)

# Depicted sexual acts (or the umbrella drawn-explicit term).
NSFW_EXPLICIT_ACT_TERMS: frozenset[str] = frozenset(
    {
        "69",
        "anal",
        "anal sex",
        "blowjob",
        "creampie",
        "cum",
        "double penetration",
        "ejaculation",
        "fingering",
        "gangbang",
        "handjob",
        "hentai",
        "intercourse",
        "masturbation",
        "oral sex",
        "orgy",
        "paizuri",
        "penetration",
        "scissoring",
        "semen",
        "sex",
        "sexual intercourse",
        "threesome",
        "vaginal sex",
    }
)

# Fetish / kink themes (no specific act required).
NSFW_FETISH_TERMS: frozenset[str] = frozenset(
    {
        "armpits",
        "bdsm",
        "bondage",
        "bound",
        "collar",
        "domination",
        "femdom",
        "feet",
        "foot fetish",
        "gag",
        "gagged",
        "humiliation",
        "latex",
        "leash",
        "leather",
        "maledom",
        "masochism",
        "nipple clamps",
        "pet play",
        "restraints",
        "sadism",
        "shibari",
        "slave",
        "spanking",
        "submission",
        "submissive",
        "tied up",
        "whipping",
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


def classify_nsfw_subcategory(
    purity: object,
    tags: Iterable[Any] = (),
) -> str:
    """Derive a finer NSFW subcategory (second axis).

    Returns one of :data:`NSFW_SUBCATEGORIES`. Precedence is
    ``explicit`` > ``fetish`` > ``nudity`` > ``unspecified``. The result is
    only meaningful when :func:`classify_content` already rates the image as
    ``nsfw``; for any other rating (or no signal) it returns ``unspecified``,
    which is also the triage bucket for provider-NSFW images with no matching
    sub-tag.
    """

    # Callers may provide a generator. Materialize it once so the overall
    # rating and second-axis classifier inspect identical evidence.
    materialized_tags = tuple(tags)
    normalized_purity = normalize_label(purity)
    overall = classify_content(purity, materialized_tags).rating
    if overall != RATING_NSFW:
        return NSFW_SUB_UNSPECIFIED

    labels = tuple(
        label
        for label in (
            normalize_label(_tag_name(tag)) for tag in materialized_tags
        )
        if label
    )
    if _matching_terms(labels, NSFW_EXPLICIT_ACT_TERMS):
        return NSFW_SUB_EXPLICIT
    if _matching_terms(labels, NSFW_FETISH_TERMS):
        return NSFW_SUB_FETISH
    if _matching_terms(labels, NSFW_NUDITY_TERMS):
        return NSFW_SUB_NUDITY
    # Provider purity already forced NSFW up in classify_content; keep that
    # signal so a purity-only NSFW image still lands here rather than nudity.
    if normalized_purity in {"adult", "explicit", "nsfw", "unsafe"}:
        return NSFW_SUB_UNSPECIFIED
    return NSFW_SUB_UNSPECIFIED


def nsfw_subcategory_tag_blob(purity: object, tag_blob: object) -> str:
    """SQLite-friendly NSFW subcategory using unit-separated tag names."""

    tags = [] if tag_blob is None else str(tag_blob).split(TAG_SEPARATOR)
    return classify_nsfw_subcategory(purity, tags)


def register_sqlite_function(conn: sqlite3.Connection) -> None:
    """Register the deterministic scalars used by content-rating queries."""

    conn.create_function(
        "wallpaper_content_rating",
        2,
        classify_tag_blob,
        deterministic=True,
    )
    conn.create_function(
        "wallpaper_nsfw_subcategory",
        2,
        nsfw_subcategory_tag_blob,
        deterministic=True,
    )
