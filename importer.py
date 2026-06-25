import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Series, Book

# Excel serial date → Python date
def excel_to_date(value):
    if pd.isna(value):
        return None
    try:
        return datetime(1899, 12, 30) + timedelta(days=int(value))
    except:
        return None

def run_import(file_path: str):
    db: Session = SessionLocal()

    print("Loading spreadsheet...")
    df = pd.read_excel(file_path, sheet_name="Master")

    for _, row in df.iterrows():
        series_name = row.get("Series Name")
        author = row.get("Author")

        # CASE 1 — Standalone book (no series)
        if pd.isna(series_name) or not str(series_name).strip():
            date_read = excel_to_date(row.get("Date Read"))
            year = date_read.year if date_read else None

            db_book = Book(
                title=row["Title"],
                author=row["Author"],
                year=year,
                genre=None,
                series_id=None,
                book_number=None,
                release_date=excel_to_date(row.get("Next Release Date")),
                read_status=(row.get("Record Status") == "Read"),
            )

            db.add(db_book)
            db.commit()
            print(f"Imported standalone book: {row['Title']}")
            continue

        # CASE 2 — Book belongs to a series
        db_series = (
            db.query(Series)
            .filter(Series.name == series_name, Series.author == author)
            .first()
        )

        if not db_series:
            db_series = Series(
                name=series_name,
                author=author,
                google_query_url=None,
                series_finished=(row.get("Series Finished") == "Yes"),
                check_series=(row.get("Check Series") == "No"),
                last_checked=excel_to_date(row.get("Last Checked")),
            )
            db.add(db_series)
            db.commit()
            db.refresh(db_series)

        date_read = excel_to_date(row.get("Date Read"))
        year = date_read.year if date_read else None

        db_book = Book(
            title=row["Title"],
            author=row["Author"],
            year=year,
            genre=None,
            series_id=db_series.id,
            book_number=row.get("Book #"),
            release_date=excel_to_date(row.get("Next Release Date")),
            read_status=(row.get("Record Status") == "Read"),
        )

        db.add(db_book)
        db.commit()
        print(f"Imported series book: {row['Title']}")

    print("Import complete.")

if __name__ == "__main__":
    run_import("Books_Tracker_Master_Pivot_21Jun2026.xlsx")
