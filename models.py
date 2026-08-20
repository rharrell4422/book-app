from datetime import datetime, date
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Date,
    DateTime,
    Float,
    Text,
    ForeignKey,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import relationship
from database import Base
from services.discovery_health import compute_discovery_health


class User(Base):
    """Schema-only groundwork for eventual multi-tenancy -- not wired into
    auth yet (see routers/deps.py, which still does single-owner-password +
    share-token auth). Adding this table and the nullable owner_id columns
    below now means a future move to real per-user accounts is a data
    migration + auth rewrite, not also a schema-design exercise done under
    pressure. A single "owner" row is seeded on boot (see bootstrap.py) so
    existing personal data has somewhere to point its owner_id at.
    """

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False)
    display_name = Column(String, nullable=True)
    role = Column(String, nullable=False, default="owner")  # owner, member, etc. (future use)
    created_at = Column(DateTime, default=datetime.utcnow)


class Profile(Base):
    """A library, not an account. Multiple profiles can exist under the one
    login this app has today (see routers/deps.py) -- each is a fully
    isolated set of series/books, scoped by the profile_id column on those
    tables below. Deliberately a separate table from `users` rather than a
    rename of it: `users` is reserved for real future per-account logins,
    and `owner_user_id` here (unused/unenforced for now, like `owner_id`
    above) is the eventual attachment point -- when real accounts exist, a
    user's profiles are just `WHERE owner_user_id = current_user.id`, no
    further schema change needed.
    """

    __tablename__ = "profiles"

    id = Column(String, primary_key=True)
    display_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_default = Column(Boolean, default=False)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    # Stamped only on successful completion of a Full Auto Discovery sweep
    # (services/auto_discovery.py) -- i.e. the sweep finished iterating every
    # eligible series, even if individual series checks hit provider/LLM
    # errors along the way. Never stamped at job start, so an
    # interrupted/crashed sweep doesn't accidentally burn the cooldown.
    last_full_discovery_run_at = Column(DateTime, nullable=True)


