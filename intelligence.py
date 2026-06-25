# intelligence.py
#
# Goodreads-powered detection engine
# Rookie Mode: extremely explicit, fully commented, no magic.

import re
import requests
import datetime
from bs4 import BeautifulSoup
from typing import List, Optional, Dict, Any

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

GOODREADS_BASE_URL = "https://www.goodreads.com"
GOODREADS_SEARCH_URL = f"{GOODREADS_BASE_URL}/search"

# Pretend to be a browser so Goodreads doesn't block us
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Written numbers 1–50 (for "Book Thirty-Five" style)
WRITTEN_NUMBERS: Dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
    "twenty-one": 21, "twenty-two": 22, "twenty-three": 23,
    "twenty-four": 24, "twenty-five": 25, "twenty-six": 26,
    "twenty-seven": 27, "twenty-eight": 28, "twenty-nine": 29,
    "thirty": 30,
    "thirty-one": 31, "thirty-two": 32, "thirty-three": 33,
    "thirty-four": 34, "thirty-five": 35, "thirty-six": 36,
    "thirty-seven": 37, "thirty-eight": 38, "thirty-nine": 39,
    "forty": 40,
    "forty-one": 41, "forty-two": 42, "forty-three": 43,
    "forty-four": 44, "forty-five": 45, "forty-six": 46,
    "forty-seven": 47, "forty-eight": 48, "forty-nine": 49,
    "fifty": 50,
}

# ---------------------------------------------------------------------------
# BASIC HELPERS
# ---------------------------------------------------------------------------

def generate_goodreads_search_url(title: str, author: Optional[str] = None) -> str:
    """
    Build a Goodreads search URL using title + optional author.
    """
    query_parts = [title]
    if author:
        query_parts.append(author)
    query = "+".join(part.replace(" ", "+") for part in query_parts)
    return f"{GOODREADS_SEARCH_URL}?q={query}"


def fetch_page_html(url: str) -> Optional[str]:
    """
    Fetch raw HTML from a URL.
    Returns None if request fails.
    """
    try:
        resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=15)
        if resp.status_code == 200:
            return resp.text
        return None
    except Exception:
        return None


def parse_int_safe(value: str) -> Optional[int]:
    """Convert string to int safely."""
    try:
        return int(value)
    except Exception:
        return None


def normalize_space(text: str) -> str:
    """Normalize whitespace (handles weird spaces)."""
    return re.sub(r"\s+", " ", text).strip()


def extract_written_number(text: str) -> Optional[int]:
    """
    Look for written numbers (one, twenty-one, thirty-five, etc.)
    """
    text_lower = text.lower()
    for word, num in WRITTEN_NUMBERS.items():
        if word in text_lower:
            return num
    return None


def extract_book_number_from_text(text: str) -> Optional[int]:
    """
    Extract book number from messy Goodreads text.
    Handles:
      - "Book 12"
      - "Book 12 of 14"
      - "#12"
      - "12th book"
      - "Book Thirty-Five"
    """
    text_norm = normalize_space(text)

    # Pattern 1: "Book 12"
    m = re.search(r"Book\s+(\d+)", text_norm, re.IGNORECASE)
    if m:
        return parse_int_safe(m.group(1))

    # Pattern 2: "#12"
    m = re.search(r"#\s*(\d+)", text_norm)
    if m:
        return parse_int_safe(m.group(1))

    # Pattern 3: "12th book"
    m = re.search(r"(\d+)\s*(st|nd|rd|th)\s+(book|installment)", text_norm, re.IGNORECASE)
    if m:
        return parse_int_safe(m.group(1))

    # Pattern 4: written numbers
    written = extract_written_number(text_norm)
    if written is not None:
        return written

    return None
# ---------------------------------------------------------------------------
# GOODREADS SEARCH PARSING
# ---------------------------------------------------------------------------

def parse_goodreads_search(html: str) -> Dict[str, Optional[str]]:
    """
    Parse Goodreads search results HTML.
    Returns:
      {
        "series_url": URL to series page (if found),
        "book_url": URL to first book result (fallback)
      }
    """
    soup = BeautifulSoup(html, "html.parser")

    result = {
        "series_url": None,
        "book_url": None,
    }

    # Goodreads search results are usually in a table with class "tableList"
    table = soup.find("table", class_="tableList")
    if not table:
        return result

    rows = table.find_all("tr")
    if not rows:
        return result

    # Try to find a "Series" result first (Hybrid strategy)
    for row in rows:
        series_link = row.find("a", href=True)
        if series_link and "series" in series_link.get("href", ""):
            href = series_link["href"]
            if href.startswith("/"):
                href = GOODREADS_BASE_URL + href
            result["series_url"] = href
            break

    # If no series_url found, fallback to first book result
    if result["series_url"] is None:
        first_row = rows[0]
        book_link = first_row.find("a", class_="bookTitle", href=True)
        if book_link:
            href = book_link["href"]
            if href.startswith("/"):
                href = GOODREADS_BASE_URL + href
            result["book_url"] = href

    return result


