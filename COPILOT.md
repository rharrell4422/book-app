# Book App — Copilot Context
@workspace


## Rookie‑Mode Guidance (Strict)

- No jargon. Use plain English and **short, step-by-step instructions**.
- Steps should be concise: do X, then Y, then Z. Only add brief “why” notes when truly helpful.
- Always state the exact file path for any code change (e.g., backend/models.py, backend/importer.py, app/books/page.tsx).
- Never provide code that requires pasting into nested structures or partial blocks.
- Only provide code in clearly defined sections with explicit START/END markers that I can paste safely.
- Never assume I know which file needs modification. Always specify the file and location.

## File Location & Search Guidance

- When referring to existing code (e.g., “find the line that looks like X”), always:
  - Name the **most likely file(s)** where that code lives (e.g., backend/intelligence.py, backend/schemas.py, app/series/page.tsx).
  - Mention whether it’s **backend (Python/FastAPI)** or **frontend (Next.js/.tsx)**.
  - If multiple files are possible, list them explicitly so I know where to look first.
- Avoid vague “find something that looks like…” instructions without file hints.
- When using error output or stack traces, map the error back to the **specific file and function** where the fix should be applied.



## Safety Rules
- Always check existing file contents before refactors.
- Never overwrite large sections without confirmation.
- When chat restarts, I paste COPILOT.md.

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
