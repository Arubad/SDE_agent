import os
import json
import asyncio
import traceback
import contextvars
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
import google.generativeai as genai
from llama_index.core import StorageContext, load_index_from_storage, Settings
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from youtube_transcript_api import YouTubeTranscriptApi

# 1. Load API Key with robust path resolution
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

gemini_api_key = os.getenv("GEMINI_API_KEY")
if gemini_api_key:
    genai.configure(api_key=gemini_api_key)

# 2. ContextVar for per-request streaming event queue & step history
current_event_context: contextvars.ContextVar = contextvars.ContextVar("current_event_context", default=None)

def emit_agent_event(event: dict):
    ctx_val = current_event_context.get()
    if ctx_val:
        queue, loop, steps = ctx_val
        steps.append(event)
        loop.call_soon_threadsafe(queue.put_nowait, event)

# 3. Initialize RAG Database as a pure retriever (no external LLM needed)
Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5", device="cpu")
storage_path = BASE_DIR / "storage"

if storage_path.exists():
    try:
        storage_context = StorageContext.from_defaults(persist_dir=str(storage_path))
        index = load_index_from_storage(storage_context)
        retriever = index.as_retriever(similarity_top_k=5)
        print("✅ Loaded local knowledge base successfully.")
    except Exception as e:
        print(f"⚠️ Error loading knowledge base from storage: {e}")
        retriever = None
else:
    print(f"⚠️ Warning: '{storage_path}' not found. Run ingest.py first.")
    retriever = None

# 4. Define Agent Tools with live thought / task emission
def query_sde_knowledge_base(query: str) -> str:
    """Retrieves context-augmented answers from the curated Software Engineering vector database."""
    print(f"   [TOOL TRIGGERED] RAG querying for: {query}")
    emit_agent_event({
        "type": "tool_start",
        "stage": "rag",
        "title": "Searching Vector Database",
        "tool": "RAG Vector Knowledge Base",
        "query": query,
        "detail": f"Querying local SDE vector index for: '{query}'"
    })

    if not retriever:
        emit_agent_event({
            "type": "tool_error",
            "stage": "rag",
            "title": "Database Unavailable",
            "detail": "Vector database not initialized on server."
        })
        return "Error: Vector database not initialized."

    try:
        nodes = retriever.retrieve(query)
        if not nodes:
            emit_agent_event({
                "type": "tool_done",
                "stage": "rag",
                "title": "No Matches",
                "sources": [],
                "detail": "No matching documentation found in vector index."
            })
            return "No relevant documentation found in the knowledge base."

        sources_found = []
        formatted_chunks = []
        for i, n in enumerate(nodes):
            meta = n.node.metadata
            source = (
                meta.get("url") or
                meta.get("URL") or
                meta.get("title") or
                meta.get("file_path") or
                meta.get("file_name") or
                meta.get("origin_page") or
                meta.get("source") or
                f"Doc {i+1}"
            )
            if str(source).startswith("http"):
                parsed = urllib.parse.urlparse(str(source))
                slug = parsed.path.strip("/").split("/")[-1] if parsed.path.strip("/") else parsed.netloc
                clean_source = f"{parsed.netloc}/{slug}" if slug != parsed.netloc else parsed.netloc
            else:
                clean_source = Path(str(source)).name if ("/" in str(source) or "\\" in str(source)) else str(source)
            if clean_source and clean_source not in sources_found:
                sources_found.append(clean_source)
            formatted_chunks.append(f"[Source: {source}]\n{n.node.get_content().strip()}")

        emit_agent_event({
            "type": "tool_done",
            "stage": "rag",
            "title": "Knowledge Context Retrieved",
            "tool": "RAG Vector Knowledge Base",
            "sources": sources_found,
            "detail": f"Retrieved {len(nodes)} document chunks from {', '.join(sources_found[:3])}"
        })
        return "\n\n---\n\n".join(formatted_chunks)
    except Exception as e:
        emit_agent_event({
            "type": "tool_error",
            "stage": "rag",
            "title": "Query Failed",
            "detail": f"Vector retrieval error: {str(e)}"
        })
        return f"Error: {str(e)}"

YOUTUBE_FAILURE_NOTE = (
    "TOOL FAILED: the video content could not be loaded. Do NOT describe, summarize or guess what this "
    "video contains, even if you think you recognize it. Tell the user plainly that the video could not "
    "be retrieved (and why, if known), then offer to help another way."
)

# Low media resolution ~ 100 tokens/sec of video, so even ~1h videos fit in the context window
VIDEO_ANALYSIS_MODEL = "gemini-3.5-flash"


def _extract_youtube_id(value: str) -> str:
    value = value.strip()
    if "v=" in value:
        return value.split("v=")[1].split("&")[0]
    for marker in ("youtu.be/", "/embed/", "/shorts/", "/live/"):
        if marker in value:
            return value.split(marker)[1].split("?")[0].split("/")[0]
    return value


