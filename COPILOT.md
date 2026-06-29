# Book App — Copilot Context
@workspace


## Rookie‑Mode Guidance (Strict)

- No jargon. Use plain English and **short, step-by-step instructions**.
- Steps should be concise: do X, then Y, then Z. Only add brief “why” notes when truly helpful.
- Always state the exact file path for any code change (e.g., backend/models.py, backend/importer.py, app/books/page.tsx).
- Never provide code that requires pasting into nested structures or partial blocks.
- Only provide code in clearly defined sections with explicit START/END markers that I can paste safely.
- Never assume I know which file needs modification. Always specify the file and location.
- - Never rewrite entire files unless I explicitly request it; only provide targeted snippets with START/END markers.


## File Location & Search Guidance

- When referring to existing code (e.g., “find the line that looks like X”), always:
  - Name the **most likely file(s)** where that code lives (e.g., backend/intelligence.py, backend/schemas.py, app/series/page.tsx).
  - Mention whether it’s **backend (Python/FastAPI)** or **frontend (Next.js/.tsx)**.
  - If multiple files are possible, list them explicitly so I know where to look first.
- Avoid vague “find something that looks like…” instructions without file hints.
- When using error output or stack traces, map the error back to the **specific file and function** where the fix should be applied.
- - When suggesting edits, Copilot must reference the exact function or component name where the change belongs.




## Safety Rules
- Always check existing file contents before refactors.
- Never overwrite large sections without confirmation.
- When chat restarts, I paste COPILOT.md.
- Copilot must show the diff for any proposed file change before applying it.
- Copilot must wait for my approval before applying any file change.
- Copilot must confirm before making any destructive change (schema edits, importer rewrites, intelligence logic changes).

## Change Approval Workflow
1. **Show Diff First**: Present changes in clear markdown diff format with file name, lines added (green +), lines deleted (red -).
2. **Create Approval Button**: Use vscode_askQuestions tool to display an interactive approval dialog with:
   - ✅ Allow Changes (recommended)
   - ❌ Cancel
3. **Apply Only After Approval**: Once user clicks "Allow Changes", apply changes via replace_string_in_file.
4. **Verify Changes**: Read back the affected lines to confirm changes took effect.
5. **Never ask "Should I proceed?"** — always use the button-based approval instead.


## Project Overview
Backend: FastAPI + SQLite  
Frontend: Next.js + Tailwind + ShadCN  
Importer: Spreadsheet → normalized DB  
Intelligence Engine: series logic + release logic  
Release Engine: metadata + upcoming detection  

## Backend Structure
main.py, models.py, schemas.py, database.py  
crud/books.py, crud/series.py  
importer/excel_importer.py, intelligence.py  

