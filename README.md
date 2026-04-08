# Sidekick

Submission for **Hack2Skill Google GenAI APAC Cohort 1**.

Sidekick is a conversational assistant that helps you stay on top of **tasks**, **calendar-style plans**, and **notes** in one place. You chat in your browser; the assistant can organize information for you and, when you connect your Google account, work with familiar Google products while keeping a consistent backup in a database.

### Live app

The deployed instance is available at **[https://sidekick.amngupta.com](https://sidekick.amngupta.com)** (same host as the default `SIDEKICK_RESOURCE_LABEL` in this repo). If you move hosting, update this link in the README.

---

## Google Cloud and Google products used

| Area | What we use |
|------|----------------|
| **Compute** | **[Cloud Run](https://cloud.google.com/run)** — runs the container (Flask UI + ADK); `K_SERVICE` / `TRUST_PROXY_HEADERS` in `.env.example` match Cloud Run’s HTTPS proxy behavior. |
| **Database** | **[AlloyDB for PostgreSQL](https://cloud.google.com/alloydb)** — Google's managed **PostgreSQL**-compatible database; primary datastore via `DATABASE_URL` or the **[AlloyDB Auth Proxy / Connector](https://cloud.google.com/alloydb/docs/connect-connectors)** (`ALLOYDB_*` in `.env.example`). All application tables live in this database (see diagram below). |
| **AI / ML** | **[Vertex AI](https://cloud.google.com/vertex-ai)** with **Gemini** (`GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`) for the ADK agents and for natural-language → UTC time parsing in schedule tools. |
| **Agents** | **[Google Agent Development Kit (ADK)](https://google.github.io/adk-docs/)** — multi-agent orchestration (`LlmAgent`, tools, MCP). |
| **Models** | **[Gemini 2.5 Flash](https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash)** — 2.5 Flash is best for large scale processing, low-latency, high volume tasks that require thinking, and agentic use cases. |
| **Identity & APIs** | **Google OAuth 2.0 / OpenID Connect** — sign-in and offline refresh tokens stored per user. Optional product APIs: **[Calendar](https://developers.google.com/calendar)**, **[Tasks](https://developers.google.com/tasks)**, **[Keep](https://developers.google.com/workspace/keep/api)** (via `google-api-python-client`). |
| **Observability** | ADK can send **trace** / **OpenTelemetry** data to Google Cloud when `ADK_TRACE_TO_CLOUD` / `ADK_OTEL_TO_CLOUD` are enabled. |

Locally or on other hosts you can still use **any PostgreSQL** (not only AlloyDB) via `DATABASE_URL`, or call Gemini with **`GOOGLE_API_KEY`** instead of Vertex when not using `GOOGLE_GENAI_USE_VERTEXAI`.

---

## In plain language

### What you can do

- **Tasks** — Add or list things to do. With Google Tasks enabled, items can appear in your Google Tasks list; the app can also keep its own copy for reference.
- **Schedule** — Describe meetings or blocks of time in everyday words (“tomorrow at 3pm for an hour”). The assistant turns that into precise times and can create calendar entries when Google Calendar is connected.
- **Notes** — Capture short notes or reference text. With Google Keep enabled, notes can live in Keep as well as in the app’s records.

### How it knows it’s you

With **OAuth configured** (the intended production setup), you **must** complete **Sign in with Google** before the chat UI or `/api` agent calls work. Flask ties your session to your Google account; the proxy injects your **`sub`** into ADK so tools read and write rows scoped by **`owner_sub`** in AlloyDB. **The chat does not show which Google user “owns” each task in the UI**—that separation is enforced in the database and API, not as labels inside the conversation.

If OAuth client env vars are **omitted** (developer “open” mode), the SPA treats you as signed-in and the server does not gate `/api`; that is not how the deployed app behaves.

### What “Sidekick” leaves behind in Google

Items the assistant creates are tagged with a small **label** (configurable) so you can search for them in Google and so “show me everything from Sidekick” stays accurate. See `SIDEKICK_RESOURCE_LABEL` in `.env.example` for operators.

---

## Diagrams

### Big picture (what talks to what)

**Production (OAuth on):** visitors without a Google session only use **static, public pages** from Flask. The **assistant, AlloyDB-backed tools, and `/api`** run only **after** sign-in.

```mermaid
flowchart TB
  subgraph Public["No Google sign-in — public only"]
    direction TB
    V[Visitor browser]
    V --> Home[Homepage / — header + sign-in prompt + footer]
    V --> Priv[Privacy policy /privacy-policy]
    V --> Tos[Terms /terms-and-conditions]
  end

  subgraph Authenticated["After Google sign-in"]
    direction TB
    B[Signed-in browser]
    subgraph App["Sidekick app e.g. Cloud Run"]
      Web[Flask session + OAuth + /api proxy]
      Brain[ADK assistant runtime]
    end
    subgraph AlloyDB["AlloyDB PostgreSQL — rows keyed by owner_sub"]
      direction TB
      T[sidekick_tasks]
      C[sidekick_calendar_events]
      N[sidekick_notes]
      O[sidekick_google_oauth]
    end
    subgraph GoogleAPIs["Google APIs optional"]
      Tasks[Tasks]
      Cal[Calendar]
      Keep[Keep]
      Vertex[Vertex AI Gemini]
    end
    B --> Web
    Web --> Brain
    Brain --> T
    Brain --> C
    Brain --> N
    Brain --> O
    Brain -.-> Tasks
    Brain -.-> Cal
    Brain -.-> Keep
    Brain --> Vertex
  end

  Home -.->|Sign in with Google| Web
```

- **Public** block: no chat form, no agent, no AlloyDB access from the browser—only **homepage shell**, **Privacy**, and **Terms** (plus theme toggle and links). `/login/google` starts OAuth; it does not by itself expose the assistant.
- **Authenticated** block: same runtime as before—**`owner_sub`** scopes data per user server-side; the UI does not print user ids on individual tasks.
- **Dashed** edges to **Tasks / Calendar / Keep**: product APIs when enabled and consented. **Vertex AI**: model + schedule parsing when configured (`.env.example`).

Solid lines are core architecture for signed-in use. Dashed lines are optional Google product APIs or the transition from landing page to signed-in app.

### Your journey as a user

**Production:** the landing page has **no chat** until you **Sign in with Google**; after that you can talk to the assistant.

Sidekick can reach **Google** in three ways: **OAuth 2.0 / OpenID Connect** (sign-in, refresh tokens, and user profile), the **Google Calendar API** (for Google Calendar events), the **Google Tasks API** (for Google Tasks), the **Google Keep API** (for Google Keep), and **Vertex AI** for **Gemini** (orchestrating agents and natural-language time parsing).

```mermaid
flowchart TD
  A([Open Sidekick]) --> B[Landing page — no chat yet]
  B --> C[Sign in with Google]
  C --> D[You can use the chat]
  D --> E[You send a message]
  E --> F[ADK: Gemini on Vertex AI + coordinator and specialists]
  F --> DB[(AlloyDB for your owner_sub)]
  F -.-> APIs[Google Calendar Events, Tasks, Keep — when a tool updates them]
  DB --> R[Assistant reply in chat — only after tools finish, including any Google API updates]
  APIs -.-> R
  R --> D
```

- **Landing:** no composer until you are signed in (see the **Public** block in the big-picture diagram).
- **Each turn:** specialists may write to **AlloyDB** and, when integrations are on, call **Calendar / Tasks / Keep**; the next message appears **after** those tool calls complete. The chat UI does **not** show user ids on each item.
- **Full inventory:** a single user message can trigger **`list_sidekick_inventory`** plus **three** specialist interpretation passes (tasks, then calendar, then notes) before the final reply—see the inventory sequence diagram below.

### How the assistant is organized (conceptual)

Agents run **after** sign-in; tools use **`owner_sub`** for AlloyDB. **Google product APIs** are optional (dashed). **Gemini on Vertex AI** powers the LLM agents.

```mermaid
flowchart TB
  subgraph Agents["ADK agents — Gemini on Vertex AI"]
    Coordinator[SidekickCoordinator]
    TaskSpec[TaskSpecialist]
    SchedSpec[ScheduleSpecialist]
    NotesSpec[NotesSpecialist]
    InvTool[list_sidekick_inventory shared tool]
    Coordinator -->|delegate usual sub-request| TaskSpec
    Coordinator -->|delegate usual sub-request| SchedSpec
    Coordinator -->|delegate usual sub-request| NotesSpec
    Coordinator --> InvTool
    TaskSpec --> InvTool
    SchedSpec --> InvTool
    NotesSpec --> InvTool
  end

  TaskSpec --> TT[Task tools DB and/or Google Tasks]
  SchedSpec --> ST[Schedule tools DB Calendar API time helper]
  NotesSpec --> NT[Notes tools DB and/or Keep]

  subgraph AlloyDB["AlloyDB — all tool rows keyed by owner_sub"]
    direction TB
    TB[(sidekick_tasks)]
    CB[(sidekick_calendar_events)]
    NB[(sidekick_notes)]
    OB[(sidekick_google_oauth)]
  end

  TT --> TB
  ST --> CB
  NT --> NB
  InvTool --> TB
  InvTool --> CB
  InvTool --> NB
  TT -.-> OAuthUse[Uses tokens from]
  ST -.-> OAuthUse
  NT -.-> OAuthUse
  OAuthUse --> OB

  TT -.-> GTasks[Google Tasks API]
  ST -.-> GCal[Google Calendar API]
  NT -.-> GKeep[Google Keep API]
```

The **coordinator** usually routes to **one specialist** per sub-request. For a **full Sidekick inventory** (list everything across tasks, calendar, and notes), it calls **`list_sidekick_inventory`**, summarizes, then **transfers in order** to **TaskSpecialist → ScheduleSpecialist → NotesSpecialist** so each domain interprets its slice and the coordinator synthesizes next actions (suggestions only unless the user asked to change data). **Specialists also have `list_sidekick_inventory`** when they need cross-domain context on other turns. Specialists call **database tools** and, when OAuth scopes and APIs allow, **Google** tools; **`sidekick_google_oauth`** holds refresh tokens for those calls.

### ADK agent architecture (as implemented)

This is the “wiring diagram” of what actually runs in this repo: `main.py` starts an internal ADK FastAPI server, the Flask app proxies `/api/*` to it, injects the signed-in user’s `sub` as ADK `user_id`, and the ADK root agent (`SidekickCoordinator`) delegates with **`transfer_to_agent`**—usually **one specialist** per sub-request, except for a **full inventory** turn where Root runs **`list_sidekick_inventory`** then chains **Task → Schedule → Notes** before synthesizing.

```mermaid
flowchart TB
  Browser["Browser SPA"]
  subgraph Runtime["Sidekick runtime (one container)"]
    direction TB
    Flask["Flask app + OAuth + /api proxy<br/>main.py"]
    ADK["ADK FastAPI server<br/>get_fast_api_app"]
    subgraph Graph["ADK agent graph<br/>sidekick/agent.py"]
      Root["SidekickCoordinator<br/>root_agent"]
      Task["TaskSpecialist"]
      Sched["ScheduleSpecialist"]
      Notes["NotesSpecialist"]
      Inv["list_sidekick_inventory<br/>on Root + each specialist"]
      Root -->|transfer_to_agent| Task
      Root -->|transfer_to_agent| Sched
      Root -->|transfer_to_agent| Notes
      Root --> Inv
      Task --> Inv
      Sched --> Inv
      Notes --> Inv
    end
  end

  subgraph Tools["Tool backends"]
    direction TB
    DB["PostgreSQL / AlloyDB<br/>owner_sub row ownership"]
    Vertex["Vertex AI Gemini<br/>LLM + time parsing helper"]
    GTasks["Google Tasks API"]
    GCal["Google Calendar API"]
    GKeep["Google Keep API"]
    MCP["MCP toolsets (optional)<br/>SIDEKICK_MCP_*"]
  end

  Browser -->|static pages + OAuth| Flask
  Browser -->|/api proxy| Flask
  Flask -->|rewrite user path + user_id in run body| ADK
  ADK --> Root

  Task --> DB
  Sched --> DB
  Notes --> DB
  Inv --> DB

  Sched --> Vertex
  Task -.->|when enabled| GTasks
  Sched -.->|when enabled| GCal
  Notes -.->|when enabled| GKeep
  Task -.->|optional| MCP
  Sched -.->|optional| MCP
  Notes -.->|optional| MCP
```

**Full inventory flow:** Root calls **`list_sidekick_inventory`** (reads tasks, calendar events, and notes from DB and/or Google APIs), then **`transfer_to_agent`** in order: **Task → Schedule → Notes** for domain interpretation, then Root synthesizes. On other turns, Root typically transfers to **one** specialist; any specialist may call **`list_sidekick_inventory`** for cross-domain context.

### ADK request flow (one chat turn)

```mermaid
sequenceDiagram
  participant UI as Browser UI
  participant Web as Flask /api proxy
  participant ADK as ADK FastAPI
  participant Root as SidekickCoordinator
  participant Spec as Specialist agent
  participant DB as AlloyDB/Postgres
  participant G as Google APIs (optional)

  UI->>Web: POST /api/.../run (message)
  Note right of Web: If OAuth on: require session. Inject user_id = user_sub.
  Web->>ADK: Forward rewritten request
  ADK->>Root: Run root_agent
  Root->>Spec: transfer_to_agent (Tasks/Schedule/Notes)
  Spec->>DB: Tool calls (CRUD rows for owner_sub)
  Spec-->>G: Tool calls (Calendar/Tasks/Keep) when enabled
  Spec-->>Root: Tool results
  Root-->>ADK: Final response text
  ADK-->>Web: HTTP response (JSON)
  Web-->>UI: Proxy response
```

### ADK flow: full Sidekick inventory + specialist interpretation

When the user asks to list everything Sidekick-tagged across tasks, calendar, and notes, the coordinator calls **`list_sidekick_inventory`** once, then transfers to each specialist in order so they interpret their slice (recommendations only unless the user asked for changes); the coordinator then synthesizes.

```mermaid
sequenceDiagram
  participant UI as Browser UI
  participant Web as Flask /api proxy
  participant ADK as ADK FastAPI
  participant Root as SidekickCoordinator
  participant Inv as list_sidekick_inventory
  participant Task as TaskSpecialist
  participant Sched as ScheduleSpecialist
  participant Notes as NotesSpecialist
  participant DB as AlloyDB/Postgres
  participant G as Google APIs (optional)

  UI->>Web: POST /api/.../run (inventory message)
  Web->>ADK: Forward with user_id
  ADK->>Root: Run root_agent
  Root->>Inv: Tool call (combined inventory JSON)
  Inv->>DB: Read tasks / events / notes (and/or Google-backed paths)
  Inv-->>G: List Tasks/Calendar/Keep when APIs enabled
  Inv-->>Root: JSON payload
  Root->>Task: transfer_to_agent (interpret tasks section)
  Task-->>Root: Domain notes / suggestions
  Root->>Sched: transfer_to_agent (interpret calendar_events section)
  Sched-->>Root: Domain notes / suggestions
  Root->>Notes: transfer_to_agent (interpret notes section)
  Notes-->>Root: Domain notes / suggestions
  Root-->>ADK: Summary + synthesized next actions
  ADK-->>Web: HTTP response
  Web-->>UI: Proxy response
```

---

## For developers

- **Run locally:** configure environment from `.env.example`, install dependencies (e.g. `uv sync`), run `python main.py`.
- **OAuth vs UI:** When `GOOGLE_OAUTH_CLIENT_ID` / `SECRET` are set, `static/index.html` hides the chat until `/auth/me` shows a signed-in user and returns **401** on `/api` without a session—matching the public-vs-authenticated diagram above. Omit those vars only for local open testing.
- **Code map:** `main.py` serves the UI and proxies the agent API; `sidekick/agent.py` defines the multi-agent graph; `sidekick/db.py` handles the database; Google integrations live in `sidekick/google_*` modules.

Python modules include **module and function docstrings** describing behavior and configuration hooks.

## Legal and policy pages

**Privacy Policy** and **Terms of Service** are always reachable without signing in (`/privacy-policy`, `/terms-and-conditions`). On the **homepage** (`/`), visitors without a session still see the header and footer (including those links) but **not** the chat composer—that stays behind the login wall until Google sign-in completes (`static/index.html`).
