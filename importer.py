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
            db_book = Book(
                title=row["Title"],
                author=row["Author"],
                format=row.get("Format"),
                publication_date=excel_to_date(row.get("Publication Date")),
                series_id=None,
                book_number=row.get("Book #"),
                is_read=(row.get("Record Status") == "Read"),
                read_date=excel_to_date(row.get("Date Read")),
                rating=row.get("Rating"),
                notes=row.get("Notes"),
            )

            db.add(db_book)
            db.commit()
            print(f"Imported standalone book: {row['Title']}")
            continue

        # CASE 2 — Book belongs to a series
        db_series = (
            db.query(Series)
            .filter(Series.name == series_name)
            .first()
        )

        if not db_series:
            db_series = Series(
                name=series_name,
                is_finished=(row.get("Series Finished") == "Yes"),
                total_books=row.get("Series Total Books"),
            )
            db.add(db_series)
            db.commit()
            db.refresh(db_series)

        db_book = Book(
            title=row["Title"],
            author=row["Author"],
            format=row.get("Format"),
            publication_date=excel_to_date(row.get("Publication Date")),
            series_id=db_series.id,
            series_order=row.get("Book #"),
            series_total_books=row.get("Series Total Books"),
            is_series_finished=(row.get("Series Finished") == "Yes"),
            book_number=row.get("Book #"),
            is_read=(row.get("Record Status") == "Read"),
            read_date=excel_to_date(row.get("Date Read")),
            rating=row.get("Rating"),
            notes=row.get("Notes"),
        )

        db.add(db_book)
        db.commit()
        print(f"Imported series book: {row['Title']}")

    print("Import complete.")


if __name__ == "__main__":
    # CHANGE THIS TO YOUR MINI-LIBRARY FILE
    run_import("Small_Master Library_21June2026.xlsx")
