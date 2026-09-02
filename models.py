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
    Index,
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
    # CR-10: this used to also carry default="robbie", so any code path
    # that omitted profile_id (a bug -- every real request path already
    # passes it explicitly: routers/*, importer, agents/series_agent.py)
    # silently attributed the row to Robbie's profile instead of failing
    # loudly. That silent fallback masked at least one real production bug
    # (see services/series_check_engine.py and
    # tests/test_intelligence_profile_scoping.py's "ghost row" case) and is
    # now gone -- an omitted profile_id is a NOT NULL failure (this column
    # was already nullable=False at the DB level; only the ORM-side
    # in-Python default is what's been removed here), same as any other
    # required column. Test call sites that used to rely on the default
    # now pass profile_id explicitly instead (see the call-site audit this
    # ticket names).
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

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
    # See Series.profile_id above (CR-10) for why this has no ORM-side
    # default -- an omitted profile_id here is a NOT NULL failure now, not
    # a silent fallback to Robbie's profile.
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)

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
    # Availability axis -- independent of the reading axis above (is_read/
    # read_status). "upcoming" (not yet released), "available" (released,
    # not yet owned/downloaded), "owned" (the user has their own copy).
    # Deliberately separate from read_status: a book can be "available" (a
    # digital copy exists that the user hasn't downloaded) AND "unread" at
    # the same time -- collapsing both into one field is exactly the bug
    # that prompted this column (see the "Two-Axis Status Architecture"
    # design chat). read_status/is_upcoming_auto/is_upcoming_final are kept
    # in sync from this + is_read as a legacy bridge for not-yet-migrated
    # consumers -- see services/availability_bridge.py.
    availability_status = Column(String, default="available")  # upcoming, available, owned
    # True once a user (manual edit, CSV import with an explicit token) or
    # a self-heal rule has authoritatively set availability_status --
    # discovery/Check Now and library_sync may only write availability_status
    # while this is False (see services/availability_bridge.py and
    # library_sync.update_from_series), so a user's explicit choice can
    # never be silently overwritten by a later automated run.
    availability_locked = Column(Boolean, default=False)
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
    provenance).

    Two write paths, both in services/skeleton_store.py, merge into this
    row asymmetrically rather than overwrite it (see that module's
    docstring for the full rule and the retention/concurrency policy):
    `backfill_skeleton_for_series` rebuilds the library-sourced entries
    fresh from Book rows every call, and `apply_skeleton_updates` applies
    agent-returned findings post-persistence (Phase 1; a no-op today since
    no agent exists yet in Phase 0, but the call site and its concurrency
    protection are already live -- see series_check_engine.py).

    skeleton_json entries carry confidence/status/sources/source_class
    fields -- for a library-sourced entry these are always
    "confirmed"/"high"/a "library" source/"library"; a `source_class:
    "discovered"` entry (Phase 1, agent-written) additionally carries
    first_seen_at/last_confirmed_at timestamps used by the TTL retention
    policy for entries never upgraded to owned.

    One row per series (series_id is the primary key) since there's
    exactly one skeleton per series -- no separate surrogate id needed.
    """

    __tablename__ = "series_skeleton"

    series_id = Column(Integer, ForeignKey("series.id"), primary_key=True)

    # List of dicts, one per known book number:
    #   book_number, title, status ("confirmed" | "unconfirmed" | "upcoming"),
    #   confidence ("high" | "medium" | "low"), release_date (ISO string or
    #   None), edition_hints, sources ([{provider, url, fetched_at}]),
    #   source_class ("library" | "discovered" -- see skeleton_store.py's
    #   asymmetric merge rule; missing on schema_version 1 rows == "library"),
    #   first_seen_at, last_confirmed_at (both ISO strings), isbn13 (string or
    #   None -- additive, LitRPG-discovery-plan field; absent on rows written
    #   before it existed, so always read with .get("isbn13"), never assume
    #   presence).
    skeleton_json = Column(JSON, nullable=False, default=list)

    # FIX-SS-ENUM: matches services/skeleton_store.SCHEMA_VERSION (bumped
    # when source_class was added); see the 5414264c11af migration for the
    # server-side default + backfill of pre-existing schema_version=1 rows.
    schema_version = Column(Integer, nullable=False, default=2)

    # CR-4: optimistic-concurrency token. `_upsert_skeleton_row`
    # (services/skeleton_store.py) reads this alongside skeleton_json,
    # then writes conditioned on `version == <value it read>`, incrementing
    # it by 1. Two concurrent UPDATEs (e.g. a boot-time backfill sweep
    # racing an in-flight Check Now's post-persistence apply) used to both
    # succeed with no version check at all -- the second commit silently
    # clobbered the first's merge_fn result with one computed from a
    # now-stale read. A version mismatch now makes the losing writer's
    # UPDATE affect zero rows, which is treated as a conflict and retried
    # against a fresh read, same as the existing IntegrityError/
    # OperationalError retry path.
    version = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    series = relationship("Series")


class SeriesFingerprint(Base):
    """Durable per-series *identity/pattern* memory -- a narrow, additive
    companion to `SeriesSkeleton`, not a replacement or a second copy of
    it (see `discovery_agentic_fingerprint_recommendation.md` for the full
    ten-round design chain this table implements).

    Boundary, stated once here since it governs everything else about this
    table: `SeriesSkeleton` remains the sole owner of titles, numbering,
    status, confidence, sources, and gaps -- one row per known book.
    `SeriesFingerprint` owns exactly the four signals that have no home on
    `SeriesSkeleton` at all: who this series' author might also be
    credited as, what branding noise this series' catalog listings tend
    to carry, which providers' hits for *this* series have historically
    turned out trustworthy, and how far apart this series' volumes tend to
    release. One row per series (`series_id` is the primary key, same
    shape as `SeriesSkeleton`) -- there is exactly one fingerprint per
    series, same as there is exactly one skeleton.

    Two independent write paths, both in `services/fingerprint_store.py`,
    mirroring `SeriesSkeleton`'s own Builder/Consumer split:
      - Builder (`apply_fingerprint_updates`): runs post-round, after
        fusion/delta/confidence/persistence, from the same
        `services/series_check_engine.py` call site that already calls
        `apply_skeleton_updates`. Merges this round's observations
        (`result["fingerprint_updates"]`, computed purely inside
        `agents/series_agent.py` with no DB access of its own) into the
        durable row.
      - Consumer (`get_effective_fingerprint`): read once per job, before
        confidence scoring, gated by `settings.FINGERPRINT_INFLUENCE_ENABLED`
        + `settings.is_fingerprint_activated(series_id)` -- a dedicated
        two-tier gate, deliberately *not* the pre-existing
        `AGENTIC_ROUTING_ENABLED`/`is_agentic_activated` pair, which gates
        an unrelated subsystem (the agentic promotion evaluator) and does
        not govern the always-live `confidence_engine.py` code path this
        table feeds. The fingerprint itself is always built (shadow-first,
        zero cost -- it only reads this round's already-computed
        delta/confidence output); the gate controls only whether
        `confidence_engine.compute_confidence` is ever handed a non-`None`
        `fingerprint` argument.

    `fingerprint_json` shape (a single flat dict, not a list of entries --
    unlike `SeriesSkeleton.skeleton_json`, there is no per-book axis here):
        {
          "author_aliases": [str, ...],
          "naming_patterns": [str, ...],
          "provider_bias": {provider_name: float, ...},   # 0.5-1.5 multiplier
          "release_cadence": {
              "mean_interval_days": float | None,
              "stddev_interval_days": float | None,
              "interval_count": int,
          },
        }
    Every field is model-agnostic and provider-agnostic -- plain strings,
    floats, and counts, never an LLM-specific shape -- per the design
    chain's item 4 (model-agnostic schema).
    """

    __tablename__ = "series_fingerprint"

    series_id = Column(Integer, ForeignKey("series.id"), primary_key=True)

    fingerprint_json = Column(JSON, nullable=False, default=dict)

    # Matches services/fingerprint_store.SCHEMA_VERSION.
    schema_version = Column(Integer, nullable=False, default=1)

    # Optimistic-concurrency token, same role as SeriesSkeleton.version --
    # see services/fingerprint_store._upsert_fingerprint_row.
    version = Column(Integer, nullable=False, default=0, server_default="0")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    series = relationship("Series")


class Notification(Base):
    """Durable series-level discovery notification (see the "Durable
    Series-Level Discovery Notifications" design chat's finalized spec).

    Originally a per-book table (one row per triggering event, kind=
    "new_book") for the old DB-driven popup. That popup is retired --
    notifications are now aggregated one row per series per discovery run
    (kind="series_discovery_delta"), written once at the end of
    services/series_check_engine.py's run_series_check_job_full after
    counting that run's brand-new inserts plus upcoming->available
    transitions for the series being checked. `count_new_books` and
    `series_name` are only populated for that new kind; legacy "new_book"
    rows (retired via migration d... -- see its dismissed_at backfill) are
    filtered out of the durable Notifications view by kind, not deleted.
    `dismissed_at` supports both a per-row dismiss (the Notifications view)
    and a bulk dismiss-all, unlike the old single-modal-only bulk dismiss.

    `book_titles_json` (added in the "Durable Notifications: Count Fix +
    Title List + Dedupe" follow-up) is a compact `[{"title": str, "status":
    "available"|"upcoming"}, ...]` list -- one entry per *distinct* book
    (by id) that contributed to `count_new_books` for this run, deduped so
    a book that flips upcoming->available mid-run (across the multi-round
    catch-up loop) appears once, with its last-known status. Nullable so
    pre-existing rows written before this column existed remain valid;
    application code always populates it for any row written going
    forward. Strictly informational, like the rest of this table -- no
    confidence/provider/candidate-review fields, which stay exclusive to
    SeriesCandidateNotification below.
    """

    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    book_id = Column(Integer, ForeignKey("books.id"), nullable=True)
    series_id = Column(Integer, ForeignKey("series.id"), nullable=True)
    kind = Column(String, nullable=False, default="new_book")
    # Nullable at the DB level so legacy "new_book" rows (which never had
    # these) remain valid -- application code guarantees both are always
    # populated for any row written with kind="series_discovery_delta".
    count_new_books = Column(Integer, nullable=True)
    series_name = Column(String, nullable=True)
    book_titles_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    dismissed_at = Column(DateTime, nullable=True)


class SeriesCandidateNotification(Base):
    """"Review Candidate Book" notifications (see the LitRPG Enhanced
    Discovery design chat's finalized spec) -- one durable row per
    ambiguous/low-confidence discovery candidate that today's routing in
    `agents/series_agent.py` sends to the `needs_review` bucket (the
    `low_confidence_ambiguous and (overall_grade in {"medium", None})`
    branch). That branch no longer appends to `needs_review`/writes a
    SeriesSkeleton entry directly -- this table is its sole replacement,
    giving the human a durable, actionable (Add to Series / Review / Do
    Not Add) surface instead of a candidate that only ever lived inside
    one `Check Now` response and an unconfirmed skeleton row.

    `series_id` is nullable for a hypothetical future standalone-candidate
    path (mirrors `Notification.series_id`); every current write site
    always has a series.

    Identity/dedupe columns (`isbn13`, `title_key`, `bare_title_key`,
    `candidate_number`) mirror the normalized-identity comparisons
    `agents/series_agent._is_known_candidate` already uses for owned
    books, reused here (see `services/candidate_notifications.py`) so the
    same candidate rediscovered on a later run refreshes `last_seen_at`
    in place instead of creating a duplicate row, and so a candidate
    explicitly dismissed via "Do Not Add" (`resolution="ignored"`) never
    resurfaces as a new row either -- both checks match on this same
    normalized identity, not on literal title/ASIN string equality (raw
    provider titles for the same book vary run to run).

    `resolution` distinguishes *why* a row is no longer active from
    `dismissed_at`-style single-state tables like `Notification`: `None`
    (unresolved/pending), `"added"` (persisted as a real Book row), or
    `"ignored"` (permanently suppressed, never recreated). `resolved_at`
    is stamped for both terminal states.
    """

    __tablename__ = "series_candidate_notifications"

    id = Column(Integer, primary_key=True, index=True)
    profile_id = Column(String, ForeignKey("profiles.id"), nullable=False, index=True)
    series_id = Column(Integer, ForeignKey("series.id"), nullable=True, index=True)
    series_name = Column(String, nullable=True)

    candidate_title = Column(String, nullable=False)
    candidate_number = Column(Float, nullable=True)
    overall_confidence = Column(String, nullable=True)
    provider_confidence = Column(String, nullable=True)
    isbn13 = Column(String, nullable=True)
    publication_date = Column(String, nullable=True)
    asin = Column(String, nullable=True)
    author = Column(String, nullable=True)
    source_url = Column(String, nullable=True)
    provider = Column(String, nullable=True)
    # Same-author/different-series false positives (confidence_engine has
    # no series-identity dimension -- see the routing block's comment in
    # run_series_check) are common enough in this bucket that a human
    # reviewer needs this at a glance to dismiss them on sight.
    series_name_hint = Column(String, nullable=True)
    # List of strings, e.g. "number_inferred_from_title",
    # "missing_series_number" -- see services/candidate_notifications.py.
    reason_flags = Column(JSON, nullable=False, default=list)

    # Normalized-identity columns for dedupe/ignore matching -- see class
    # docstring. Always populated (possibly empty string) so an equality
    # filter never has to special-case NULL.
    title_key = Column(String, nullable=False, default="", index=True)
    bare_title_key = Column(String, nullable=False, default="")

    # None = unresolved/pending, "added" = persisted as a Book row,
    # "ignored" = permanently suppressed via "Do Not Add".
    resolution = Column(String, nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    # Step 8 (Tier C Shadow Scoring Persistence + Promotion Path): set only
    # when this row was created/refreshed while the series' Tier C
    # promotion state was "shadow_advisory" AND the Tier C shadow call for
    # this same candidate disagreed with the deterministic gate on
    # belongs_to_series (see agents/series_agent.py's shadow call site).
    # Purely informational -- never read by any routing/dedupe logic in
    # this table, only surfaced to a human reviewer. Null for every row
    # written before this column existed and for every row where no
    # disagreement occurred (i.e. almost all rows -- shadow_only is the
    # only state anything will realistically be in until an explicit
    # promotion policy exists).
    tier_c_disagreement = Column(JSON, nullable=True)


class AgenticSkeletonPreview(Base):
    """Phase 2 dual-write shadow table (`discovery_agentic_phase1_plan.md`/
    `discovery_agentic_phase1_evaluation.md`'s settled architecture, not
    re-litigated here): one row per dry-run turn, storing the Phase 1
    shadow loop's (`agents/agentic_series_agent.run_agentic_turn`)
    `skeleton_merge_previews` output for a series.

    Purely diagnostic/side-channel, same as every other Phase 1/2 agentic
    artifact -- entirely separate from `SeriesSkeleton.skeleton_json`
    (the live, routing-relevant table). Written only by
    `services/agentic_skeleton_preview_store.py`, which never touches
    `SeriesSkeleton`, and read only by that same module's `get_agentic_
    skeleton_previews` and `/admin/agentic/previews/{series_id}`.

    One row per turn rather than one row per series (unlike
    `SeriesSkeleton`, which is one row per series) -- multiple previews
    accumulate over time so the admin endpoint can show how the shadow
    loop's preview drifts (or doesn't) across repeated dry runs, hence
    the surrogate `id` primary key instead of `series_id` alone.
    """

    __tablename__ = "agentic_skeleton_previews"

    id = Column(Integer, primary_key=True)
    series_id = Column(Integer, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    preview_json = Column(JSON, nullable=False)


class AgenticConfidenceDecision(Base):
    """Phase 2 dual-write shadow table (final Phase 2 scaffolding block,
    same settled architecture as `AgenticSkeletonPreview` above -- see
    that class's docstring, not re-litigated here): one row per (series,
    book_number, dry-run turn), pairing the live pipeline's confidence
    for that book against the Phase 1 shadow loop's
    (`agents/agentic_series_agent.run_agentic_turn`) `confidence_traces`
    entry for the same book, at the moment of that turn.

    Purely diagnostic/side-channel -- entirely separate from whatever
    drives live routing (`SeriesSkeleton.skeleton_json`'s `confidence`
    field, computed by `confidence_engine.py`). Written only by
    `agentic/confidence_gate_store.py`, which never touches
    `SeriesSkeleton` or `confidence_engine.py`, and read only by that
    same module's `get_agentic_confidence_history` and `/admin/agentic/
    confidence/{series_id}`.

    One row per turn (not per series, and not per series+book_number
    unique) for the same reason as `AgenticSkeletonPreview`: history
    accumulates across repeated dry runs so the admin endpoint can show
    whether live/agentic confidence drift over time, hence the surrogate
    `id` primary key. `live_confidence`/`agentic_confidence` are stored
    as opaque JSON blobs (whatever shape the caller passes in) rather
    than individual scalar columns, matching every other Phase 1/2
    shadow-diagnostic table's "store the trace as-is" convention.
    """

    __tablename__ = "agentic_confidence_decisions"
    __table_args__ = (
        Index("ix_agentic_confidence_decisions_series_id_book_number", "series_id", "book_number"),
    )

    id = Column(Integer, primary_key=True)
    series_id = Column(Integer, index=True)
    book_number = Column(Float, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    live_confidence = Column(JSON, nullable=False)
    agentic_confidence = Column(JSON, nullable=False)


class AgenticGateDecision(Base):
    """Phase 2 dual-write shadow table -- the `belongs-to-series` gate
    counterpart to `AgenticConfidenceDecision` immediately above (see
    that class's docstring for the shared rationale, not repeated here):
    one row per (series, book_number, dry-run turn), pairing the live
    pipeline's gate outcome for that book against the Phase 1 shadow
    loop's `gate_traces` entry for the same book.

    Written only by `agentic/confidence_gate_store.py`
    (`store_agentic_gate`), which never touches `SeriesSkeleton` or the
    live `evaluate_belongs_to_series_gate` logic in `agents/series_
    agent.py`, and read only by that same module's `get_agentic_gate_
    history` and `/admin/agentic/gate/{series_id}`.
    """

    __tablename__ = "agentic_gate_decisions"
    __table_args__ = (Index("ix_agentic_gate_decisions_series_id_book_number", "series_id", "book_number"),)

    id = Column(Integer, primary_key=True)
    series_id = Column(Integer, index=True)
    book_number = Column(Float, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    live_gate = Column(JSON, nullable=False)
    agentic_gate = Column(JSON, nullable=False)


class AgenticPromotionDecision(Base):
    """Phase 3 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`'s settled architecture, not re-litigated here):
    the first table recording an actual *routing* decision rather than a
    purely diagnostic side-by-side comparison -- one row per (series,
    book_number, live routing turn) recording not just the live vs
    agentic confidence/gate pair (same shape as `AgenticConfidenceDecision`/
    `AgenticGateDecision` above), but also `promotion_outcome`: which of
    the two `agentic.promotion_evaluator.evaluate_promotion`
    actually chose for that book -- `"use_live"`, `"use_agentic"`, or
    `"reject_agentic"`.

    Written only by `agentic/promotion_evaluator.py`
    (`store_promotion_decision`), and only from `agents/series_agent.py`'s
    live routing path (gated by `settings.AGENTIC_ROUTING_ENABLED`) --
    never from the Phase 1/2 dry-run block, which has its own separate
    `AgenticConfidenceDecision`/`AgenticGateDecision` tables and never
    calls the promotion evaluator. Read only by that same module's
    `get_promotion_history` and `/admin/agentic/promotion-history/
    {series_id}`.

    One row per turn (not unique per series+book_number) for the same
    reason as every other Phase 1/2/3 shadow/decision table: history
    accumulates across repeated live routing turns, hence the surrogate
    `id` primary key and the composite `(series_id, book_number)` index
    for lookups rather than a uniqueness constraint on it.
    """

    __tablename__ = "agentic_promotion_decisions"
    __table_args__ = (
        Index("ix_agentic_promotion_decisions_series_id_book_number", "series_id", "book_number"),
    )

    id = Column(Integer, primary_key=True)
    series_id = Column(Integer, index=True)
    book_number = Column(Float, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    live_confidence = Column(JSON, nullable=False)
    agentic_confidence = Column(JSON, nullable=False)

    live_gate = Column(JSON, nullable=False)
    agentic_gate = Column(JSON, nullable=False)

    promotion_outcome = Column(String, nullable=False)


class ShadowLLMCall(Base):
    """Step 8 (Tier C Shadow Scoring Persistence + Promotion Path): one
    durable row per Tier C shadow LLM call made in `agents/series_agent.py`'s
    classification loop -- the persisted counterpart to Step 7's per-run,
    in-memory-only `DiscoveryTelemetry.tier_c_shadow_scores`/
    `record_tier_c_shadow_score`. Turns Tier C from a per-run diagnostic
    into a cross-run, queryable signal (accuracy over time, cost over
    time) without changing what's scored or how -- every field below is
    produced by the existing `_score_tier_c_shadow_response`/gate
    computation at that call site; this table only stores those outputs,
    it never recomputes them (Step 8 architectural diff, section 1.2).

    Written only from the Tier C shadow call site, immediately after
    `_score_tier_c_shadow_response` succeeds (i.e. only when the shadow
    call itself returned a response -- a failed/timed-out shadow call has
    nothing to score and gets no row here, same gating as `record_tier_c_
    shadow_score`). Write failures are fail-soft (logged, never raised)
    -- same dual-write convention as `agentic/confidence_gate_store.py`
    and friends: a persistence hiccup must never sink the Check Now run
    it's shadowing.

    `run_id` (Step 8 diff, revision 2/section 1.1) is minted once per
    Check Now job in `services/series_check_engine.run_series_check_job_
    full`, alongside `telemetry`/`discovery_cache`, and threaded into
    `series_agent.run_series_check` as a parameter -- NOT regenerated per
    round, since a single job's multiple catch-up rounds must share one
    durable identifier (mirrors how `telemetry` is already cumulative
    across rounds for the whole job).

    `series_id` is NOT NULL: the only call site that exists today always
    has a concrete series (Step 8 diff, section 1.3). A future call site
    that fires before a `Series` row exists should add its own nullable
    field (e.g. `candidate_series_name`) rather than relaxing this
    constraint.
    """

    __tablename__ = "shadow_llm_calls"

    id = Column(Integer, primary_key=True)
    series_id = Column(Integer, ForeignKey("series.id"), nullable=False, index=True)
    run_id = Column(String, nullable=False, index=True)
    tier = Column(String, nullable=False, default="C")

    # Gate context: the deterministic gate's already-computed decision for
    # this same candidate, at the moment the shadow call was made.
    gate_belongs_to_series = Column(Boolean, nullable=False)
    gate_inferred_number = Column(Integer, nullable=True)
    gate_confidence = Column(String, nullable=True)  # confidence_engine overall_grade bucket

    # Shadow output: the Tier C response itself, normalized via
    # llm_client.LLMResponse and parsed by _score_tier_c_shadow_response.
    shadow_provider = Column(String, nullable=False)
    shadow_model_id = Column(String, nullable=False)
    shadow_belongs_to_series = Column(Boolean, nullable=True)  # null if unparseable
    shadow_inferred_number = Column(Integer, nullable=True)
    shadow_confidence = Column(String, nullable=True)
    # Recorded, never compared -- the gate has no alternate-title concept
    # (see _score_tier_c_shadow_response's docstring).
    shadow_is_alternate_title_of_known_book = Column(Boolean, nullable=True)

    # Scoring: _score_tier_c_shadow_response's derived comparison output,
    # stored as-is (see class docstring, section 1.2 -- single source of
    # truth, never recomputed here).
    parsed_ok = Column(Boolean, nullable=False)
    belongs_to_series_agreement = Column(Boolean, nullable=True)
    inferred_number_agreement = Column(Boolean, nullable=True)
    confidence_aligned = Column(Boolean, nullable=True)

    # Cost and meta.
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_cost_usd = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class TierCPromotionState(Base):
    """Step 8 (Tier C Shadow Scoring Persistence + Promotion Path):
    current Tier C promotion state for one series -- `series_id` is the
    "route" (Step 8 diff, revision 1/section 1.1 dropped the standalone
    "route" concept: this codebase has no routing unit distinct from a
    series, so promotion state is keyed directly on `series_id`).

    One row per series (PK = `series_id`), current-state only -- no
    transition history in Phase 8a (an optional `tier_c_promotion_
    transitions` log is explicit Phase 8b future work, see the Step 8
    diff's section 2.2). Mirrors `SeriesSkeleton`/`SeriesFingerprint`'s
    existing one-row-per-series shape rather than bolting another nullable
    column onto `Series` itself.

    Absence of a row for a given `series_id` means `"shadow_only"` --
    every series defaults to shadow_only until a row is explicitly created
    (see `services/tier_c_shadow_store.get_tier_c_promotion_state`).
    Phase 8a has no automated policy that ever creates/updates a row here
    (that's explicit Phase 8b future work); until one exists, the only way
    a series reaches `shadow_advisory`/`live` is a manual write to this
    table.

    States (Step 8 diff, section 2.1):
      - "shadow_only": Tier C runs in parallel, never affects routing.
      - "shadow_advisory": disagreements are recorded on
        SeriesCandidateNotification.tier_c_disagreement; deterministic
        gate still drives routing.
      - "live": Tier C's belongs_to_series decision overrides the gate's
        (deterministic gate stays computed and persisted for audit/
        fallback) for the specific ambiguous candidates Tier C already
        shadows in every other state.
    """

    __tablename__ = "tier_c_promotion_state"

    series_id = Column(Integer, ForeignKey("series.id"), primary_key=True)
    tier_c_state = Column(String, nullable=False, default="shadow_only")
    tier_c_provider = Column(String, nullable=True)
    tier_c_model_id = Column(String, nullable=True)
    last_evaluated_at = Column(DateTime, nullable=True)
