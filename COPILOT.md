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




## Copilot Chat Setup (Every Session)
1. **Load Context**: User includes `@workspace` + `#COPILOT.md` in first message
2. **Read This File**: Copilot reads COPILOT.md to understand project state, backend/frontend structure, and approval workflow
3. **Use Workspace Files**: All code decisions based on actual workspace files, not conversation history
4. **Track with Memory**: Use `/memories/session/` for in-progress notes and `/memories/repo/` for verified codebase facts

## Safety Rules
- Always check existing file contents before refactors.
- Never overwrite large sections without confirmation.
- When chat restarts, user pastes COPILOT.md.
- Copilot should provide a brief plain-English summary of the planned change before applying it.
- Copilot should not dump command output or code diffs on screen unless user explicitly asks.
- Copilot must wait for my approval before applying any file change.
- Copilot must confirm before making any destructive change (schema edits, importer rewrites, intelligence logic changes).

## Change Approval Workflow
1. **Brief Summary First**: Present a short plain-English description of what will change and which file(s) are affected.

2. **Create Approval Button**: Use `vscode_askQuestions` tool to display interactive approval dialog with:
   - Header: "Approve Code Change"
   - Question: "Ready to apply changes to [filename]?"
   - Options: 
     - ✅ Allow Changes (marked recommended)
     - ❌ Cancel

3. **Apply Only After Approval**: Once user clicks "Allow Changes" button, apply the approved changes.

4. **Verify Changes**: After applying, read back the affected lines (read_file) to confirm changes took effect

5. **Never Ask "Should I proceed?"** — Always use vscode_askQuestions button-based approval instead of chat questions

6. **Multi-File Changes**: If multiple independent edits needed, use `multi_replace_string_in_file` tool to apply all at once after approval

## Copilot Tool Reference
| Tool | Purpose | When to Use |
|------|---------|------------|
| `read_file` | Read existing code | Understand current state before changes |
| `replace_string_in_file` | Edit single section | Apply changes after approval |
| `multi_replace_string_in_file` | Edit multiple files | Apply batch changes after approval |
| `vscode_askQuestions` | Get user confirmation | Approval workflow for code changes |
| `memory` (session) | Track progress | Note issues, debugging context, plans |
| `memory` (repo) | Store verified facts | Confirmed timings, API endpoints, working commands |
| `run_in_terminal` | Execute commands | Tests, API checks, terminal operations |
| `semantic_search` | Find code patterns | Locate functions, imports, patterns across workspace |
| `grep_search` | Find exact text | Search within specific files for strings |

## Context Persistence Strategy
- **Session Memory** (`/memories/session/`): In-progress debugging, current issues, temporary notes
  - Used when: Working through a problem that spans multiple turns
  - Cleared after session ends
  
- **Repo Memory** (`/memories/repo/`): Verified facts about codebase
  - Used when: Discovered something that will help future sessions (e.g., "timeout values that work", "where the API key is read")
  - Persists across sessions
  
- **Copilot.md**: This file = source of truth for project architecture, workflows, and current feature status
  - Update after completing major work
  - Document known limitations, next steps


## Project Overview
Backend: FastAPI + SQLite  
Frontend: Next.js + Tailwind + ShadCN  
Importer: Spreadsheet → normalized DB  
Intelligence Engine: series logic + release logic  
Release Engine: metadata + upcoming detection  

## Backend Structure
main.py, models.py, schemas.py, database.py  
crud/books.py, crud/series.py  
importer/importer.py, intelligence.py  

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

### Git Commit Workflow
- When committing, assume only `.py` and `.tsx` files should be staged unless Copilot explicitly says otherwise.
- User performs all staging manually in the VS Code Source Control panel.
- After staging, user will commit and push files to GitHub.
- Before user commits, Copilot provides a short commit summary message for the commit text.
- Once user says they are keeping file changes, Copilot must list any changed files that are not `.py` or `.tsx` and call out whether they should also be staged.
- If there are no non-`.py`/`.tsx` files that should be included, Copilot must explicitly say to stage all changed `.py` and `.tsx` files.
- Copilot must provide a brief commit message that summarizes the workflow and work completed.

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

