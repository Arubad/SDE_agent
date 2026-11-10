import os
from pathlib import Path
from fastmcp import FastMCP
from llama_index.core import StorageContext, load_index_from_storage, Settings
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from youtube_transcript_api import YouTubeTranscriptApi

# 1. Initialize identical embedding model to decode the vector database
Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5")

# 2. Initialize FastMCP Server
mcp = FastMCP(name="SDE_Knowledge_Agent")

# 3. Load the RAG database securely using robust path resolution
BASE_DIR = Path(__file__).resolve().parent
storage_path = BASE_DIR / "storage"

if storage_path.exists():
    try:
        storage_context = StorageContext.from_defaults(persist_dir=str(storage_path))
        index = load_index_from_storage(storage_context)
        # top_k=5 gives the agent enough context to piece together complex SDE answers
        retriever = index.as_retriever(similarity_top_k=5)
        print("Loaded local knowledge base successfully.")
    except Exception as e:
        print(f"Warning: Failed to load knowledge base. Error: {str(e)}")
        retriever = None
else:
    print(f"Warning: '{storage_path}' not found. Run ingest.py first.")
    retriever = None

# 4. Define Agent Tools
@mcp.tool
def query_sde_knowledge_base(query: str) -> str:
    """
    Retrieves context-augmented answers from the curated Software Engineering 
    and System Design vector database (GitHub repos, SRE books, and blogs).
    """
    if not retriever:
        return "Error: Vector database not initialized on server."
    try:
        nodes = retriever.retrieve(query)
        if not nodes:
            return "No matching documentation found."
        formatted_chunks = []
        for i, n in enumerate(nodes):
            meta = n.node.metadata
            source = meta.get("file_name") or meta.get("URL") or meta.get("file_path") or f"Doc {i+1}"
            formatted_chunks.append(f"[Source: {source}]\n{n.node.get_content().strip()}")
        return "\n\n---\n\n".join(formatted_chunks)
    except Exception as e:
        return f"Error querying knowledge base: {str(e)}"

@mcp.tool
def get_live_youtube_transcript(video_id: str) -> str:
    """
    Fetches full text transcripts dynamically from a YouTube video ID. 
    Use this when the user asks about a video not in the knowledge base.
    """
    if "v=" in video_id:
        video_id = video_id.split("v=")[1].split("&")[0]
    elif "youtu.be/" in video_id:
        video_id = video_id.split("youtu.be/")[1].split("?")[0]

    try:
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
            return " ".join([segment['text'] for segment in transcript_list])
        else:
            transcript = YouTubeTranscriptApi().fetch(video_id)
            return " ".join([snippet.text for snippet in transcript])
    except Exception as e:
        return f"Error retrieving transcript: {str(e)}"

if __name__ == "__main__":
    # stdio transport allows direct hookup to IDEs (Cursor/Claude)
    mcp.run()
