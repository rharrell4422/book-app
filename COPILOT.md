# Book App — Copilot Context

@workspace

**Guidance to Copilot For Me Being a Rookie:**  
- Don't assume I know programmer language or Jargon
- Don't assume I know when to switch from FE/BE server path to root path
- Don't assume I know what file you are in based on code structure..tell me which .py, .tsx. or other
- Don't give me file snippets to paste into current code
- I cannot figure out how to indent and paste properly
- I can do paste separe sections, put sections in code with descriptions and I can replace sections  

## Backend Files
#file: main.py
#file: models.py
#file: schemas.py
#file: crud/__init__.py
#file: crud/books.py
#file: crud/series.py
#file: database.py
#file: importer.py
## Importer Files
#file: importer/__init__.py
#file: importer/excel_importer.py
#file: intelligence.py
#file: debug_scrape.py
#file: test.py
#file: requirements.txt

## Frontend Files
#file: book-app-ui/app
#file: book-app-ui/components
#file: book-app-ui/lib
#file: book-app-ui/public
#file: book-app-ui/.gitignore
#file: book-app-ui/AGENTS.md
#file: book-app-ui/CLAUDE.md
#file: book-app-ui/components.json
#file: book-app-ui/eslint.config.mjs
#file: book-app-ui/next-env.d.ts
#file: book-app-ui/next.config.ts
#file: book-app-ui/package-lock.json
#file: book-app-ui/package.json
#file: book-app-ui/postcss.config.mjs
#file: book-app-ui/README.md
#file: book-app-ui/tsconfig.json
#file: book-app-ui/app/books/page.tsx
#file: book-app-ui/app/series/page.tsx
#file: book-app-ui/app/series/[seriesId]/page.tsx
#file: book-app-ui/app/layout.tsx
#file: book-app-ui/app/page.tsx
#file: book-app-ui/app/providers.tsx
#file: book-app-ui/app/globals.css

#file: book-app-ui/components/ui/button.tsx
#file: book-app-ui/components/ui/card.tsx
#file: book-app-ui/components/ui/dialog.tsx
#file: book-app-ui/components/ui/input.tsx
#file: book-app-ui/components/ui/label.tsx
#file: book-app-ui/components/ui/spinner.tsx
#file: book-app-ui/components/ui/table.tsx
#file: book-app-ui/components/ui/toast.tsx
#file: book-app-ui/components/ui/toaster.tsx
#file: book-app-ui/components/ui/use-toast.tsx

---

## 📘 Product Requirements Summary

**Purpose:** Replace manual spreadsheet tracking with a web‑based app powered by Agentic AI.  
**Problem:** Manual entry and searching for new releases, missing books, and series completion.  
**Audience:** Initially you (Robert); future external users possible.  
**Tactical Goals:**  
- Replace 2,200‑row spreadsheet.  
- Automate book and series tracking.  
- Provide fast search, filtering, and series intelligence.  
- Eliminate manual release checks.  
- Stabilize backend and importer.  

**Strategic Goals:**  
- Support extended metadata.  
- Enable customizable views.  
- Prepare for multi‑user and cloud sync.  
- Expand metadata sources (Goodreads → Google Books → Kindle Unlimited).  

---

## 🧩 Key Requirements

**Book‑Level:**  
Title, Author, Read Status, Date Read, Book Number, Series Name, Next Release Date, Summary/Recap  

**Series‑Level:**  
Series Name, Author, Total Known Books, Missing Books, Next Unread, Next Upcoming, Release Dates, Finished/Unfinished, “View Books”, “Check Now”  

**Automation:**  
- Import spreadsheet automatically.  
- Detect series relationships and missing books.  
- Retrieve metadata and release dates.  
- Compute Next Unread, Next Upcoming, Missing, Total, Finished, Last Checked.  

**UI:**  
- Books Screen → Upcoming on top, Read below.  
- Series Screen → Ongoing on top, Finished below.  
- Fast search, filtering, “View Books”, “Check Now”.  