# ---------------------------------------------------------------------------
# GOODREADS BOOK PAGE SCRAPING
# ---------------------------------------------------------------------------

def scrape_goodreads_book_page(html: str) -> Dict[str, Optional[str]]:
    """
    Scrape a Goodreads book page to find:
      - series name (if any)
      - series URL (if any)
      - book number within the series (if any)
    """
    soup = BeautifulSoup(html, "html.parser")

    result = {
        "series_name": None,
        "series_url": None,
        "book_number": None,
    }

    # Goodreads often shows series info in a "h2" or "div" near the title
    series_section = soup.find("h2", class_="gr-h2--seriestitle")
    if not series_section:
        series_section = soup.find("div", class_="seriesHeader")

    if series_section:
        text = normalize_space(series_section.get_text(" ", strip=True))

        # Extract book number
        num = extract_book_number_from_text(text)
        if num is not None:
            result["book_number"] = num

        # Extract series name (usually after colon)
        parts = text.split(":")
        if len(parts) > 1:
            result["series_name"] = parts[-1].strip()

        # Extract series URL
        series_link = series_section.find("a", href=True)
        if series_link:
            href = series_link["href"]
            if href.startswith("/"):
                href = GOODREADS_BASE_URL + href
            result["series_url"] = href

    return result


# ---------------------------------------------------------------------------
# GOODREADS SERIES PAGE SCRAPING
# ---------------------------------------------------------------------------

def scrape_goodreads_series_page(html: str) -> Dict[str, Any]:
    """
    Scrape a Goodreads series page.
    Returns:
      {
        "series_name": str,
        "books": [
          {
            "title": str,
            "book_number": int or None,
            "year": int or None
          },
          ...
        ]
      }
    """
    soup = BeautifulSoup(html, "html.parser")

    # Series name
    series_name = None
    h1 = soup.find("h1")
    if h1:
        series_name = normalize_space(h1.get_text(" ", strip=True))
    else:
        h2 = soup.find("h2")
        if h2:
            series_name = normalize_space(h2.get_text(" ", strip=True))

    books: List[Dict[str, Any]] = []

    # Goodreads series pages vary; try multiple patterns
    book_items = soup.find_all("div", class_="book")
    if not book_items:
        book_table = soup.find("table")
        if book_table:
            book_items = book_table.find_all("tr")

    for item in book_items:
        title_tag = item.find("a", class_="bookTitle") or item.find("a", href=True)
        if not title_tag:
            continue

        title = normalize_space(title_tag.get_text(" ", strip=True))

        # Extract book number
        text = normalize_space(item.get_text(" ", strip=True))
        book_number = extract_book_number_from_text(text)

        # Extract year (conservative)
        year = None
        m = re.search(r"(\d{4})", text)
        if m:
            year_int = parse_int_safe(m.group(1))
            if year_int and 1500 <= year_int <= 2100:
                year = year_int

        books.append(
            {
                "title": title,
                "book_number": book_number,
                "year": year,
            }
        )

    return {
        "series_name": series_name,
        "books": books,
    }


# ---------------------------------------------------------------------------
# HIGH-LEVEL DETECTION API
# ---------------------------------------------------------------------------