## Frontend Structure
app/books/page.tsx  
app/series/page.tsx  
app/series/[seriesId]/page.tsx  
components/ui/*  

---

# 📅 Development Roadmap
1. Backend stabilization  
2. Importer alignment  
3. Database rebuild  
4. Intelligence engine  
5. Release monitoring  
6. Frontend integration  

---

# 🛠 Backend Stabilization
### Models & Schemas
- Align Book + Series models with importer output.
- Keep extended metadata fields hidden from UI.

### Importer
- Header mapping  
- Normalize all fields  
- Parse dates, status, book numbers  
- Detect missing + upcoming  
- Produce JSON‑safe metadata

### Database Rebuild
- Delete books.db only  
- Restart FastAPI  
- Re-import full spreadsheet  
- Validate counts, normalization, duplicates

### Intelligence Engine
- Next Unread  
- Next Upcoming  
- Missing Books  
- Total Books  
- Finished  
- Last Checked  
- Inconsistency detection

### Release Monitoring
- Detect upcoming releases  
- Auto-tag upcoming  
- Store release dates  
- Metadata fetcher validation

### API Stability
- Books API predictable  
- Series API predictable  
- Sorting rules stable  

---

# 🧠 Series Intelligence Logic
### Next Unread
Lowest unread book; None if finished or none exist.

### Next Upcoming
Lowest upcoming book; always sorted to top.

### Missing Books
Known total − imported.

### Total Books
Importer + metadata.

### Series Finished
All read AND no upcoming AND no future releases AND no missing.

### Last Checked
Updated on “Check Now” or automated agent.

### Inconsistency Detection
- Out-of-order numbers  
- Duplicates  
- Missing numbers  
- Series name mismatches  
- Metadata conflicts  

### Intelligence Output
- Next Unread  
- Next Upcoming  
- Missing Books  
- Total Books  
- Finished  
- Last Checked  
- Inconsistency Flags  

---

# 🧑‍💻 Developer Rules
### Terminal Usage
- Terminal 1: FastAPI server  
- Terminal 2: installs, scripts, DB resets  
- Never mix them.
- Copilot must follow the roadmap sequence and never jump ahead (e.g., no frontend changes before backend stabilization).


### Editing Rules
Stop FastAPI before editing:
- models  
- schemas  
- importer  
- database logic  

### Database Rules
- books.db = primary  
- wal/shm = normal  
- never delete wal/shm  
- delete books.db only during planned rebuilds  

### File Management
Keep open: main.py, models.py, schemas.py, importer.py, intelligence.py, database.py, Books/Series pages.

### Refactor Steps
1. Stop FastAPI  
2. Edit  
3. Save  
4. Restart FastAPI  
5. Test endpoints  
6. Test importer  
7. Test intelligence  

### Copilot Interaction
- Always start chats with @workspace + #COPILOT.md  
- Never ask for full file pastes  
- Request only needed snippets  
- Follow roadmap order  
## Copilot Workspace Rules
- Copilot must always load @workspace and #COPILOT.md at the start of every session.
- Copilot must use the actual workspace files as the source of truth.
- Copilot must not rely on past chat history.
- All architectural decisions, schema definitions, and importer logic live in this COPILOT.md.
## Development Roadmap (Condensed)
1. Backend: FastAPI + SQLite schema finalized.
2. Importer: Rebuild importer logic to match schema.
3. Frontend: Next.js + Tailwind + ShadCN UI.

---

# 📚 Book Search & Suggestions Feature

## Current Status (Completed Work)
- ✅ Sequential fetching for missing book suggestions (no parallel timeouts)
- ✅ Frontend timeout increased to 90 seconds (AbortSignal.timeout(90000))
- ✅ Backend httpx timeouts increased to 30 seconds (search_google_books, search_openlibrary)
- ✅ CORS middleware reconfigured (explicit origins, headers, max_age=3600)
- ✅ Schema serialization updated (BookBase includes is_read, external_rating, external_rating_count)
- ✅ No 500 errors or CORS errors (all timeouts/errors fixed)

## External API Integration

### Endpoint: GET `/books/suggest`
**Parameters:**
- `series_name` (required): Series title to search for
- `book_number` (optional): Specific book number in series (#1, #2, etc.)
- `author` (optional): Author name for filtering

**Response:**
```json
{
  "query": "final query executed",
  "results": [
    {
      "title": "Book Title",
      "author": "Author Name",
      "year": "2024",
      "description": "...",
      "source_url": "...",
      "series_name": "Series Name",
      "series_position": 1,
      "source": "google_books" | "openlibrary"
    }
  ]
}
```

### Search Strategy
1. Try Google Books API with multiple query variations (title, title+author, title+number, etc.)
2. If Google fails or returns empty, try OpenLibrary with candidate queries
3. If still empty and author provided, try author-only fallback searches
4. Return first matching results or empty array

### Current Limitation: Rate Limiting & Missing Data
**Google Books API (Primary)**
- Returns 429 (Too Many Requests) without API key
- High request quota available with authenticated API key
- Indie/self-published books may not be indexed

**OpenLibrary API (Fallback)**
- Free, no authentication required
- Indie/self-published LitRPG books NOT indexed
- Example searches that fail: "Unbound" series, "1% Lifesteal" series

**Indie Book Limitation**
User's library contains self-published LitRPG books not in standard databases:
- "Unbound" by Nicoli Gonnella
- "1% Lifesteal" by Robert Blaise
- "The Harry Starke Novels" by Blair Howard

## Next Steps: Google Books API Setup (REQUIRED)

### Prerequisites
User must set up Google Books API key:

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create new project (name: "Book App")
3. Enable "Google Books API"
4. Create API key (type: Restricted)
   - Restrict to Books API only
   - Restrict to Application Type: None (unrestricted)
5. Copy the API key

### Configuration
Add to FastAPI server environment:
```bash
export GOOGLE_BOOKS_API_KEY="YOUR_API_KEY_HERE"
uvicorn main:app --reload
```

Or add to `.env` file and load in main.py.

### Code Location
Environment variable is read by `intelligence.py` line ~135:
```python
api_key = get_google_books_api_key()
if api_key:
    params["key"] = api_key
```

## Timeouts & Configuration Reference
**Frontend** (book-app-ui/app/series/[seriesId]/page.tsx):
- Sequential fetch loop (lines 69-79)
- Per-request timeout: 90 seconds
- Max concurrent: 1 (sequential only)

**Backend** (intelligence.py):
- Google Books httpx timeout: 30 seconds (line 145)
- OpenLibrary httpx timeout: 30 seconds (line 196)
- Fallback retries with stripped/modified queries

**Middleware** (main.py):
- CORS allowed origins: http://localhost:3000, http://127.0.0.1:3000
- CORS max_age: 3600 seconds
- Allowed methods: GET, POST, PUT, PATCH, DELETE, OPTIONS
4. Intelligence Engine: Series detection, new-book detection, manual "Check Now" feature.
5. Git/GitHub: Version control active and required for all changes.
## Personal Context for Copilot
- User: Robbie Harrell
- Location: Big Creek, GA
- Goal: Build full Book App web version to replace spreadsheet.
- Skills: Beginner in FastAPI, Next.js, Tailwind, ShadCN, Git.
- Preferences: Extremely explicit instructions, calm sequencing, no jargon.
- - Copilot must maintain continuity across sessions using COPILOT.md as the single source of truth.