---

## 🧠 Architecture Overview

**Backend:** FastAPI + SQLite  
- Flexible models and schemas  
- Importer with header mapping  
- CRUD and API routes  
- Series Intelligence Engine  
- Release Monitoring Engine  

**Frontend:** Next.js + Tailwind + ShadCN  
- Two main screens (Books, Series)  
- Clean default views  
- Expandable columns  
- Local preferences  

---

## 📅 Feature Roadmap (Condensed)

### Phase 0 — Foundation & Stabilization
- Align models, schemas, CRUD, and API routes  
- Build importer header‑mapping and normalization  
- Rebuild database and validate importer  

**Outcome:** Stable backend that cleanly imports your 2,200‑book spreadsheet.

---

### Phase 1 — Spreadsheet Replacement (Tactical Goal)
- Books Screen: display, sort, filter, search  
- Series Screen: ongoing vs. finished, “View Books”, “Check Now”  
- Series Intelligence Engine: compute unread/upcoming/missing/finished  
- Release Monitoring Engine: detect and tag upcoming releases  
- Metadata Fetcher Agent (Goodreads v1): summaries, release dates  

**Outcome:** Spreadsheet fully replaced; manual tasks eliminated.

---

### Phase 2 — Quality of Life Enhancements
- Book Detail Page and Series Detail Page  
- Column customization (personal version)  
- Import/export improvements  

**Outcome:** More usable, flexible, and enjoyable.

---

### Phase 3 — Agentic Automation
- Automated series monitoring and library maintenance  
- Optional release prediction agent  

**Outcome:** Self‑updating, self‑maintaining library.

---

### Phase 4 — Future‑Ready Foundation
- Multi‑user architecture  
- UI customization framework  
- Metadata source expansion  

**Outcome:** Ready for commercialization if desired.

---

### Phase 5 — Commercial Edition (Optional)
- User accounts, cloud sync, notifications, mobile app, monetization  

**Outcome:** Personal app evolves into a commercial product.

---

## 🧭 Development Workflow

- **Default terminal:** BE Server  
- **General terminal:** for installs and standalone scripts  
- **Never edit backend while FastAPI is running**  
- **Use `@workspace` + `#COPILOT.md`** at start of every new chat to restore full context  

---

## ✅ Success Criteria

**Tactical:**  
- Spreadsheet replaced  
- Manual tasks eliminated  
- Series intelligence and release monitoring automated  
- Importer handles full library without errors  

**Strategic (Future):**  
- Supports external users  
- Customizable views  
- Expanded metadata  
- Multi‑user architecture  

---

## 🧩 Notes
- SQLite WAL files (`books.db‑wal`, `books.db‑shm`) are normal; do not delete.  
- Keep backend and frontend folders open in VS Code for Copilot visibility.  
- Save this file and reference it in every new chat:

## 📥 Importer Logic Summary

### Purpose
Replace manual spreadsheet entry with a robust importer that can ingest all 2,200+ books, normalize data, detect series relationships, and prepare the library for intelligence processing.

### Supported Fields (from spreadsheet)
- Title  
- Author  
- Read Status (Read / Upcoming / Unread)  
- Date Read  
- Next Release Date  
- Series Name  
- Book Number  
- Series Finished (Yes/No)  
- Any additional metadata columns  

### Header Mapping System
The importer must:
- Accept ANY spreadsheet header  
- Map headers to internal model fields  
- Normalize variations (e.g., “Series”, “Series Name”, “Saga”, “Franchise”)  
- Support future fields without breaking existing imports  

### Data Normalization Rules
- Convert “Upcoming” → status = UPCOMING  
- Convert “Read” → status = READ  
- Convert blank status → UNREAD  
- Convert date strings → Python `datetime`  
- Convert book numbers to integers  
- Trim whitespace from titles, authors, series names  
- Normalize series names (consistent casing, spacing)  

