import os
import re
import urllib.request
import urllib.parse
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from llama_index.core import VectorStoreIndex, Settings, SimpleDirectoryReader, Document
from llama_index.embeddings.huggingface import HuggingFaceEmbedding
from llama_index.readers.github import GithubRepositoryReader, GithubClient
from youtube_transcript_api import YouTubeTranscriptApi

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
DATA_DIR = BASE_DIR / "data"
TRANSCRIPT_CACHE_DIR = DATA_DIR / "transcripts_cache"

# Load environment variables from .env
load_dotenv(BASE_DIR / ".env")

# Use local embedding model
Settings.embed_model = HuggingFaceEmbedding(model_name="BAAI/bge-small-en-v1.5")

# Regex to detect YouTube video IDs (URLs and YAML frontmatter)
YOUTUBE_URL_REGEX = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([a-zA-Z0-9_-]{11})'
)
YOUTUBE_YAML_REGEX = re.compile(r'id:\s*([a-zA-Z0-9_-]{11})')

def fetch_youtube_transcript_document(video_id: str, origin_info: str = ""):
    """
    Dynamically fetches full text transcripts from YouTube for any video ID detected
    during web scraping or repository ingestion, caching it locally to disk and
    packaging it into a LlamaIndex Document.
    """
    TRANSCRIPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = TRANSCRIPT_CACHE_DIR / f"{video_id}.txt"

    full_text = ""
    if cache_file.exists():
        try:
            full_text = cache_file.read_text(encoding="utf-8")
            print(f"      [YouTube Cache] Loaded {video_id} from disk cache ({len(full_text.split())} words).")
        except Exception as e:
            full_text = ""

    if not full_text:
        try:
            if hasattr(YouTubeTranscriptApi, "get_transcript"):
                transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
                full_text = " ".join([segment['text'] for segment in transcript_list])
            else:
                transcript = YouTubeTranscriptApi().fetch(video_id)
                full_text = " ".join([snippet.text for snippet in transcript])

            if full_text and len(full_text.strip()) >= 50:
                # Save to disk cache
                cache_file.write_text(full_text, encoding="utf-8")
        except Exception as e:
            print(f"      [YouTube Notice] Could not fetch transcript for {video_id}: {e}")
            return None

    if not full_text or len(full_text.strip()) < 50:
        return None

    clean_text = (
        f"YouTube Video Lecture Transcript\n"
        f"Video ID: {video_id}\n"
        f"Video URL: https://www.youtube.com/watch?v={video_id}\n"
        f"Origin Reference: {origin_info}\n\n"
        f"{full_text}"
    )

    metadata = {
        "source": "youtube_transcript",
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "origin_page": origin_info,
        "title": f"YouTube Video {video_id} ({origin_info})"
    }
    return Document(text=clean_text, metadata=metadata)

