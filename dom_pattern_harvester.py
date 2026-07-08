"""Describe stable Amazon product detail DOM patterns from backend HTML."""


def _normalize_html(raw_html):
    if raw_html is None:
        return ""
    if isinstance(raw_html, str):
        return raw_html
    return str(raw_html)


def _contains(html_lower, needle):
    return needle.lower() in html_lower


def _has_any(html_lower, markers):
    for marker in markers:
        if _contains(html_lower, marker):
            return True
    return False


def _detect_page_type(html_text):
    html_lower = html_text.lower()

    detail_markers = (
        'id="dp"',
        "id='dp'",
        'id="producttitle"',
        "id='producttitle'",
        "detailbullets_feature_div",
        "booksdetails_feature_div",
        "bylineinfo",
        "dp-container",
    )

    if _has_any(html_lower, detail_markers):
        return "Amazon Product Detail Page"
    return "Unknown Page Type"


def _add_section(lines, title, entry):
    lines.append(title)
    if entry:
        lines.append("- " + entry)
    else:
        lines.append("- Not detected.")
    lines.append("")


def _detect_pattern(html_text, primary_markers, description, fallback_markers=()):
    html_lower = html_text.lower()
    if _has_any(html_lower, primary_markers):
        return description
    if fallback_markers and _has_any(html_lower, fallback_markers):
        return description
    return None


def _title_pattern(html_text):
    return _detect_pattern(
        html_text,
        ('id="producttitle"', "id='producttitle'"),
        "Title usually appears in an h1 with id productTitle inside the main product container.",
        ("titlefeaturediv", "centercol"),
    )


def _author_pattern(html_text):
    return _detect_pattern(
        html_text,
        ("bylineinfo", "author", "contributor"),
        "Author names often appear in the byline block near the title, usually as links or small text next to a 'by' label.",
        ("a-size-base",),
    )


def _series_name_pattern(html_text):
    return _detect_pattern(
        html_text,
        ("book ", " of ", "series", "books in this series", "seriesasinlist"),
        "Series name often appears in a nearby link or badge close to 'Book X of Y' text or inside a series module.",
        ("rhy_feature_div",),
    )


def _series_position_pattern(html_text):
    return _detect_pattern(
        html_text,
        ("book 1 of", "book 2 of", "book 3 of", " of "),
        "Series position appears in a small text block near the title or inside a metadata list as 'Book X of Y'.",
    )


def _price_pattern(html_text):
    return _detect_pattern(
        html_text,
        ("a-price-whole", "kindle", "paperback", "hardcover", "buybox"),
        "Prices usually appear in buybox or format offer sections, often in spans such as a-price-whole or adjacent format-specific price containers.",
        ("coreprice", "a-price"),
    )


def _availability_pattern(html_text):
    return _detect_pattern(
        html_text,
        ("in stock", "pre-order", "preorder", "availability", "out of stock"),
        "Availability usually appears near the buybox in a status block containing text like 'In Stock', 'Pre-order', or similar stock messaging.",
    )


def _asin_pattern(html_text):
    return _detect_pattern(
        html_text,
        ("asin", "detailbullets_feature_div", "product details", "booksdetails_feature_div"),
        "ASIN usually appears in the product details section inside a table row or list item labeled ASIN.",
    )


def _publication_date_pattern(html_text):
    return _detect_pattern(
        html_text,
        ("publication date", "publisher", "product details", "detailbullets_feature_div"),
        "Publication date usually appears in the product details section near labels like 'Publication date' or inside publisher metadata.",
    )


def _page_count_pattern(html_text):
    return _detect_pattern(
        html_text,
        ("print length", "page count", "pages", "product details"),
        "Page count usually appears in the product details section near labels like 'Print length'.",
    )


def _layout_variation_notes(html_text):
    html_lower = html_text.lower()
    notes = []

    if _has_any(html_lower, ('id="dp"', "id='dp'", 'id="producttitle"', "id='producttitle'")):
        notes.append(
            "Primary layout uses a main product container with productTitle, byline, buybox pricing, and product details modules."
        )
    if _has_any(html_lower, ("detailbullets_feature_div", "booksdetails_feature_div")):
        notes.append(
            "Details may appear either in detail bullets modules or in a book details feature section with similar labels but different container structure."
        )
    if _has_any(html_lower, ("seriesasinlist", "rhy_feature_div", "books in this series")):
        notes.append(
            "Some product pages add a separate series or recommendation module; these are secondary to the main product container."
        )

    if not notes:
        return "No clear product-detail layout variation detected."
    return " ".join(notes)


def _missing_fields(field_map):
    missing = []
    for label, value in field_map.items():
        if not value:
            missing.append(label)
    if not missing:
        return "None"
    return ", ".join(missing)


def harvest_dom_patterns(raw_html):
    html_text = _normalize_html(raw_html)
    page_type = _detect_page_type(html_text)

    title_pattern = _title_pattern(html_text) if page_type == "Amazon Product Detail Page" else None
    author_pattern = _author_pattern(html_text) if page_type == "Amazon Product Detail Page" else None
    series_name_pattern = _series_name_pattern(html_text) if page_type == "Amazon Product Detail Page" else None
    series_position_pattern = _series_position_pattern(html_text) if page_type == "Amazon Product Detail Page" else None
    price_pattern = _price_pattern(html_text) if page_type == "Amazon Product Detail Page" else None
    availability_pattern = _availability_pattern(html_text) if page_type == "Amazon Product Detail Page" else None
    asin_pattern = _asin_pattern(html_text) if page_type == "Amazon Product Detail Page" else None
    publication_date_pattern = _publication_date_pattern(html_text) if page_type == "Amazon Product Detail Page" else None
    page_count_pattern = _page_count_pattern(html_text) if page_type == "Amazon Product Detail Page" else None
    layout_variation_notes = _layout_variation_notes(html_text) if page_type == "Amazon Product Detail Page" else None

    missing_fields = _missing_fields(
        {
            "Title Pattern": title_pattern,
            "Author Pattern": author_pattern,
            "Series Name Pattern": series_name_pattern,
            "Series Position Pattern": series_position_pattern,
            "Price Pattern": price_pattern,
            "Availability Pattern": availability_pattern,
            "ASIN Pattern": asin_pattern,
            "Publication Date Pattern": publication_date_pattern,
            "Page Count Pattern": page_count_pattern,
            "Layout Variation Notes": layout_variation_notes,
        }
    )

    lines = []
    _add_section(lines, "Page Type", page_type)
    _add_section(lines, "Title Pattern", title_pattern)
    _add_section(lines, "Author Pattern", author_pattern)
    _add_section(lines, "Series Name Pattern", series_name_pattern)
    _add_section(lines, "Series Position Pattern", series_position_pattern)
    _add_section(lines, "Price Pattern", price_pattern)
    _add_section(lines, "Availability Pattern", availability_pattern)
    _add_section(lines, "ASIN Pattern", asin_pattern)
    _add_section(lines, "Publication Date Pattern", publication_date_pattern)
    _add_section(lines, "Page Count Pattern", page_count_pattern)
    _add_section(lines, "Layout Variation Notes", layout_variation_notes)
    _add_section(lines, "Missing Fields", missing_fields)

    return "\n".join(lines).strip()


def describe_dom_patterns(raw_html):
    return harvest_dom_patterns(raw_html)