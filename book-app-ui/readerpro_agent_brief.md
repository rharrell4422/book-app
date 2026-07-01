PROJECT GOAL
Add an Intelligence Agent to ReaderPro that automates book discovery, metadata extraction, and book creation — replacing the clunky scraping + manual “use intelligence” workflow.
---
WHAT THE AGENT MUST DO
1. Generate search queries automatically
	◦ Based on author, series, missing book numbers, release patterns.
2. Fetch search results (NOT scrape Amazon/Google)
	◦ Use search APIs or simple text fetches.
	◦ Even partial/messy text is fine.
3. Extract meaningful text
	◦ Titles
	◦ Authors
	◦ Series
	◦ Book numbers
	◦ Release dates
	◦ Metadata
	◦ Summaries
4. Interpret the text using an LLM
	◦ Turn messy search results into structured book objects.
5. Create books automatically
	◦ Use your existing create_book_from_intelligence() logic.
6. Run on-demand
	◦ No cloud hosting yet.
	◦ Triggered manually from the UI.
---
ARCHITECTURE DECISIONS (LOCKED IN)
• Agent runs inside FastAPI backend.
• Agent is a Python class inside a new agents/ folder.
• Agent uses OpenAI Python SDK (or Anthropic if preferred).
• Agent uses a reasoning loop (perception → reasoning → action).
• Agent does NOT scrape Amazon/Google directly.
• Agent uses search APIs or simple text fetches.
• Agent integrates with your existing book creation pipeline.
• Agent runs locally on your Mac.
• No cloud queues, workers, or hosting until later.
---
TECH STACK (LOCKED IN)
• FastAPI backend
• Next.js frontend
• PostgreSQL database
• OpenAI Python SDK
• Copilot Chat inside VS Code for coding
• This chat for architecture guidance
FOLDER STRUCTURE (PLANNED)
backend/
  agents/
    book_agent.py
  routes/
    agent_routes.py
  services/
    intelligence_service.py
  core/
    search_api.py
    text_extraction.py
CURRENT STEP
Step 1:
Set up the agent environment in your FastAPI backend:
• Install OpenAI Python SDK
• Create agents/ folder
• Create book_agent.py with a basic agent class
• Add placeholder methods for:
	◦ generate_search_queries
	◦ fetch_text
	◦ interpret_text
	◦ create_book
When you get back from dinner, we’ll start this step.
---
NEXT STEPS (ROADMAP)
Step 2: Build the reasoning loop
Step 3: Integrate search API
Step 4: Integrate text extraction
Step 5: Integrate interpretation (LLM)
Step 6: Integrate book creation
Step 7: Build FastAPI route
Step 8: Build Next.js UI trigger
Step 9: Test locally
Step 10: Add optional background tasks
---
RULES FOR THIS CHAT
• You paste ONLY this brief when restarting.
• You do NOT paste code here.
• You do NOT debug code here.
• You do NOT fight indentation here.
• You ask VS Code Copilot Chat to write/modify code.
• You use this chat ONLY for architecture, strategy, and next steps.