def _fetch_transcript(video_id: str) -> str:
    if hasattr(YouTubeTranscriptApi, "get_transcript"):
        return " ".join(seg["text"] for seg in YouTubeTranscriptApi.get_transcript(video_id))
    return " ".join(snippet.text for snippet in YouTubeTranscriptApi().fetch(video_id))


def _analyze_video_with_gemini(video_id: str, question: str) -> str:
    from google import genai as genai_v2
    from google.genai import types

    client = genai_v2.Client(api_key=os.getenv("GEMINI_API_KEY"))
    prompt = (
        "Watch this YouTube video and produce a detailed, faithful account of its content for a software "
        "engineering tutor: main topics in order, key technical claims, definitions, examples, code or "
        "diagrams shown on screen, and notable quotes with approximate timestamps (MM:SS). "
        "Only report what is actually in the video."
    )
    if question:
        prompt += f"\n\nThe user specifically wants to know: {question}"
    response = client.models.generate_content(
        model=VIDEO_ANALYSIS_MODEL,
        contents=types.Content(parts=[
            types.Part(file_data=types.FileData(file_uri=f"https://www.youtube.com/watch?v={video_id}")),
            types.Part(text=prompt),
        ]),
        config=types.GenerateContentConfig(media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW),
    )
    if not response.text:
        raise RuntimeError("Gemini returned no analysis for this video")
    return response.text


def analyze_youtube_video(video_url: str, question: str = "") -> str:
    """Loads the actual content of a YouTube video (by URL or video ID) so you can summarize, analyze, or answer questions about it. Use this whenever the user shares a YouTube link or asks about a YouTube video. Pass the user's specific question about the video in `question` (or an empty string for a general summary)."""
    video_id = _extract_youtube_id(video_url)
    print(f"   [TOOL TRIGGERED] Loading YouTube video: {video_id}")

    # 1) Captions: fast and exact, but YouTube blocks this from most cloud IPs (e.g. HF Spaces)
    emit_agent_event({
        "type": "tool_start",
        "stage": "youtube",
        "title": "Fetching YouTube Transcript",
        "tool": "YouTube Transcript API",
        "query": video_id,
        "detail": f"Fetching captions for video ID: {video_id}"
    })
    try:
        text = _fetch_transcript(video_id)
        emit_agent_event({
            "type": "tool_done",
            "stage": "youtube",
            "title": "Transcript Loaded",
            "tool": "YouTube Transcript API",
            "detail": f"Loaded video transcript ({len(text.split())} words)"
        })
        return f"[Transcript of https://www.youtube.com/watch?v={video_id}]\n{text}"
    except Exception as e:
        transcript_error = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        print(f"   [YOUTUBE] Transcript unavailable, falling back to Gemini video input: {transcript_error}")

    # 2) Fallback: Gemini watches the public video directly (audio + frames), no scraping involved
    emit_agent_event({
        "type": "tool_start",
        "stage": "youtube",
        "title": "Watching Video with Gemini",
        "tool": "Gemini Video Understanding",
        "query": video_id,
        "detail": "Captions unavailable from this server, so Gemini is analysing the video directly..."
    })
    try:
        analysis = _analyze_video_with_gemini(video_id, question)
        emit_agent_event({
            "type": "tool_done",
            "stage": "youtube",
            "title": "Video Analysed",
            "tool": "Gemini Video Understanding",
            "detail": f"Gemini analysed the video ({len(analysis.split())}-word breakdown)"
        })
        return f"[Gemini analysis of the video content of https://www.youtube.com/watch?v={video_id}]\n{analysis}"
    except Exception as e:
        emit_agent_event({
            "type": "tool_error",
            "stage": "youtube",
            "title": "YouTube Video Unavailable",
            "detail": f"Transcript failed ({transcript_error}); Gemini video analysis failed ({e})"
        })
        return f"{YOUTUBE_FAILURE_NOTE}\nTranscript error: {transcript_error}\nVideo analysis error: {e}"

# 5. Setup FastAPI
app = FastAPI()

FRONTEND_FILE = BASE_DIR.parent / "frontend" / "index.html"

@app.get("/")
async def serve_index():
    if FRONTEND_FILE.exists():
        return FileResponse(FRONTEND_FILE)
    return {"message": "SDE Mastery Hub API Online. Open frontend/index.html to use the UI."}

# CORS & Private Network Access Middleware (allows cross-origin from localhost:3000, 127.0.0.1, file://, etc.)
@app.middleware("http")
async def cors_and_pna_middleware(request: Request, call_next):
    origin = request.headers.get("origin", "*")
    if request.method == "OPTIONS":
        response = Response(status_code=200)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE, PATCH"
        response.headers["Access-Control-Allow-Headers"] = request.headers.get("access-control-request-headers", "*")
        response.headers["Access-Control-Max-Age"] = "86400"
        if "access-control-request-private-network" in request.headers:
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = origin
    if "access-control-request-private-network" in request.headers:
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