class Series(Base):
    __tablename__ = "series"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # default="robbie" (not just the migration's backfill) so that any code
    # path constructing a Series/Book without an explicit profile_id --
    # notably the many pre-existing unit tests that build these rows
    # directly -- still gets a valid row instead of a NOT NULL failure.
    # Every real request path (routers/*, importer, agents/series_agent.py)
    # always passes profile_id explicitly; this default only ever matters
    # for code that predates profiles entirely.
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True, default="robbie")

    # Core
    name = Column(String, nullable=False)
    author = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    genre = Column(String, nullable=True)
    tags = Column(JSON, nullable=True)  # list of strings

    # Structure
    is_finished = Column(Boolean, default=False)
    total_books = Column(Integer, nullable=True)
    books_in_series = Column(JSON, nullable=True)  # list of book IDs
    series_status = Column(String, default="unknown")  # ongoing, completed, unknown

    # Intelligence
    next_unread_book_number = Column(Float, nullable=True)
    next_upcoming_book_number = Column(Float, nullable=True)
    missing_books = Column(JSON, nullable=True)  # list of book_numbers
    last_checked = Column(Date, nullable=True)
    has_new_books = Column(Boolean, default=False)
    has_unread_books = Column(Boolean, default=False)
    has_upcoming_books = Column(Boolean, default=False)
    is_caught_up = Column(Boolean, default=False)
    title_normalization_mode_override = Column(String, nullable=True)

    # External IDs
    goodreads_series_id = Column(String, nullable=True)
    storygraph_series_id = Column(String, nullable=True)

    # Importer metadata
    import_source = Column(String, nullable=True)
    import_raw_headers = Column(JSON, nullable=True)
    import_raw_row = Column(JSON, nullable=True)
    import_errors = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    books = relationship("Book", back_populates="series")

    @property
    def _active_books(self):
        return [book for book in (self.books or []) if str(book.record_status or "active") != "deleted"]

    @property
    def has_new_available_books(self):
        """True if Check Now discovered a book that's out now but not yet
        read -- distinct from has_unread_books, which also counts books you
        added yourself. Normally clears once the book is marked read (see
        crud.books._should_clear_ghost_flags), but that flag-clearing step
        can be skipped by write paths that update is_read without going
        through it (bulk syncs, older data, etc.) -- checking is_read here
        directly makes this self-healing instead of trusting the flag was
        cleared correctly everywhere it could be set."""
        return any(bool(book.is_missing) and not bool(book.is_read) for book in self._active_books)

    @property
    def has_new_upcoming_books(self):
        """True if Check Now discovered an announced future release. See
        has_new_available_books for why is_read is also checked directly
        rather than relying solely on the ghost flag being cleared."""
        return any(
            (bool(book.is_upcoming_auto) or bool(book.is_upcoming_final)) and not bool(book.is_read)
            for book in self._active_books
        )

    @property
    def series_state(self):
        return {
            "has_new_books": bool(self.has_new_books),
            "has_new_available_books": self.has_new_available_books,
            "has_new_upcoming_books": self.has_new_upcoming_books,
            "has_unread_books": bool(self.has_unread_books),
            "has_upcoming_books": bool(self.has_upcoming_books),
            "is_caught_up": bool(self.is_caught_up),
        }

    @property
    def discovery_health(self):
        """Derived badge state for `last_checked` -- "never_checked",
        "healthy", "stale", or "very_stale" (see services/discovery_health.py
        for the actual thresholds). A real @property rather than a stored
        column, like series_state above, so it can never drift out of sync
        with last_checked/is_finished; consumed automatically by any
        response schema with from_attributes=True (e.g. SeriesListItem) via
        plain attribute access. Finished series should be greyed out/
        suppressed in the UI regardless of what this returns -- see the
        Discovery Health Indicator spec, §1.
        """
        return compute_discovery_health(self.last_checked, bool(self.is_finished))

    @property
    def read_count(self):
        active_books = [book for book in (self.books or []) if str(book.record_status or "active") != "deleted"]
        return sum(1 for book in active_books if bool(book.is_read))

    @property
    def unread_count(self):
        active_books = [book for book in (self.books or []) if str(book.record_status or "active") != "deleted"]
        return sum(1 for book in active_books if not bool(book.is_read))


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # See Series.profile_id above for why this has a default.
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True, default="robbie")

    # Core identity
    title = Column(String, nullable=False)
    # Provider-resolved title, written only by FIND binding, discovery
    # persistence, or bulk re-resolution -- never by direct user input. NULL
    # means unresolved. `title` itself stays the user's original entry
    # forever (including marketing suffixes/series parentheticals), so a
    # wrong FIND match is always recoverable by re-resolving rather than by
    # the user re-typing from memory. Display/identity-matching code should
    # coalesce to canonical_title, falling back to title -- see
    # book_metadata_utils.py and services/identity.py.
    canonical_title = Column(String, nullable=True)
    author = Column(String, nullable=False)
    subtitle = Column(String, nullable=True)
    series_id = Column(Integer, ForeignKey("series.id"), nullable=True)
    series_order = Column(Integer, nullable=True)
    series_total_books = Column(Integer, nullable=True)
    is_series_finished = Column(Boolean, default=False)
    book_number = Column(Float, nullable=True)  # supports 0.5, etc.
    # Where title/author/isbn13 came from -- "user" (typed manually, FIND
    # declined/unavailable), "provider" (FIND resolved + bound), "import"
    # (spreadsheet), "discovery" (Check Now), or NULL (legacy row, unknown
    # origin). A row is "verified" iff this is "provider" or "discovery" --
    # deliberately derived rather than a separate stored boolean, so the two
    # can never drift out of sync. Governs title/author/isbn13 as a group
    # (not per-field) because a single FIND call guarantees those three came
    # from the same underlying volume -- see services/identity.py's group
    # integrity discussion.
    metadata_source = Column(String, nullable=True)
    # Where book_number came from -- "user", "provider" (a structured hint
    # from FIND/Check Now), "title_inferred" (parsed from title text), or
    # NULL. Separate from metadata_source because the number's origin is
    # genuinely independent of the volume identity's origin -- a user can
    # type book_number for a title FIND resolved, or vice versa.
    book_number_source = Column(String, nullable=True)
    # True only when this row's metadata_source="provider" bind was made
    # against a low-confidence FIND candidate -- i.e. it's provider-sourced
    # (verified, not down-weighted, not excluded from discovery) but should
    # be re-checked once provider catalogs fill in further. Never set for
    # metadata_source in (discovery, user, import, NULL): discovery is
    # already provider-sourced by construction and exempt from FIND
    # confidence entirely, and the other three are already reachable via the
    # "unverified" branch of any bulk re-resolution query. Cleared back to
    # False whenever bulk re-resolution finds a confident replacement match
    # or a fresh manual FIND bind supersedes it -- see services/
    # metadata_provenance.py.
    needs_reresolution = Column(Boolean, nullable=True)

    # Publishing
    format = Column(String, nullable=True)
    publication_date = Column(Date, nullable=True)
    publisher = Column(String, nullable=True)
    edition = Column(String, nullable=True)
    pages = Column(Integer, nullable=True)
    language = Column(String, nullable=True)
    release_date = Column(Date, nullable=True)

    # Identifiers
    isbn = Column(String, nullable=True)
    isbn13 = Column(String, nullable=True)
    asin = Column(String, nullable=True)
    google_books_id = Column(String, nullable=True)
    goodreads_id = Column(String, nullable=True)
    storygraph_id = Column(String, nullable=True)

    # The retailer/catalog page this book was discovered from (e.g. an
    # Amazon listing), if any. Surfaced in the UI as a "check online" link
    # so the user can verify details (like an unconfirmed release date)
    # themselves rather than the app scraping retailer pages to extract it.
    source_url = Column(String, nullable=True)

    # User reading data
    is_read = Column(Boolean, default=False)
    read_date = Column(Date, nullable=True)
    date_added = Column(Date, nullable=True)
    date_started = Column(Date, nullable=True)
    date_finished = Column(Date, nullable=True)
    read_status = Column(String, nullable=True)  # unread, reading, read, abandoned
    rating = Column(Integer, nullable=True)
    external_rating = Column(Float, nullable=True)
    external_rating_count = Column(Integer, nullable=True)
    review = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    tags = Column(JSON, nullable=True)  # list of strings

    # Intelligence
    is_upcoming_auto = Column(Boolean, default=False)
    is_upcoming_final = Column(Boolean, default=False)
    is_missing = Column(Boolean, default=False)
    record_status = Column(String, default="active")  # active, archived, deleted

    # Importer metadata
    import_source = Column(String, nullable=True)
    import_raw_headers = Column(JSON, nullable=True)
    import_raw_row = Column(JSON, nullable=True)
    import_errors = Column(JSON, nullable=True)

    # Auto summary (you already had)
    auto_summary = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    series = relationship("Series", back_populates="books")

    @property
    def series_name(self):
        return self.series.name if self.series else None

    @property
    def display_title(self):
        """canonical_title when present, falling back to the user's
        original title -- see canonical_title's own docstring above.
        Read-only convenience for server-side code; the API response
        already exposes both raw columns (see schemas.BookBase) so a
        frontend can apply this same fallback itself where needed."""
        canonical = str(self.canonical_title or "").strip()
        return canonical or self.title


