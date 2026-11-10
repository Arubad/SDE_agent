---
title: SDE Mastery Hub - Autonomous Agentic RAG Tutor
emoji: 🧠
colorFrom: indigo
colorTo: purple
sdk: gradio
sdk_version: 5.20.0
app_file: app.py
pinned: false
license: mit
---

# 🚀 SDE Mastery Hub — Autonomous Agentic RAG & Engineering Tutor

[![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![LlamaIndex](https://img.shields.io/badge/LlamaIndex-0.10+-black.svg)](https://www.llamaindex.ai/)
[![Gemini](https://img.shields.io/badge/Gemini_3.5_Flash-Google_AI-4285F4.svg?logo=google&logoColor=white)](https://ai.google.dev/)
[![Hugging Face Spaces](https://img.shields.io/badge/%F0%9F%A4%97%20Hugging%20Face-Spaces-yellow)](https://huggingface.co/spaces)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg?logo=docker&logoColor=white)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

An end-to-end, agentic full-stack Software Engineering tutor and system design assistant. Powered by **Google Gemini 3.5 Flash**, **LlamaIndex RAG**, and local Hugging Face embeddings (`BAAI/bge-small-en-v1.5`), this agent grounds its reasoning in over **1,500+ curated engineering knowledge chunks**—including MIT Missing Semester lectures, System Design Primer, Google SRE books, and live spoken YouTube video transcripts.

Featuring a modern dark-mode UI with **real-time Server-Sent Events (SSE) streaming**, an **interactive reasoning timeline**, and a **half-landscape / full-maximize responsive layout**.

---

## 🌟 Key Highlights & Architectural Potential

```
                        +---------------------------------------------+
                        |        Client Frontend (Web Browser)         |
                        |   - Glassmorphic Responsive Dashboard       |
                        |   - Half-Landscape & Full 94vw Maximize     |
                        |   - Live SSE Animated Execution Timeline    |
                        +----------------------+----------------------+
                                               |
                                     (SSE /chat POST Stream)
                                               v
+-----------------------------------------------------------------------------------------+
|                               FastAPI Backend Agent Router                              |
|   - Dynamic Port Binding ($PORT / 7860 / 8001)                                          |
|   - Custom Private Network Access (PNA) & Wildcard CORS Middleware                      |
|   - Thread-Safe ContextVars Event Streaming (thought -> tool_start -> tool_done -> reply)|
+-----------------------------+-----------------------------+-----------------------------+
                              |                             |
                              v                             v
           +-----------------------------------+   +-----------------------------------+
           |    LlamaIndex RAG Knowledge Base  |   |   Dynamic YouTube & Web Tools     |
           | - 1,591 Embedded Vector Chunks    |   | - Live YouTubeTranscriptApi Fetch |
           | - BAAI/bge-small-en-v1.5 (CPU)    |   | - yt-dlp Video Caption Fallback   |
           | - MIT Missing Semester Notes & Vids|  | - Disk Transcript Cache System    |
           | - Donne Martin System Design Primer|  +-----------------------------------+
           | - Google SRE & Testing Trophy     |
           +-----------------------------------+
                              \                             /
                               \                           /
                                v                         v
                        +-----------------------------------------+
                        |         Gemini 3.5 Flash Model          |
                        |   - Google SRE & Toil Elimination Focus |
                        |   - Algorithmic Rigor in C++            |
                        |   - Function Calling Tool Execution     |
                        +-----------------------------------------+
```

### 1. 📚 Multi-Source Curated Engineering Knowledge Base (1,500+ Chunks)
Unlike generic chatbots prone to hallucination, SDE Mastery Hub retrieves strict context from:
* **MIT's The Missing Semester of Your CS Education**: Written lecture notes, code samples, exercises, and full spoken lecture transcripts across Shell, Data Wrangling (`sed`/`awk`), Git, Security, and Debugging.
* **The System Design Primer (Donne Martin)**: High-level architectural patterns, load balancing, caching strategies, replication, and sharding.
* **Awesome System Design Resources**: Curated architectural case studies and conference lectures.
* **Google Site Reliability Engineering (SRE) Book**: Error budgets, SLOs/SLIs, toil elimination, and distributed incident management.
* **Google Engineering Practices**: Senior code review standards and engineering hygiene.
* **Testing Trophy Methodology**: Integration-first testing strategies and end-to-end verification.

### 2. 🎥 Autonomous YouTube Embed Detection & Transcript Extraction Pipeline
* **Automated Scraper Detection**: When scraping web pages or GitHub documentation, the ingestion engine detects embedded YouTube videos and players via regex (`youtube.com/watch?v=`, `youtu.be/`, `/embed/`, and YAML `id: ...`).
* **Spoken Transcript Indexing**: Discovered videos are automatically passed to the YouTube transcript pipeline, extracting the instructor's spoken dialogue into structured vector documents.
* **Local Disk Caching (`backend/data/transcripts_cache/`)**: Fetched transcripts are cached locally on disk to protect against YouTube cloud IP rate limits and eliminate unnecessary network round-trips.

### 3. ⏱️ Live Animated Agent Reasoning Timeline (Server-Sent Events)
* **Real-Time Step Visualization**: Shows the agent's step-by-step thinking before the answer renders:
  * 🧠 *Planning / Analyzing Query*
  * 🔍 *Searching Vector Database* (displays exact query parameters)
  * ✅ *Knowledge Context Retrieved* (displays interactive badge pills for retrieved sources)
  * 🎬 *Fetching Live YouTube Transcript* (when dynamic video analysis is triggered)
  * ⚡ *Synthesizing Architecture Solution*
* **Live Timer & Collapsible Disclosure**: A real-time timer runs while the agent executes tasks. Once complete, the entire trace is preserved inside a collapsible disclosure card.

### 4. 💻 Dynamic Live Video Analysis Tool
* Users can provide any YouTube link in chat (e.g. `Can you explain the key takeaways from https://www.youtube.com/watch?v=sz_dsktIjt4 ?`).
* The agent automatically triggers `get_live_youtube_transcript`, extracts the full captions in real time, and synthesizes an executive technical summary with timestamps and code snippets.

### 5. 🛡️ Production Engineering Standards Built In
* **Algorithmic Rigor**: All algorithmic queries require optimal, production-grade **C++** implementations with Big-O time and space complexity annotations.
* **SRE Principles**: Emphasizes toil elimination, graceful degradation, circuit breakers, and telemetry.
* **Testing Strategies**: Grounded in the Testing Trophy (integration testing focus) and Playwright E2E UI testing patterns.

---

## 🗂️ Project Directory Structure

```
SDE_mastery_hub/
├── .dockerignore                  # Docker build exclusions (keeps image small)
├── .gitignore                     # Git exclusions (protects .env and venv)
├── Dockerfile                     # Production container spec for Hugging Face Spaces
├── README.md                      # Comprehensive project guide & HF metadata
├── backend/
│   ├── .env                       # Environment variables (GEMINI_API_KEY, GITHUB_TOKEN)
│   ├── ingest.py                  # Knowledge ingestion, web crawler & YouTube extractor
│   ├── mcp_server.py              # FastMCP Agent Server for Claude Desktop / Cursor
│   ├── requirements.txt           # Python dependency specifications
│   ├── server.py                  # FastAPI server with SSE streaming & CORS middleware
│   ├── data/
│   │   ├── swe_at_google.pdf      # Local Software Engineering PDFs
│   │   └── transcripts_cache/     # Cached YouTube lecture transcripts
│   └── storage/                   # Pre-indexed LlamaIndex vector store (1,591 nodes, 22MB)
└── frontend/
    └── index.html                 # Single-page responsive dashboard & resizable chat UI
```

---

## 🚀 Quickstart: Running Locally

### Prerequisites
* **Python 3.10+** (Python 3.12 recommended)
* A free **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/)
* A personal **GitHub Token** (optional, only needed if you wish to run `ingest.py` from scratch)

### 1. Clone & Set Up Virtual Environment

```bash
git clone https://github.com/<your-username>/SDE_mastery_hub.git
cd SDE_mastery_hub

# Create and activate virtual environment
python3 -m venv backend/venv
source backend/venv/bin/activate  # On Windows: backend\venv\Scripts\activate

# Install dependencies with CPU-only PyTorch (fast and lightweight)
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r backend/requirements.txt
```

### 2. Configure Environment Variables

Create a `backend/.env` file:

```env
GEMINI_API_KEY=your_gemini_api_key_here
GITHUB_TOKEN=your_github_token_here       # Optional: only for re-running ingest.py
```

> **Note on Gemini API Keys:** When creating your key on Google AI Studio, select **"Create API key in new project"** to ensure it is assigned to the Free Tier (15 RPM, 1500 RPD).

### 3. Start the Server

```bash
# Run the FastAPI server (serves both API and Frontend)
python backend/server.py
```

Open your browser and navigate to:
👉 **[http://localhost:8001](http://localhost:8001)**

*(Alternatively, you can run a local HTTP server in `frontend/` on port 3000: `python3 -m http.server 3000 --directory frontend` and browse to [http://localhost:3000](http://localhost:3000).)*

---

## 🔄 (Optional) Rebuilding the Vector Knowledge Base

The repository includes a pre-indexed vector store in `backend/storage/` (1,591 chunks, 22MB), so you can run the application immediately without re-ingesting!

If you wish to add new repositories, documentation sites, or lecture series:

1. Edit the sources list in `backend/ingest.py`.
2. Run the ingestion pipeline:
   ```bash
   python backend/ingest.py
   ```
3. The crawler will scrape all pages, detect any embedded YouTube links, fetch their transcripts, compute local embeddings, and persist the updated index to `backend/storage/`.

---

## ☁️ Deployment: 100% Free on Hugging Face Spaces

This project is configured out of the box for **Hugging Face Spaces** using their free **2-vCPU / 16GB RAM Gradio SDK** tier (100% free, no credit card required).

### Step 1: Create a New Space
1. Log in to [Hugging Face](https://huggingface.co).
2. Click your profile avatar $\rightarrow$ **New Space**.
3. Fill in:
   * **Space name**: `sde-mastery-hub`
   * **License**: `mit`
   * **Space SDK**: Select **Gradio**
   * **Space hardware**: Select **Free (2 vCPU · 16GB RAM)**
   * **Visibility**: Public (or Private)
4. Click **Create Space**.

### Step 2: Configure Secret (`GEMINI_API_KEY`)
1. In your newly created Space, navigate to **Settings** $\rightarrow$ **Variables and secrets**.
2. Under **Secrets**, click **New secret**:
   * **Name**: `GEMINI_API_KEY`
   * **Value**: *Paste your Google Gemini API key*
3. Click **Save**.

### Step 3: Push the Repository to Hugging Face
In your local repository:

```bash
# Initialize git if not already initialized
git init
git add .
git commit -m "Deploy SDE Mastery Hub to Hugging Face Spaces"

# Add Hugging Face Space as a remote and push
git remote add space https://huggingface.co/spaces/<your-hf-username>/<your-space-name>
git push --force space main
```

*(Alternatively, you can connect your Space to your GitHub repository under Space Settings for automatic CI/CD deployment on every `git push`!)*

### Step 4: Access Your Live App
Hugging Face will automatically build the `Dockerfile`, download CPU-only PyTorch, load the pre-built vector database, and start Uvicorn on port `7860`. Within 2-3 minutes, your interactive AI tutor is live on the internet!

---

## 🛠️ FastMCP Agent Setup (Claude Desktop & Cursor)

This project also includes a standalone FastMCP server in [`backend/mcp_server.py`](file:///Users/arushlalitkumarbadhe/Desktop/SDE_mastery_hub/backend/mcp_server.py) to give your IDE or Claude Desktop direct access to your local knowledge base.

Add this snippet to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sde-knowledge-agent": {
      "command": "/path/to/SDE_mastery_hub/backend/venv/bin/python",
      "args": ["/path/to/SDE_mastery_hub/backend/mcp_server.py"]
    }
  }
}
```

---

## 🛡️ License

This project is licensed under the [MIT License](LICENSE).
Feel free to fork, customize, and build upon this project for study groups, interview preparation, and production engineering practices.