class ChatRequest(BaseModel):
    message: str
    history: list
    stream: bool = True

SYSTEM_INSTRUCTION = """
You are an elite Software Engineering AI Tutor. Base your answers heavily on the following ideologies:
1. System Design: Favor scalable, boilerplate multi-agent workflows over theoretical papers.
2. SRE: Apply Google SRE principles (error budgets, toil elimination).
3. Testing: Emphasize 'The Testing Trophy' and end-to-end testing (e.g., Playwright).
4. Algorithms: The user already knows core concepts. Whenever writing Data Structures or Algorithms code, ONLY use C++ syntax. 

Grounding rules:
- When the user shares a YouTube link, ALWAYS call analyze_youtube_video and base your answer only on what it returns.
- If a tool returns an error or says "TOOL FAILED", tell the user it failed. Never fill the gap from your own memory or guess what a video or document contains.
"""

def execute_chat_sync(model, formatted_history, message):
    chat = model.start_chat(
        history=formatted_history,
        enable_automatic_function_calling=True
    )
    return chat.send_message(message)

@app.post("/chat")
async def chat_endpoint(req: ChatRequest):
    print(f"\n---> NEW MESSAGE RECEIVED: {req.message} (stream={req.stream})")
    load_dotenv(BASE_DIR / ".env", override=True)
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "..." or len(api_key.strip()) < 10:
        raise HTTPException(
            status_code=400,
            detail="Missing or invalid GEMINI_API_KEY in backend/.env. Please configure a valid Google Gemini API key."
        )

    # Format history
    formatted_history = []
    for msg in req.history:
        if "parts" in msg and msg["parts"] and "text" in msg["parts"][0]:
            formatted_history.append({
                "role": msg["role"],
                "parts": [msg["parts"][0]["text"]] 
            })

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-3.5-flash",
        tools=[query_sde_knowledge_base, analyze_youtube_video],
        system_instruction=SYSTEM_INSTRUCTION
    )

    # If non-streaming requested, execute directly
    if not req.stream:
        try:
            steps_log = []
            queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            current_event_context.set((queue, loop, steps_log))
            ctx = contextvars.copy_context()

            with ThreadPoolExecutor() as pool:
                response = await loop.run_in_executor(
                    pool, ctx.run, execute_chat_sync, model, formatted_history, req.message
                )

            reply_text = ""
            if response.candidates and response.candidates[0].content:
                for part in response.candidates[0].content.parts:
                    if hasattr(part, "text") and part.text:
                        reply_text += part.text

            return {"reply": reply_text or response.text, "steps": steps_log}
        except Exception as e:
            traceback.print_exc()
            raise HTTPException(status_code=500, detail=str(e))

    # Streaming mode with Live Agent Thoughts and Tool Traces
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    steps_log = []
    current_event_context.set((queue, loop, steps_log))
    ctx = contextvars.copy_context()

    async def sse_event_stream():
        # Emit initial thinking event
        yield f"data: {json.dumps({'type': 'thought', 'stage': 'planning', 'title': 'Analyzing Query', 'detail': 'Evaluating system architecture requirements and planning tool execution...'})}\n\n"

        async def run_worker():
            try:
                with ThreadPoolExecutor() as pool:
                    resp = await loop.run_in_executor(
                        pool, ctx.run, execute_chat_sync, model, formatted_history, req.message
                    )

                emit_agent_event({
                    "type": "thought",
                    "stage": "synthesizing",
                    "title": "Synthesizing Architecture Solution",
                    "detail": "Applying Google SRE principles, C++ DSA patterns, and structuring output..."
                })

                reply_text = ""
                if resp.candidates and resp.candidates[0].content:
                    for part in resp.candidates[0].content.parts:
                        if hasattr(part, "text") and part.text:
                            reply_text += part.text

                emit_agent_event({
                    "type": "reply",
                    "reply": reply_text or resp.text,
                    "steps": list(steps_log)
                })
            except Exception as e:
                err_msg = str(e)
                if "prepayment credits are depleted" in err_msg or "billing#prepay" in err_msg:
                    err_msg = "Google AI Studio prepayment credits depleted: Key is linked to a project requiring prepayment. Create a key in a free-tier project."
                elif "API_KEY_INVALID" in err_msg or "API key not valid" in err_msg:
                    err_msg = "Google Gemini API key is invalid. Please check GEMINI_API_KEY in backend/.env."
                emit_agent_event({"type": "error", "detail": err_msg})
            finally:
                emit_agent_event({"type": "done"})

        task = asyncio.create_task(run_worker())

        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    break
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        sse_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
