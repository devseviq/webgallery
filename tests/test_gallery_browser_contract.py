from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GALLERY = ROOT / "reports" / "library-browser.html"


class _GalleryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.scripts: list[dict[str, object]] = []
        self._script: dict[str, object] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.elements[element_id] = (tag, values)
        if tag == "script":
            self._script = {"attrs": values, "parts": []}
            self.scripts.append(self._script)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._script = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            parts = self._script["parts"]
            assert isinstance(parts, list)
            parts.append(data)


def _function_body(script: str, name: str) -> str:
    """Extract a named function using brace/string/comment-aware scanning."""

    match = re.search(
        rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
        script,
    )
    if match is None:
        raise AssertionError(f"JavaScript function not found: {name}")
    start = match.end() - 1
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = start
    while index < len(script):
        char = script[index]
        next_char = script[index + 1] if index + 1 < len(script) else ""
        if line_comment:
            if char in "\r\n":
                line_comment = False
        elif block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 1
        elif quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in {"'", '"', "`"}:
            quote = char
        elif char == "/" and next_char == "/":
            line_comment = True
            index += 1
        elif char == "/" and next_char == "*":
            block_comment = True
            index += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return script[start + 1 : index]
        index += 1
    raise AssertionError(f"Unbalanced JavaScript function: {name}")


class GalleryBrowserContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = GALLERY.read_text(encoding="utf-8")
        cls.parser = _GalleryParser()
        cls.parser.feed(cls.html)
        executable = []
        for script in cls.parser.scripts:
            attrs = script["attrs"]
            assert isinstance(attrs, dict)
            script_type = attrs.get("type")
            if "src" not in attrs and script_type in {
                None,
                "",
                "text/javascript",
                "application/javascript",
                "module",
            }:
                executable.append(script)
        cls.executable_scripts = executable
        if len(executable) == 1:
            parts = executable[0]["parts"]
            assert isinstance(parts, list)
            cls.script = "".join(str(part) for part in parts)
        else:
            cls.script = ""

    def element(self, element_id: str) -> tuple[str, dict[str, str | None]]:
        self.assertIn(element_id, self.parser.elements)
        return self.parser.elements[element_id]

    def test_exactly_one_inline_application_script_has_valid_syntax(self) -> None:
        self.assertEqual(len(self.executable_scripts), 1)
        self.assertNotIn(".innerHTML", self.script)
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is not available for JavaScript syntax checking")
        temporary = tempfile.NamedTemporaryFile(
            mode="w", suffix=".js", encoding="utf-8", delete=False
        )
        try:
            with temporary:
                temporary.write(self.script)
            result = subprocess.run(
                [node, "--check", temporary.name],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        finally:
            Path(temporary.name).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cards_use_thumbnails_and_full_resolution_is_explicit(self) -> None:
        render_item = _function_body(self.script, "renderItem")
        self.assertIn("img.src = item.thumbnail_url", render_item)
        self.assertNotRegex(render_item, r"img\.src\s*=\s*item\.original_url")
        self.assertNotIn("item.url", self.script)
        original_assignments = re.findall(
            r"\b\w+\.src\s*=\s*item\.original_url", self.script
        )
        self.assertEqual(original_assignments, ["loader.src = item.original_url"])
        load_original = _function_body(self.script, "loadDetailOriginal")
        self.assertIn("loader.src = item.original_url", load_original)
        self.assertIn("generation !== detailImageGeneration", load_original)
        self.assertIn("detailItemId !== item.id", load_original)
        self.assertIn("detailOriginalLoader !== loader", load_original)
        self.assertIn(
            "var hasVisiblePreview = !detailImage.hidden && "
            "detailImage.hasAttribute('src')",
            load_original,
        )
        self.assertIn("if (hasVisiblePreview)", load_original)
        self.assertIn(
            "keeping the cached thumbnail",
            load_original,
        )
        self.assertIn(
            "no thumbnail preview is available",
            load_original,
        )
        load_tag, _load_attrs = self.element("detail-load-original")
        self.assertEqual(load_tag, "button")
        open_detail = _function_body(self.script, "openDetail")
        self.assertIn("detailDialog.showModal()", open_detail)
        self.assertNotIn("loadDetailOriginal(item)", open_detail)
        self.assertNotIn("requestAnimationFrame", open_detail)
        preview = _function_body(self.script, "renderDetailPreview")
        self.assertIn("image.src = item.thumbnail_url", preview)
        self.assertIn("generation !== detailImageGeneration", preview)
        self.assertIn("if (item) loadDetailOriginal(item)", self.script)
        self.assertIn("img.loading = 'lazy'", render_item)
        self.assertIn("img.decoding = 'async'", render_item)
        self.assertIn("img.width", render_item)
        self.assertIn("Thumbnail unavailable", render_item)

    def test_no_filesystem_or_database_url_contract_is_reintroduced(self) -> None:
        lowered = self.html.casefold()
        for forbidden in (
            "../library",
            "../temp_downloads",
            ".wallpaper-download-queue",
            "queue-state",
            "wallpaper_library.sqlite",
            "sqlite://",
            "file://",
        ):
            self.assertNotIn(forbidden, lowered)
        self.assertNotRegex(self.html, r"[A-Za-z]:\\")

    def test_nsfw_subcategory_uses_response_schema_three_end_to_end(self) -> None:
        field_tag, field_attrs = self.element("nsfw-subcategory-field")
        self.assertEqual(field_tag, "label")
        self.assertIn("hidden", field_attrs)
        select_tag, select_attrs = self.element("nsfw-subcategory")
        self.assertEqual(select_tag, "select")
        self.assertIn("disabled", select_attrs)
        self.assertIn("var LIBRARY_RESPONSE_SCHEMA = 3", self.script)
        self.assertIn(
            "['', 'nudity', 'explicit', 'fetish', 'unspecified']",
            self.script,
        )

        read_url = _function_body(self.script, "readUrl")
        self.assertIn("p.get('nsfw_subcategory')", read_url)
        self.assertIn("normalizeNsfwSubcategory()", read_url)
        normalize = _function_body(self.script, "normalizeNsfwSubcategory")
        self.assertIn("state.rating !== 'nsfw'", normalize)
        self.assertIn("state.nsfw_subcategory = ''", normalize)

        sync_url = _function_body(self.script, "syncUrl")
        api_url = _function_body(self.script, "apiUrl")
        for body in (sync_url, api_url):
            self.assertIn("state.rating === 'nsfw'", body)
            self.assertIn("nsfw_subcategory", body)

        controls = _function_body(self.script, "updateControls")
        self.assertIn("nsfwSubcategoryField.hidden", controls)
        self.assertIn("nsfwSubcategorySelect.disabled", controls)
        self.assertIn(
            "setSelect('nsfw-subcategory', facets.nsfw_subcategories",
            _function_body(self.script, "loadStatus"),
        )
        self.assertIn(
            "Number(data.facets.schema_version) !== LIBRARY_RESPONSE_SCHEMA",
            _function_body(self.script, "loadStatus"),
        )
        self.assertIn(
            "Number(data.schema_version) !== LIBRARY_RESPONSE_SCHEMA",
            _function_body(self.script, "loadMore"),
        )
        self.assertIn(
            "nsfw_subcategory",
            _function_body(self.script, "renderActiveFilterChips"),
        )
        self.assertIn(
            "item.nsfw_subcategory",
            _function_body(self.script, "renderDetailFacts"),
        )
        self.assertIn(
            "'rating', 'nsfw_subcategory'",
            _function_body(self.script, "applyPreset"),
        )
        self.assertIn(
            "state.rating = allowed(button.dataset.rating, RATINGS, 'sfw');\n"
            "        normalizeNsfwSubcategory();",
            self.script,
        )
        for identifier in (
            "recent",
            "desktop-4k",
            "portrait",
            "needs-rating",
            "least-tagged",
            "shuffle",
        ):
            self.assertRegex(
                self.script,
                rf"'{re.escape(identifier)}': \{{[^\n}}]*nsfw_subcategory: ''",
            )
        self.assertIn(
            "state.nsfw_subcategory = ''",
            _function_body(self.script, "clearNavigationFilters"),
        )
        hydrate = _function_body(self.script, "hydrateFromHistory")
        self.assertIn("readUrl()", hydrate)
        self.assertIn("normalizeNsfwSubcategory()", read_url)
        self.assertIn(
            "window.addEventListener('popstate', hydrateFromHistory)",
            self.script,
        )

    def test_accessibility_state_announcements_focus_and_motion_contract(self) -> None:
        status_tag, status_attrs = self.element("status")
        self.assertEqual(status_tag, "div")
        self.assertEqual(status_attrs.get("role"), "status")
        self.assertEqual(status_attrs.get("aria-atomic"), "true")
        filters_tag, filters_attrs = self.element("filters")
        self.assertEqual(filters_tag, "form")
        self.assertEqual(filters_attrs.get("aria-label"), "Gallery filters")
        grid_tag, grid_attrs = self.element("grid")
        self.assertEqual(grid_tag, "section")
        self.assertEqual(grid_attrs.get("aria-label"), "Wallpaper results")
        self.assertNotIn("aria-live", grid_attrs)

        rating_tabs = _function_body(self.script, "paintRatingTabs")
        self.assertIn("button.setAttribute('aria-pressed'", rating_tabs)
        self.assertIn(":focus-visible", self.html)
        self.assertIn("outline: 3px solid var(--accent)", self.html)
        self.assertIn("min-height: 28px", self.html)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.html)
        self.assertIn("transition: none !important", self.html)

    def test_counted_keyboard_autocomplete_applies_exact_provider_tags(self) -> None:
        tag, attrs = self.element("tag")
        self.assertEqual(tag, "input")
        self.assertEqual(attrs.get("role"), "combobox")
        self.assertEqual(attrs.get("aria-autocomplete"), "list")
        self.assertEqual(attrs.get("aria-controls"), "tag-options")
        options_tag, options_attrs = self.element("tag-options")
        self.assertEqual(options_tag, "ul")
        self.assertEqual(options_attrs.get("role"), "listbox")
        fetch_body = _function_body(self.script, "fetchAutocomplete")
        self.assertIn("/api/library/tags", fetch_body)
        self.assertIn("prefix", fetch_body)
        self.assertIn("limit", fetch_body)
        queue_body = _function_body(self.script, "queueAutocomplete")
        self.assertRegex(queue_body, r"setTimeout\([\s\S]*?,\s*220\)")
        self.assertIn("autocompleteController.abort()", queue_body)
        key_body = _function_body(self.script, "handleAutocompleteKey")
        for key in ("ArrowDown", "ArrowUp", "Enter", "Escape"):
            self.assertIn(key, key_body)
        self.assertIn(
            "setAutocompleteIndex(autocompleteIndex + 1)",
            key_body,
        )
        self.assertIn(
            "setAutocompleteIndex(autocompleteIndex < 0 ? "
            "autocompleteItems.length - 1 : autocompleteIndex - 1)",
            key_body,
        )
        autocomplete_index = _function_body(self.script, "setAutocompleteIndex")
        self.assertIn(
            "(index + autocompleteItems.length) % autocompleteItems.length",
            autocomplete_index,
        )
        render_body = _function_body(self.script, "renderAutocomplete")
        self.assertIn("item.image_count", render_body)
        self.assertIn("role', 'option", render_body)
        self.assertIn("applyExactTag(item.name)", render_body)
        self.assertIn("tagOptions.hidden = !hasOptions", render_body)
        self.assertIn("hasOptions ? 'true' : 'false'", render_body)
        self.assertIn("item.tags", _function_body(self.script, "renderItem"))
        self.assertIn("providerTagButton(tag)", self.script)
        diverse = _function_body(self.script, "diverseCardTags")
        self.assertIn("usedPriorities", diverse)
        self.assertIn("remainder.slice", diverse)
        self.assertLess(diverse.index(".sort("), diverse.index(".filter("))

    def test_dialog_is_accessible_navigable_and_returns_focus(self) -> None:
        tag, attrs = self.element("detail-dialog")
        self.assertEqual(tag, "dialog")
        self.assertEqual(attrs.get("aria-labelledby"), "detail-title")
        self.assertEqual(attrs.get("aria-describedby"), "detail-status")
        title_tag, title_attrs = self.element("detail-title")
        self.assertEqual(title_tag, "h2")
        self.assertEqual(title_attrs.get("tabindex"), "-1")
        _, close_attrs = self.element("detail-close")
        self.assertIn("Close", close_attrs.get("aria-label") or "")
        _, original_attrs = self.element("detail-original")
        self.assertEqual(original_attrs.get("target"), "_blank")
        self.assertIn("noopener", original_attrs.get("rel") or "")
        close_body = _function_body(self.script, "finalizeDetailClose")
        self.assertIn("opener.focus()", close_body)
        self.assertIn("preventScroll: true", close_body)
        self.assertIn("detailImage.removeAttribute('src')", close_body)
        self.assertIn("event.target === detailDialog", self.script)
        self.assertIn("event.key === 'Escape'", self.script)
        self.assertIn("event.key === 'ArrowLeft'", self.script)
        self.assertIn("event.key === 'ArrowRight'", self.script)
        self.assertIn("isInteractiveTarget(event.target)", self.script)
        open_body = _function_body(self.script, "openDetail")
        self.assertIn("detailTitle.focus()", open_body)
        self.assertNotIn("detail-close').focus()", open_body)
        interactive_body = _function_body(self.script, "isInteractiveTarget")
        self.assertIn("button, a, input, textarea, select", interactive_body)
        self.assertIn("trapFallbackFocus(event)", self.script)
        self.assertIn(
            "dialog.detail-dialog:not([open]):not(.fallback-open)", self.html
        )
        self.assertIn("fallbackSurfaceState", self.script)
        self.assertIn("surface.inert = true", self.script)
        self.assertIn("document.body.style.overflow = 'hidden'", self.script)

    def test_rating_tab_painting_and_handlers_exclude_dialog_rating_state(self) -> None:
        scoped_selector = ".rating-tabs button[data-rating]"
        self.assertEqual(
            self.script.count(f"document.querySelectorAll('{scoped_selector}')"),
            2,
        )
        self.assertNotIn("document.querySelectorAll('[data-rating]')", self.script)
        paint = _function_body(self.script, "paintRatingTabs")
        self.assertIn(scoped_selector, paint)
        self.assertIn("setAttribute('aria-pressed'", paint)
        self.assertIn("detailDialog.dataset.rating", _function_body(self.script, "openDetail"))

    def test_density_fit_and_seed_are_bookmarkable_without_page_offsets(self) -> None:
        self.assertIn("['compact', 'comfortable', 'cinematic']", self.script)
        self.assertIn("['contain', 'crop']", self.script)
        sync = _function_body(self.script, "syncUrl")
        self.assertIn("searchParams.set('density'", sync)
        self.assertIn("searchParams.set('fit'", sync)
        self.assertIn("searchParams.set('seed'", sync)
        self.assertIn("searchParams.delete('offset')", sync)
        presentation = _function_body(self.script, "applyPresentation")
        self.assertIn("document.body.dataset.density", presentation)
        self.assertIn("document.body.dataset.fit", presentation)
        self.assertNotIn("resetFeed", presentation)
        self.assertIn("window.crypto.getRandomValues", self.script)
        self.assertIn("MAX_SHUFFLE_SEED", self.script)
        api_url = _function_body(self.script, "apiUrl")
        self.assertIn("shuffle_seed", api_url)
        self.assertIn("String(state.seed)", api_url)

    def test_navigation_pushes_intent_and_popstate_rehydrates_without_ephemera(self) -> None:
        sync = _function_body(self.script, "syncUrl")
        self.assertIn("history.pushState", sync)
        self.assertIn("history.replaceState", sync)
        self.assertIn("nextKey === currentKey", sync)
        commit = _function_body(self.script, "commitNavigation")
        self.assertIn("syncUrl('push')", commit)
        self.assertIn("if (resetCollection) resetFeed()", commit)

        hydrate = _function_body(self.script, "hydrateFromHistory")
        for hook in (
            "readUrl()",
            "updateControls()",
            "syncUrl('replace')",
            "resetFeed()",
        ):
            self.assertIn(hook, hydrate)
        self.assertEqual(
            self.script.count("window.addEventListener('popstate'"),
            1,
        )
        self.assertIn(
            "readUrl();\n    updateControls();\n    syncUrl('replace');",
            self.script,
        )
        self.assertNotIn("syncUrl", _function_body(self.script, "resetFeed"))
        self.assertNotIn("syncUrl", _function_body(self.script, "appendBatch"))
        for ephemeral in (
            "selectedIds",
            "data-reveal-nsfw",
            "activeTransferJob",
            "detailItemId",
        ):
            self.assertNotIn(ephemeral, sync)
            self.assertNotIn(ephemeral, _function_body(self.script, "readUrl"))

    def test_transfer_controls_and_dialog_have_responsive_contracts(self) -> None:
        options_tag, options_attrs = self.element("transfer-options")
        self.assertEqual(options_tag, "div")
        self.assertIn("hidden", options_attrs)
        _, selection_attrs = self.element("selection-count")
        self.assertEqual(selection_attrs.get("aria-live"), "polite")
        selection = _function_body(self.script, "updateSelectionUi")
        self.assertIn("transferExpanded", selection)
        self.assertIn("transferOptions.hidden = !transferExpanded", selection)
        for query in (
            "@media (max-width: 768px)",
            "@media (max-width: 390px)",
            "@media (max-width: 320px)",
            "@media (max-width: 768px) and (orientation: landscape)",
        ):
            self.assertIn(query, self.html)
        self.assertIn("max-height: calc(100dvh - 24px)", self.html)
        self.assertIn(".transfer-bar { position: static; }", self.html)

    def test_named_presets_are_ordinary_filters_and_active_chips(self) -> None:
        for identifier in (
            "recent",
            "desktop-4k",
            "portrait",
            "needs-rating",
            "least-tagged",
            "shuffle",
        ):
            self.assertIn(f'data-preset="{identifier}"', self.html)
        self.assertIn("rating: 'unknown'", self.script)
        self.assertIn("sort: 'rating_confidence'", self.script)
        self.assertIn("sort: 'least_tagged'", self.script)
        self.assertIn("sort: 'shuffle'", self.script)
        self.assertIn("orientation: 'landscape'", self.script)
        self.assertIn("bucket: '4K'", self.script)
        apply_preset = _function_body(self.script, "applyPreset")
        self.assertIn("state[key] = preset[key]", apply_preset)
        self.assertIn("commitNavigation(true)", apply_preset)
        chips = _function_body(self.script, "renderActiveFilterChips")
        for key in (
            "rating",
            "nsfw_subcategory",
            "source",
            "orientation",
            "bucket",
            "tag",
            "franchise",
            "sort",
            "preset",
        ):
            self.assertIn(key, chips)

    def test_suggestion_review_is_post_only_and_separate_from_provider_tags(self) -> None:
        provider = _function_body(self.script, "renderProviderTags")
        suggestions = _function_body(self.script, "renderSuggestions")
        review = _function_body(self.script, "reviewSuggestion")
        self.assertIn("item.tags", provider)
        self.assertNotIn("tag_suggestions", provider)
        self.assertIn("item.tag_suggestions", suggestions)
        self.assertIn("suggestion.created_at", suggestions)
        self.assertIn("suggestion.reviewed_at", suggestions)
        self.assertNotRegex(suggestions, r"item\.tags\s*=")
        self.assertIn("/api/library/suggestions/", review)
        self.assertIn("method: 'POST'", review)
        self.assertIn("review_status: reviewStatus", review)
        self.assertIn("reviewer: reviewer", review)
        self.assertIn("decision_note:", review)
        self.assertIn("data.suggestion", review)
        self.assertIn("detailItemId === item.id", review)
        self.assertIn("pendingSuggestionReviews", review)
        self.assertNotRegex(review, r"item\.(?:tags|content_rating|tag_count)\s*=")

    def test_existing_safety_scroll_selection_transfer_and_status_hooks_remain(self) -> None:
        for hook in (
            "data-reveal-nsfw",
            "rating-nsfw",
            "IntersectionObserver",
            "requestGeneration",
            "AbortController",
            "seenIds",
            "selectedIds",
            "select-loaded",
            "clear-selection",
            "/api/library/transfers",
            "activeTransferJob",
            "missingCount",
            "Working snapshot — not currently verified",
            "Unknown content remains separate from SFW",
        ):
            self.assertIn(hook, self.html)
        self.assertIn("item.exists && item.original_url", self.script)
        selection_ui = _function_body(self.script, "updateSelectionUi")
        self.assertIn("checkbox.disabled = Boolean(activeTransferJob)", selection_ui)
        self.assertIn("if (activeTransferJob)", _function_body(self.script, "setItemSelected"))
        facts = _function_body(self.script, "renderDetailFacts")
        self.assertIn("item.provider_coverage", facts)
        self.assertIn("coverage.provenances", facts)


if __name__ == "__main__":
    unittest.main()