def detect_series_from_goodreads(title: str, author: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    High-level function:
    - Search Goodreads for (title + author)
    - Find series (via series result or book page)
    - Scrape series page
    - Return structured series data
    """
    search_url = generate_goodreads_search_url(title, author)
    search_html = fetch_page_html(search_url)
    if not search_html:
        return None

    search_info = parse_goodreads_search(search_html)

    series_url = search_info.get("series_url")
    book_url = search_info.get("book_url")

    # Direct series hit
    if series_url:
        series_html = fetch_page_html(series_url)
        if not series_html:
            return None
        series_data = scrape_goodreads_series_page(series_html)
        if series_data.get("series_name") and series_data.get("books"):
            return series_data
        return None

    # Fallback: book page → series link
    if book_url:
        book_html = fetch_page_html(book_url)
        if not book_html:
            return None

        book_info = scrape_goodreads_book_page(book_html)
        series_url = book_info.get("series_url")
        if not series_url:
            return None

        series_html = fetch_page_html(series_url)
        if not series_html:
            return None

        series_data = scrape_goodreads_series_page(series_html)
        if series_data.get("series_name") and series_data.get("books"):
            return series_data

    return None

# ---------------------------------------------------------------------------
# ORM INTEGRATION — CHECK BOOK FOR SERIES
# ---------------------------------------------------------------------------

def check_book_for_series(db, book):
    """
    Given a Book ORM object (with title, author),
    use Goodreads to detect its series and auto-create it if needed.

    Returns a dict with series info, or None if no series detected.
    """

    # Step 1: Detect series from Goodreads
    series_data = detect_series_from_goodreads(book.title, book.author)
    if not series_data:
        return None

    series_name = series_data["series_name"]
    books_data = series_data["books"]

    # Import models dynamically (avoids circular imports)
    from models import Series, Book as BookModel

    # Step 2: Find or create the series
    existing_series = (
        db.query(Series)
        .filter(Series.name == series_name)
        .first()
    )

    if existing_series:
        series = existing_series
    else:
        series = Series(name=series_name)
        db.add(series)
        db.flush()  # get series.id

    # Step 3: Add or update books in the series
    for b in books_data:
        title = b["title"]
        book_number = b["book_number"]
        year = b["year"]

        existing_book = (
            db.query(BookModel)
            .filter(BookModel.series_id == series.id)
            .filter(BookModel.title == title)
            .first()
        )

        if existing_book:
            # Update missing fields
            if book_number is not None and existing_book.book_number is None:
                existing_book.book_number = book_number
            if year is not None and existing_book.year is None:
                existing_book.year = year
        else:
            # Create new book entry
            new_book = BookModel(
                title=title,
                author=book.author,
                genre=book.genre,
                year=year,
                series_id=series.id,
                book_number=book_number,
                release_date=None,
                read_status=False,
            )
            db.add(new_book)

    # Step 4: Link the original book to the series
    if book.series_id is None:
        book.series_id = series.id

    db.commit()

    return {
        "series_id": series.id,
        "series_name": series.name,
        "books": books_data,
    }


# ---------------------------------------------------------------------------
# CHECK FOR NEW BOOKS (OPTION C — HYBRID)
# ---------------------------------------------------------------------------

def check_for_new_books(db, series):
    """
    Hybrid strategy:
      1. Try Goodreads search using series.name
      2. If nothing found, fallback to first book title + author

    Adds missing books to the DB and recomputes intelligence.
    """

    from models import Book as BookModel

    # Step 1: Try series name
    series_data = detect_series_from_goodreads(series.name)

    # Step 2: Fallback — use first book title + author
    if not series_data:
        first_book = (
            db.query(BookModel)
            .filter(BookModel.series_id == series.id)
            .order_by(BookModel.book_number.asc())
            .first()
        )
        if first_book:
            series_data = detect_series_from_goodreads(first_book.title, first_book.author)

    if not series_data:
        return None

    books_data = series_data["books"]

    # Step 3: Add missing books
    for b in books_data:
        title = b["title"]
        book_number = b["book_number"]
        year = b["year"]

        existing_book = (
            db.query(BookModel)
            .filter(BookModel.series_id == series.id)
            .filter(BookModel.title == title)
            .first()
        )

        if not existing_book:
            new_book = BookModel(
                title=title,
                author=series.books[0].author if series.books else None,
                genre=series.books[0].genre if series.books else None,
                year=year,
                series_id=series.id,
                book_number=book_number,
                release_date=None,
                read_status=False,
            )
            db.add(new_book)

    db.commit()

    # Step 4: Recompute intelligence
    return compute_series_intelligence(db, series)


# ---------------------------------------------------------------------------
# SERIES INTELLIGENCE ENGINE
# ---------------------------------------------------------------------------

def compute_series_intelligence(db, series):
    """
    Recomputes intelligence fields for a series:
    - total_books
    - read_books
    - unread_books
    - next_unread_book
    - upcoming_books
    - missing_books
    """

    from models import Book as BookModel

    # Load all books in this series
    books = (
        db.query(BookModel)
        .filter(BookModel.series_id == series.id)
        .order_by(BookModel.book_number.asc())
        .all()
    )

    # Basic counts
    total_books = len(books)
    read_books = sum(1 for b in books if b.read_status)
    unread_books = total_books - read_books

    # Next unread book
    next_unread = None
    for b in books:
        if not b.read_status:
            next_unread = b
            break

    # Upcoming books (future releases)
    upcoming = []
    for b in books:
        if b.release_date and b.release_date > datetime.date.today():
            upcoming.append(b)

    # Missing book numbers (gaps)
    numbers = [b.book_number for b in books if b.book_number is not None]
    missing = []
    if numbers:
        min_n = min(numbers)
        max_n = max(numbers)
        for n in range(min_n, max_n + 1):
            if n not in numbers:
                missing.append(n)

    # Save intelligence fields
    series.total_books = total_books
    series.read_books = read_books
    series.unread_books = unread_books
    series.next_unread_book = next_unread.book_number if next_unread else None
    series.upcoming_books = len(upcoming)
    series.missing_books = missing

    db.commit()

    return {
        "series_id": series.id,
        "total_books": total_books,
        "read_books": read_books,
        "unread_books": unread_books,
        "next_unread_book": series.next_unread_book,
        "upcoming_books": series.upcoming_books,
        "missing_books": missing,
    }
