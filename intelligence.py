import traceback

try:
    from models import Series, Book
    from datetime import date
except Exception as e:
    print("\n\n🔥 INTELLIGENCE MODULE FAILED DURING IMPORT 🔥")
    traceback.print_exc()
    raise e

from datetime import date
from models import Book, Series

def compute_series_intelligence_for_series(db, series_id: int):
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return None

    books = db.query(Book).filter(Book.series_id == series_id).all()

    if not books:
        return {
            "series_id": series_id,
            "total_books": 0,
            "read_count": 0,
            "unread_count": 0,
            "missing_orders": [],
            "next_unread_book_id": None,
            "next_upcoming_book_id": None,
            "is_series_finished": False,
        }

    # Sort by series_order
    books.sort(key=lambda b: b.series_order or 0)

    total_books = len(books)
    read_books = [b for b in books if b.is_read]
    unread_books = [b for b in books if not b.is_read]

    read_count = len(read_books)
    unread_count = len(unread_books)

    # Missing orders
    expected_orders = set(range(1, total_books + 1))
    actual_orders = set(b.series_order for b in books if b.series_order)
    missing_orders = sorted(list(expected_orders - actual_orders))

    # Next unread
    next_unread = unread_books[0] if unread_books else None

    # Upcoming = future publication_date
    today = date.today()
    upcoming_books = [
        b for b in books
        if b.publication_date and b.publication_date > today
    ]
    upcoming_books.sort(key=lambda b: b.publication_date)
    next_upcoming = upcoming_books[0] if upcoming_books else None

    return {
        "series_id": series_id,
        "total_books": total_books,
        "read_count": read_count,
        "unread_count": unread_count,
        "missing_orders": missing_orders,
        "next_unread_book_id": next_unread.id if next_unread else None,
        "next_upcoming_book_id": next_upcoming.id if next_upcoming else None,
        "is_series_finished": read_count == total_books,
    }
def recompute_series_intelligence(db):
    """
    Recompute intelligence for ALL series in the database.
    This is the function the importer expects.
    """

    all_series = db.query(Series).all()

    for series in all_series:
        intel = compute_series_intelligence_for_series(db, series.id)

        if intel is None:
            continue

        # Update the Series model fields
        series.total_books = intel.get("total_books")
        series.is_finished = intel.get("is_series_finished")

        # Commit updates
        db.commit()
        db.refresh(series)

    return True

