# Sidekick

Submission for **Hack2Skill Google GenAI APAC Cohort 1**.

Sidekick is a conversational assistant that helps you stay on top of **tasks**, **calendar-style plans**, and **notes** in one place. You chat in your browser—**type or use voice input** (after sign-in, the composer can send short recordings to **Cloud Speech-to-Text** for transcription); the assistant can organize information for you and, when you connect your Google account, work with familiar Google products while keeping a consistent backup in a database.

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
| **Identity & APIs** | **Google OAuth 2.0 / OpenID Connect** — sign-in and offline refresh tokens stored per user. Optional product APIs: **[Calendar](https://developers.google.com/calendar)**, **[Tasks](https://developers.google.com/tasks)**, **[Docs](https://developers.google.com/docs/api)** + **[Drive](https://developers.google.com/drive/api)** (via `google-api-python-client`). |
| **Speech** | **[Cloud Speech-to-Text](https://cloud.google.com/speech-to-text)** — voice dictation in the Vue chat composer: the browser records **WebM/Opus**, `POST /ui-api/speech/transcribe` forwards audio to **`SpeechClient.recognize`**, and the returned text fills the message field. Language and sample rate come from `SPEECH_LANGUAGE_CODE` and `SPEECH_SAMPLE_RATE_HERTZ` in `.env.example`. |
| **Observability** | ADK can send **trace** / **OpenTelemetry** data to Google Cloud when `ADK_TRACE_TO_CLOUD` / `ADK_OTEL_TO_CLOUD` are enabled. |

Locally or on other hosts you can still use **any PostgreSQL** (not only AlloyDB) via `DATABASE_URL`, or call Gemini with **`GOOGLE_API_KEY`** instead of Vertex when not using `GOOGLE_GENAI_USE_VERTEXAI`.

---

## In plain language

### What you can do

- **Tasks** — Add or list things to do. With Google Tasks enabled, items can appear in your Google Tasks list; the app can also keep its own copy for reference.
- **Schedule** — Describe meetings or blocks of time in everyday words (“tomorrow at 3pm for an hour”). The assistant turns that into precise times and can create calendar entries when Google Calendar is connected.
- **Notes** — Capture short notes or reference text. With Google Docs enabled, notes are stored as Google Docs (in your Drive) as well as in the app’s records.
- **Voice input** — In the signed-in chat, use the **microphone** control to dictate: the SPA streams recorded **WebM/Opus** to **`POST /ui-api/speech/transcribe`** (`sidekick/flask_speech_api.py`), Flask calls **Google Cloud Speech-to-Text**, and the transcript is dropped into the composer so you can edit and send it like a typed message.

### How it knows it’s you

With **OAuth configured** (the intended production setup), you **must** complete **Sign in with Google** before the chat UI or `/api` agent calls work. Flask ties your session to your Google account; the proxy injects your **`sub`** into ADK so tools read and write rows scoped by **`owner_sub`** in AlloyDB. **The chat does not show which Google user “owns” each task in the UI**—that separation is enforced in the database and API, not as labels inside the conversation.

If OAuth client env vars are **omitted** (developer “open” mode), the SPA treats you as signed-in and the server does not gate `/api`; that is not how the deployed app behaves.

### What “Sidekick” leaves behind in Google

Items the assistant creates are tagged with a small **label** (configurable) so you can search for them in Google and so “show me everything from Sidekick” stays accurate. See `SIDEKICK_RESOURCE_LABEL` in `.env.example` for operators.

---

## Diagrams

### Big picture (what talks to what)

**Production (OAuth on):** visitors without a Google session load the **same Vue SPA bundle** from Flask (`/`, `/privacy-policy`, `/terms-and-conditions` all serve `static/dist/index.html`); the client shows **public** chrome until sign-in. **`/api/*`** (ADK) and **`/ui-api/*`** (chats, inventory UI, speech) are **401** without a session when OAuth is configured.

```mermaid
flowchart TB
  subgraph Public["No Google sign-in — public routes in SPA"]
    direction TB
    V[Visitor browser]
    SPA[Vue SPA from Flask static/dist]
    V --> SPA
    SPA --> Home[Landing / — sign-in prompt + footer]
    SPA --> Priv[Privacy /privacy-policy]
    SPA --> Tos[Terms /terms-and-conditions]
  end

  subgraph Authenticated["After Google sign-in"]
    direction TB
    B[Signed-in browser]
    subgraph App["Sidekick app e.g. Cloud Run"]
      Web[Flask session + OAuth + /api proxy + /ui-api JSON]
      Brain[ADK assistant runtime]
    end
    subgraph AlloyDB["AlloyDB PostgreSQL — owner_sub scoped"]
      direction TB
      T[sidekick_tasks / calendar / notes]
      O[sidekick_google_oauth]
      Chat[sidekick_chats + messages + telemetry]
      Mem[sidekick_memory + grants]
    end
    subgraph GoogleAPIs["Google APIs optional"]
      Tasks[Tasks]
      Cal[Calendar]
      Docs[Docs]
      Vertex[Vertex AI Gemini]
    end
    B --> Web
    Web --> Brain
    Web --> Chat
    Web --> Mem
    Brain --> T
    Brain --> O
    Brain -.-> Tasks
    Brain -.-> Cal
    Brain -.-> Docs
    Brain --> Vertex
  end

  Home -.->|Sign in with Google| Web
```

- **Public** block: one **Vite-built** SPA; the server does not expose **`/api`** or **`/ui-api`** without a session. **`/login/google`** starts OAuth; the assistant and DB-backed UI APIs stay behind the session gate.
- **Authenticated** block: same runtime as before—**`owner_sub`** scopes data per user server-side; the UI does not print user ids on individual tasks.
- **Dashed** edges to **Tasks / Calendar / Docs**: product APIs when enabled and consented. **Vertex AI**: model + schedule parsing when configured (`.env.example`).

Solid lines are core architecture for signed-in use. Dashed lines are optional Google product APIs or the transition from landing page to signed-in app.

### Process Flow Diagram

```mermaid
flowchart TD
    A[User opens sidekick.amngupta.com] --> B{Signed in with Google?}
    B -- No --> C[Login with Google OAuth]
    C --> D[OAuth callback + user session]
    B -- Yes --> E[Vue SPA: chat resources rails]

    D --> E
    E --> F{Request type}
    F -- Chats inventory speech --> UiApi[Flask /ui-api JSON]
    F -- Agent turn --> ApiProxy[Flask /api proxy]
    UiApi --> DBui[(AlloyDB chats messages grants)]
    ApiProxy --> MemInj[Memory embed + preamble on run]
    MemInj --> G[ADK SidekickCoordinator]

    G --> H{Intent routing}
    H --> I[TaskSpecialist]
    H --> J[ScheduleSpecialist]
    H --> K[NotesSpecialist]
    H --> L[Full Inventory Flow]

    I --> TaskTools[Tasks tools]
    J --> SchedTools[Schedule tools + time sanitize to UTC]
    K --> NotesTools[Notes tools]
    L --> InvTool[list_sidekick_inventory]

    TaskTools --> Q{Google API enabled?}
    SchedTools --> R{Google API enabled?}
    NotesTools --> S{Google API enabled?}

    Q -- Yes --> T[Google Tasks update]
    Q -- No --> Uonly[DB-only task update]
    R -- Yes --> V[Google Calendar update]
    R -- No --> W[DB-only calendar update]
    S -- Yes --> X[Google Docs update]
    S -- No --> Y[DB-only notes update]

    T --> Z[AlloyDB backup sync]
    V --> Z
    X --> Z
    Uonly --> Z
    W --> Z
    Y --> Z

    Z --> AA[Unified response to UI]
    AA --> AB[User in Vue SPA: chat rail + central view]
```

### Use Case Diagram

```mermaid
flowchart LR
    U[User]
    SK[Sidekick System]
    GA[Google Account Services]
    DB[AlloyDB]
    MCP[MCP External Tools]

    U --> UC1[Sign in / Sign out]
    U --> UC2[Create task/event/note]
    U --> UC3[List all inventory]
    U --> UC4[Update/Delete items from UI]
    U --> UC5[Ask multi-step workflow in chat]
    U --> UC6[Multiple chats rename archive]
    U --> UC7[Voice dictation + optional telemetry rail]

    UC1 --> SK
    UC2 --> SK
    UC3 --> SK
    UC4 --> SK
    UC5 --> SK
    UC6 --> SK
    UC7 --> SK

    SK --> SC1[Coordinator routes to specialist]
    SK --> SC2[Execute tools]
    SK --> SC3[Return unified response]
    SK --> SC4[Persist and query structured data]
    SK --> SC5["/ui-api: chats, inventory, speech"]

    SC2 --> GA
    SC2 --> MCP
    SC4 --> DB
    SC5 --> DB
    GA --> DB
```

### Wireframe Diagram

```mermaid
flowchart TB
  U[User Browser] --> D[sidekick.amngupta.com]
  D --> CR[Cloud Run: Flask OAuth + Vue dist + /api + /ui-api + ADK]

  subgraph AGENTS[Multi-Agent Layer]
    COORD[SidekickCoordinator]
    TS[TaskSpecialist]
    SS[ScheduleSpecialist]
    NS[NotesSpecialist]
    INV[list_sidekick_inventory shared tool]
    COORD --> TS
    COORD --> SS
    COORD --> NS
    COORD --> INV
    TS --> INV
    SS --> INV
    NS --> INV
  end

  CR --> COORD

  subgraph TOOLS[Tool Execution]
    GT[Google Tasks API]
    GC[Google Calendar API]
    GD[Google Docs API]
    MCP[MCP Toolsets optional]
  end

  TS --> GT
  SS --> GC
  NS --> GD
  TS -. optional .-> MCP
  SS -. optional .-> MCP
  NS -. optional .-> MCP

  subgraph DATA[Data Layer]
    ADB[AlloyDB: tasks calendar notes oauth]
    CHAT[Chats messages telemetry memory grants]
  end

  GT --> ADB
  GC --> ADB
  GD --> ADB
  INV --> ADB
  CR --> CHAT
  CHAT --> ADB

  subgraph NET[Network + Hosting]
    VPC[Same VPC: Cloud Run <-> AlloyDB private connectivity]
  end

  CR --- VPC
  ADB --- VPC

  COORD --> OUT[Unified response to UI]
  OUT --> U
```

### Architectural Diagram

```mermaid
flowchart LR
  U[User] --> D[sidekick.amngupta.com]

  subgraph VPC[Shared VPC]
    subgraph CR[Cloud Run]
      SPA[Vue SPA static/dist]
      Flask[Flask OAuth + /ui-api + /api proxy]
      subgraph SK[ADK agents]
        C[Coordinator Agent]
        A1[Task Specialist]
        A2[Schedule Specialist]
        A3[Notes Specialist]
        INV[Shared Inventory Tool]
      end
    end

    subgraph ADB[AlloyDB]
      DB[Inventory + chats + memory + telemetry]
    end

    D --> SPA
    SPA --> Flask
    Flask <--> SK
    Flask <--> DB
    SK <--> DB
  end

  C --> A1
  C --> A2
  C --> A3
  C --> INV
  A1 --> INV
  A2 --> INV
  A3 --> INV

  A1 --> GT[Google Tasks]
  A2 --> GC[Google Calendar]
  A3 --> GD[Google Docs]

  C -->|Unified response| U
```

### Your journey as a user

**Production:** the landing page has **no chat** until you **Sign in with Google**; after that you can talk to the assistant.

Sidekick uses **Google** for **OAuth 2.0 / OpenID Connect** (sign-in, refresh tokens, and user profile), optional **Calendar**, **Tasks**, and **Docs + Drive** APIs (when tools sync data), **Vertex AI** / **Gemini** (agents and natural-language time parsing), and **Cloud Speech-to-Text** (optional **voice dictation** into the chat composer via `/ui-api/speech/transcribe`).

```mermaid
flowchart TD
  A([Open Sidekick]) --> B[Vue SPA landing — no chat yet]
  B --> C[Sign in with Google]
  C --> D[You can use the chat]
  D --> Cmp[Composer: type or voice dictation]
  Cmp -.->|mic WebM Opus| Stt[Cloud Speech-to-Text via /ui-api/speech/transcribe]
  Stt -.-> Cmp
  Cmp --> E[You send a message]
  E --> Px[Flask /api: optional memory preamble from sidekick_memory]
  Px --> F[ADK: Gemini on Vertex AI + coordinator and specialists]
  F --> DB[(AlloyDB for your owner_sub)]
  F -.-> APIs[Google Calendar Events, Tasks, Docs — when a tool updates them]
  DB --> R[Assistant reply in chat — only after tools finish, including any Google API updates]
  APIs -.-> R
  R --> D
```

- **Landing:** no composer until you are signed in (see the **Public** block in the big-picture diagram).
- **Voice:** dictation only **fills the composer** with transcribed text; the assistant turn is still a normal **`/api`** message once you send it (same OAuth and `owner_sub` rules as typing).
- **Each turn:** specialists may write to **AlloyDB** and, when integrations are on, call **Calendar / Tasks / Docs**; the next message appears **after** those tool calls complete. The chat UI does **not** show user ids on each item.
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
  NotesSpec --> NT[Notes tools DB and/or Google Docs]

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
  NT -.-> GDocs[Google Docs API]
```

The **coordinator** usually routes to **one specialist** per sub-request. For a **full Sidekick inventory** (list everything across tasks, calendar, and notes), it calls **`list_sidekick_inventory`**, summarizes, then **transfers in order** to **TaskSpecialist → ScheduleSpecialist → NotesSpecialist** so each domain interprets its slice and the coordinator synthesizes next actions (suggestions only unless the user asked to change data). **Specialists also have `list_sidekick_inventory`** when they need cross-domain context on other turns. Specialists call **database tools** and, when OAuth scopes and APIs allow, **Google** tools; **`sidekick_google_oauth`** holds refresh tokens for those calls.

### ADK agent architecture (as implemented)

This is the “wiring diagram” of what actually runs in this repo: `main.py` starts an internal ADK FastAPI server, the Flask app serves the **Vite-built Vue** bundle from `static/dist/`, registers **`/ui-api/*`** (inventory, chats, speech), proxies **`/api/*`** to ADK, injects the signed-in user’s `sub` as ADK `user_id`, **`active_chat_id`** on runs when `X-Sidekick-Chat-Id` is set, and may **prepend a memory block** (embedding lookup on `sidekick_memory`) before forwarding **`/run`**. The ADK root agent (`SidekickCoordinator`) delegates with **`transfer_to_agent`**—usually **one specialist** per sub-request, except for a **full inventory** turn where Root runs **`list_sidekick_inventory`** then chains **Task → Schedule → Notes** before synthesizing.

```mermaid
flowchart TB
  Browser["Browser Vue SPA"]
  subgraph Runtime["Sidekick runtime (one container)"]
    direction TB
    Flask["Flask + OAuth + /ui-api + /api proxy<br/>run: memory preamble · main.py"]
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
    DB["PostgreSQL / AlloyDB<br/>tasks calendar notes oauth"]
    ChatMem["Chats messages telemetry<br/>memory grants"]
    Vertex["Vertex AI Gemini<br/>LLM + time parsing + embed helper"]
    GTasks["Google Tasks API"]
    GCal["Google Calendar API"]
    GDocs["Google Docs API"]
    MCP["MCP toolsets (optional)<br/>SIDEKICK_MCP_*"]
  end

  Browser -->|SPA routes + OAuth| Flask
  Browser -->|/api and /ui-api| Flask
  Flask -->|rewrite path user_id state_delta run body| ADK
  Flask --> ChatMem
  ADK --> Root

  Task --> DB
  Sched --> DB
  Notes --> DB
  Inv --> DB

  Sched --> Vertex
  Task -.->|when enabled| GTasks
  Sched -.->|when enabled| GCal
  Notes -.->|when enabled| GDocs
  Task -.->|optional| MCP
  Sched -.->|optional| MCP
  Notes -.->|optional| MCP
```

**Full inventory flow:** Root calls **`list_sidekick_inventory`** (reads tasks, calendar events, and notes from DB and/or Google APIs), then **`transfer_to_agent`** in order: **Task → Schedule → Notes** for domain interpretation, then Root synthesizes. On other turns, Root typically transfers to **one** specialist; any specialist may call **`list_sidekick_inventory`** for cross-domain context.

### ADK request flow (one chat turn)

```mermaid
sequenceDiagram
  participant UI as Vue SPA
  participant Web as Flask /api + /ui-api
  participant ADK as ADK FastAPI
  participant Root as SidekickCoordinator
  participant Spec as Specialist agent
  participant DB as AlloyDB/Postgres
  participant G as Google APIs (optional)

  UI->>Web: POST /api/.../run (message + optional X-Sidekick-Chat-Id)
  Note right of Web: OAuth on: session required. Set user_id, state_delta.active_chat_id, optional memory preamble from sidekick_memory.
  opt Relevant memories for this user text
    Web->>DB: Embed query + read sidekick_memory
    DB-->>Web: Top-K memory texts
  end
  Web->>ADK: Forward rewritten JSON body
  ADK->>Root: Run root_agent
  Root->>Spec: transfer_to_agent (Tasks/Schedule/Notes)
  Spec->>DB: Tool calls (CRUD rows for owner_sub)
  Spec-->>G: Tool calls (Calendar/Tasks/Docs) when enabled
  Spec-->>Root: Tool results
  Root-->>ADK: Final response text
  ADK-->>Web: HTTP response (JSON)
  Web-->>UI: Proxy response
```

### ADK flow: full Sidekick inventory + specialist interpretation

When the user asks to list everything Sidekick-tagged across tasks, calendar, and notes, the coordinator calls **`list_sidekick_inventory`** once, then transfers to each specialist in order so they interpret their slice (recommendations only unless the user asked for changes); the coordinator then synthesizes.

```mermaid
sequenceDiagram
  participant UI as Vue SPA
  participant Web as Flask /api + /ui-api
  participant ADK as ADK FastAPI
  participant Root as SidekickCoordinator
  participant Inv as list_sidekick_inventory
  participant Task as TaskSpecialist
  participant Sched as ScheduleSpecialist
  participant Notes as NotesSpecialist
  participant DB as AlloyDB/Postgres
  participant G as Google APIs (optional)

  UI->>Web: POST /api/.../run (inventory message + optional X-Sidekick-Chat-Id)
  Web->>ADK: Forward with user_id + state_delta
  ADK->>Root: Run root_agent
  Root->>Inv: Tool call (combined inventory JSON)
  Inv->>DB: Read tasks / events / notes (and/or Google-backed paths)
  Inv-->>G: List Tasks/Calendar/Docs when APIs enabled
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
- **OAuth vs UI:** When `GOOGLE_OAUTH_CLIENT_ID` / `SECRET` are set, the Vue app hides the chat until `/auth/me` shows a signed-in user; Flask returns **401** on **`/api`** and **`/ui-api`** without a session—matching the public-vs-authenticated diagram above. Omit those vars only for local open testing.
- **Code map:** `main.py` serves the built SPA from `static/dist/` and proxies **`/api/*`** to ADK; **`/ui-api/*`** is implemented in `sidekick/flask_*_api.py`; `sidekick/agent.py` defines the multi-agent graph; `sidekick/db.py` handles the database; Google integrations live in `sidekick/google_*` modules.

Python modules include **module and function docstrings** describing behavior and configuration hooks.

## Legal and policy pages

**Privacy Policy** and **Terms of Service** are always reachable without signing in (`/privacy-policy`, `/terms-and-conditions`); Flask serves the same **`static/dist/index.html`** bundle as `/`, and the client router picks the view. On the **homepage** (`/`), visitors without a session still see the header and footer (including those links) but **not** the chat composer—that stays behind the login wall until Google sign-in completes.