### Series Detection
Importer must:
- Identify series membership  
- Identify book number within series  
- Group books by series  
- Detect missing books  
- Detect next unread  
- Detect next upcoming  

### Error Handling
- Skip malformed rows but log them  
- Never crash on bad data  
- Provide a summary of import results  
- Allow re‑import without duplicating books  

### Output
Importer produces:
- Clean Book objects  
- Clean Series objects  
- Fully normalized database ready for intelligence engines  

## 🛠 Backend Stabilization Plan

### Phase 1 — Model & Schema Alignment
Goal: Ensure backend models match importer output and frontend expectations.

Tasks:
- Align Book model fields  
- Align Series model fields  
- Add extended metadata fields (hidden from UI)  
- Align Pydantic schemas  
- Align CRUD operations  
- Align API routes  

Outcome:
Backend models become stable and predictable.

---

### Phase 2 — Importer Alignment
Goal: Ensure importer writes clean, normalized data into the database.

Tasks:
- Build header‑mapping system  
- Normalize all spreadsheet fields  
- Validate series detection logic  
- Validate book number parsing  
- Validate status parsing  
- Validate date parsing  
- Validate missing book detection  

Outcome:
Importer becomes reliable and ready for full library import.

---

### Phase 3 — Database Rebuild
Goal: Start from a clean slate with aligned models and importer.

Tasks:
- Delete old `books.db`  
- Restart FastAPI to regenerate schema  
- Run importer on full spreadsheet  
- Validate:
  - All books imported  
  - All series detected  
  - All fields normalized  
  - No duplicates  
  - No missing required fields  

Outcome:
Stable database with clean data.

---

### Phase 4 — Intelligence Engine Alignment
Goal: Ensure Series Intelligence Engine works with normalized data.

Tasks:
- Compute Next Unread  
- Compute Next Upcoming  
- Compute Missing Books  
- Compute Total Books  
- Compute Series Finished  
- Compute Last Checked  
- Detect inconsistencies  

Outcome:
Series intelligence becomes accurate and reliable.

---

### Phase 5 — Release Monitoring Engine Alignment
Goal: Ensure release detection works with normalized series data.

Tasks:
- Detect upcoming releases  
- Detect new releases  
- Auto‑tag upcoming books  
- Store release dates  
- Validate Goodreads metadata fetcher  

Outcome:
Release monitoring becomes automated.

---

### Phase 6 — API Stability
Goal: Ensure frontend receives clean, predictable data.

Tasks:
- Validate Books API  
- Validate Series API  
- Validate intelligence fields  
- Validate upcoming sorting  
- Validate finished series sorting  

Outcome:
Frontend becomes stable and predictable.

---

### Phase 7 — Frontend Integration
Goal: Connect stable backend to Books and Series screens.

Tasks:
- Books screen  
- Series screen  
- “View Books”  
- “Check Now”  
- Search  
- Filtering  
- Sorting  

Outcome:
Spreadsheet replacement becomes fully functional.

## 🤖 Copilot Usage Guide

This section defines how Copilot should interact with the Book App project.  
Copilot must follow these rules in every chat where `#COPILOT.md` is referenced.

### 1. Workspace Context
Copilot must always load full project context using:
@workspace
#COPILOT.md

Copilot should rely on workspace files instead of asking the user to paste code.

### 2. File Referencing Rules
Copilot should reference files using:
#file: path/to/file

Copilot should NOT request entire file pastes unless absolutely necessary.  
Copilot should request only the specific function, class, or snippet required.

### 3. Backend/Frontend Awareness
Copilot must understand:
- Backend = FastAPI + SQLite
- Frontend = Next.js + Tailwind + ShadCN
- Importer = spreadsheet → normalized DB
- Intelligence Engine = series logic + release logic

Copilot should maintain separation between backend and frontend tasks.