def discover_subpages(base_url: str, match_filter: str = "") -> list:
    """Discovers internal sub-links on a domain or course site."""
    discovered = []
    try:
        req = urllib.request.Request(base_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            full_url = urllib.parse.urljoin(base_url, href)
            if match_filter and match_filter not in full_url:
                continue
            if full_url not in discovered and full_url != base_url:
                discovered.append(full_url)
    except Exception as e:
        print(f"    Notice: could not discover subpages from {base_url}: {e}")
    return discovered

def scrape_web_page(url: str, processed_videos: set) -> list:
    """
    Scrapes a web page, extracts its text, and detects any embedded YouTube links
    to automatically retrieve their transcripts.
    """
    docs = []
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8", errors="ignore")

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()

        page_title = soup.title.string.strip() if soup.title and soup.title.string else url
        body_text = soup.get_text(separator="\n", strip=True)

        if body_text:
            docs.append(
                Document(
                    text=f"Web Resource: {page_title}\nURL: {url}\n\n{body_text}",
                    metadata={"source": "web", "url": url, "title": page_title}
                )
            )

        # Detect embedded YouTube videos in raw HTML and text
        video_ids = set(YOUTUBE_URL_REGEX.findall(html)) | set(YOUTUBE_URL_REGEX.findall(body_text))
        for vid in video_ids:
            if vid not in processed_videos:
                processed_videos.add(vid)
                print(f"      [YouTube Link Detected] Found {vid} on {url} -> Fetching transcript...")
                yt_doc = fetch_youtube_transcript_document(vid, origin_info=f"{page_title} ({url})")
                if yt_doc:
                    docs.append(yt_doc)
                    word_count = len(yt_doc.text.split())
                    print(f"      [YouTube Pipeline] Ingested transcript for {vid} ({word_count} words).")

    except Exception as e:
        print(f"    Failed to scrape {url}: {e}")

    return docs

def build_knowledge_base():
    all_documents = []
    processed_videos = set()
    
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        raise ValueError("CRITICAL: GITHUB_TOKEN not found in .env file.")
    
    github_client = GithubClient(github_token=github_token)

    print("1. Ingesting Curated GitHub Repositories...")
    github_repos = [
        ("donnemartin", "system-design-primer", "master"),
        ("ashishps1", "awesome-system-design-resources", "main"),
        ("missing-semester", "missing-semester", "master")
    ]
    
    for owner, repo, branch in github_repos:
        print(f" -> Fetching {owner}/{repo} (branch: {branch})...")
        try:
            gh_reader = GithubRepositoryReader(
                github_client=github_client,
                owner=owner,
                repo=repo,
                use_parser=False,
                verbose=False
            )
            docs = gh_reader.load_data(branch=branch)
            
            allowed_exts = (".md", ".txt", ".py", ".cpp", ".cc", ".hpp")
            filtered_docs = []
            yt_repo_docs = []

            for doc in docs:
                file_name = doc.metadata.get("file_path", doc.metadata.get("file_name", ""))
                if file_name.endswith(allowed_exts):
                    # Set standard metadata
                    doc.metadata["source"] = f"github:{owner}/{repo}"
                    doc.metadata["repo"] = repo
                    doc.metadata["file_name"] = file_name
                    filtered_docs.append(doc)

                    # Detect embedded YouTube links in markdown or YAML
                    if file_name.endswith((".md", ".txt")):
                        vids = set(YOUTUBE_URL_REGEX.findall(doc.text))
                        if "video:" in doc.text:
                            vids.update(YOUTUBE_YAML_REGEX.findall(doc.text))

                        for vid in vids:
                            if vid not in processed_videos and len(yt_repo_docs) < 30:
                                processed_videos.add(vid)
                                print(f"      [YouTube Link in Repo] Found {vid} in {file_name} -> Fetching transcript...")
                                yt_doc = fetch_youtube_transcript_document(vid, origin_info=f"GitHub: {owner}/{repo}/{file_name}")
                                if yt_doc:
                                    yt_repo_docs.append(yt_doc)
                                    print(f"      [YouTube Pipeline] Ingested {vid} ({len(yt_doc.text.split())} words)")
            
            filtered_docs.extend(yt_repo_docs)
            print(f"    Loaded {len(filtered_docs)} relevant coding, markdown, and transcript documents.")
            all_documents.extend(filtered_docs)
            
        except Exception as e:
            print(f"    Failed to load {repo}: {str(e)}")

    print("\n2. Ingesting Curated Web Resources & Crawling Lecture Pages...")
    base_web_urls = [
        "https://sre.google/sre-book/introduction/",
        "https://sadservers.com/labs/",
        "https://symflower.com/en/company/blog/2023/what-is-the-testing-trophy/",
        "https://google.github.io/eng-practices/review/reviewer/standard.html",
        "https://missing.csail.mit.edu/"
    ]

    all_web_urls = []
    for u in base_web_urls:
        all_web_urls.append(u)
        # Automatically discover all MIT Missing Semester lecture topic pages
        if "missing.csail.mit.edu" in u:
            print(" -> Crawling MIT Missing Semester lecture topic pages...")
            subpages = discover_subpages(u, match_filter="missing.csail.mit.edu")
            lecture_subpages = [p for p in subpages if "/2020/" in p or "/2026/" in p]
            print(f"    Discovered {len(lecture_subpages)} lecture subpages.")
            all_web_urls.extend(lecture_subpages)

    # Deduplicate URLs
    all_web_urls = list(dict.fromkeys(all_web_urls))
    print(f" -> Scraping total of {len(all_web_urls)} web pages and extracting embedded YouTube videos...")

    for page_url in all_web_urls:
        page_docs = scrape_web_page(page_url, processed_videos)
        all_documents.extend(page_docs)

    print("\n3. Ingesting Local PDFs (O'Reilly Software Engineering at Google)...")
    if DATA_DIR.exists() and any(f.suffix == ".pdf" for f in DATA_DIR.iterdir()):
        try:
            pdf_reader = SimpleDirectoryReader(input_dir=str(DATA_DIR), required_exts=[".pdf"])
            all_documents.extend(pdf_reader.load_data())
        except Exception as e:
            print(f"    Failed to load PDF: {str(e)}")
    else:
        print(f" -> Skipping PDF ingestion. Add PDFs to '{DATA_DIR}' to include them.")

    print(f"\nTotal valid document chunks loaded: {len(all_documents)}")
    if len(all_documents) > 0:
        print(f"Building Vector Index and persisting to disk at {STORAGE_DIR}...")
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        index = VectorStoreIndex.from_documents(all_documents)
        index.storage_context.persist(persist_dir=str(STORAGE_DIR))
        print(f"Knowledge base successfully built and saved to {STORAGE_DIR}")
    else:
        print("No documents were loaded. Vector database was not built.")

if __name__ == "__main__":
    build_knowledge_base()