class SeriesSkeleton(Base):
    """Durable "memory" of a series' book lineup, decoupled from any single
    Check Now run -- the piece the old discovery pipeline never had (its
    only durable state was the summary fields on Series itself, like
    missing_books/last_checked, not a per-book record with confidence or
    provenance). Phase 1 only: this table is deterministically backfilled
    from existing Book rows (see services/skeleton_store.py) with zero LLM
    involvement and no behavior change anywhere else yet.

    skeleton_json entries already carry confidence/status/sources fields
    that Phase 1 only ever sets to "confirmed"/"high"/a "library" source,
    ahead of when later phases (delta checks, agentic Tier 2 discovery)
    actually need them -- reshaping this JSON structure once real rows
    exist would otherwise require migrating every row's content, not just
    adding a column.

    One row per series (series_id is the primary key) since there's
    exactly one skeleton per series -- no separate surrogate id needed.
    """

    __tablename__ = "series_skeleton"

    series_id = Column(Integer, ForeignKey("series.id"), primary_key=True)

    # List of dicts, one per known book number:
    #   book_number, title, status ("confirmed" | "unconfirmed" | "upcoming"),
    #   confidence ("high" | "medium" | "low"), release_date (ISO string or
    #   None), edition_hints, sources ([{provider, url, fetched_at}]),
    #   first_seen_at, last_confirmed_at (both ISO strings).
    skeleton_json = Column(JSON, nullable=False, default=list)

    schema_version = Column(Integer, nullable=False, default=1)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    series = relationship("Series")


class Notification(Base):
    """Minimal "New Books Added to Library" notification (Auto Discovery MVP
    spec, §3). Deliberately not a full inbox -- one row per triggering
    event (new available book created, or an upcoming->available
    transition), created by the same low-level persistence path that both
    manual Check Now and the batch Full Auto Discovery sweep share (see
    services/series_check_engine.py), so neither path needs its own copy of
    this logic. `dismissed_at` is set in bulk (dismiss-all), not
    per-notification, matching the "single modal, manual dismiss" UX in the
    spec.
    """

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    book_id = Column(Integer, ForeignKey("books.id"), nullable=True)
    series_id = Column(Integer, ForeignKey("series.id"), nullable=True)
    kind = Column(String, nullable=False, default="new_book")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    dismissed_at = Column(DateTime, nullable=True)