### 4. Chat Stability Rules
Copilot must:
- Avoid rewriting entire files unless requested
- Avoid hallucinating folder structures
- Avoid assuming missing files
- Avoid suggesting changes that contradict the stabilization plan
- Use the roadmap to guide development sequence

### 5. Development Flow
Copilot should follow this order:
1. Backend stabilization  
2. Importer alignment  
3. Database rebuild  
4. Intelligence engine  
5. Release monitoring  
6. Frontend integration  

Copilot should NOT jump ahead of this sequence.

### 6. Terminal Rules
Copilot must respect:
- Default terminal = BE Server
- General terminal = installs, standalone scripts
- Never instruct backend edits while FastAPI is running

### 7. Data Safety
Copilot must NOT suggest deleting:
- books.db  
- books.db-wal  
- books.db-shm  

These files are managed by SQLite.

### 8. Copilot Behavior Expectations
Copilot should:
- Provide step-by-step instructions
- Use explicit, rookie-mode guidance
- Avoid assumptions
- Ask for specific snippets only when needed
- Maintain consistency across chats

## 🧑‍💻 Developer Rules

These rules define how development should be performed in this project.

### 1. Terminal Usage
- Terminal 1: FastAPI backend server (default)
- Terminal 2: General terminal for installs, scripts, DB resets
- Never run backend commands in the general terminal
- Never run installs in the backend terminal

### 2. Editing Rules
- Do NOT edit backend files while FastAPI is running
- Stop FastAPI before:
  - editing models
  - editing schemas
  - editing importer
  - editing database logic

### 3. Database Rules
- books.db is the primary DB
- books.db-wal and books.db-shm are normal SQLite files
- Never delete WAL/SHM files manually
- Only delete books.db when performing a planned rebuild

### 4. File Management
- Keep these files open in VS Code:
  - main.py
  - models.py
  - schemas.py
  - importer.py
  - intelligence.py
  - database.py
  - Books and Series frontend pages

Copilot uses open files for context priority.

### 5. Refactor Rules
When refactoring:
1. Stop FastAPI  
2. Make changes  
3. Save  
4. Restart FastAPI  
5. Test endpoints  
6. Test importer  
7. Test intelligence engine  

### 6. Copilot Interaction Rules
- Always start new chats with:
  @workspace
  #COPILOT.md

- Copilot should NOT ask for full file pastes
- Copilot should request only the specific snippet needed
- Copilot should follow the roadmap sequence
## 🧠 Series Intelligence Logic Specification

Defines how the Series Intelligence Engine computes all derived fields.

### 1. Next Unread
Definition:
The lowest-numbered book in the series where status == UNREAD.

Rules:
- If no unread books exist → Next Unread = None
- If series is finished → Next Unread = None

### 2. Next Upcoming
Definition:
The lowest-numbered book in the series where status == UPCOMING.

Rules:
- Upcoming books always appear at the top of the Books screen
- Upcoming books always appear at the top of the Series screen

### 3. Missing Books
Definition:
Books that exist in the series but are NOT in the library.

Rules:
- Missing = all known books - all imported books
- Missing count determines series completeness

### 4. Total Books
Definition:
Total number of books known in the series.

Rules:
- Derived from importer + metadata fetcher
- Must update when new metadata is fetched

### 5. Series Finished
Definition:
True when:
- All books in the series are read
- AND no upcoming books exist
- AND metadata indicates no future releases

Rules:
- If metadata shows future releases → Finished = False
- If missing books exist → Finished = False

### 6. Last Checked
Definition:
Timestamp of last metadata refresh.

Rules:
- Updated when user clicks “Check Now”
- Updated when automated agent runs

### 7. Series Inconsistency Detection
Copilot must detect:
- Book numbers out of order
- Duplicate book numbers
- Missing book numbers
- Series name mismatches
- Metadata conflicts

### 8. Intelligence Output
Each series must output:
- Next Unread
- Next Upcoming
- Missing Books
- Total Books
- Series Finished
- Last Checked
- Inconsistency Flags

