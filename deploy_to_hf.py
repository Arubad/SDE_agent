import os
import getpass
from pathlib import Path
from huggingface_hub import HfApi

REPO_ID = "Arushh1050/sde-mastery-hub"
BASE_DIR = Path(__file__).resolve().parent

print("=" * 65)
print(f"🚀 Deploying SDE Mastery Hub to Hugging Face Space: {REPO_ID}")
print("=" * 65)

# Check environment variable first, or prompt securely
token = os.getenv("HF_TOKEN")
if not token:
    token = getpass.getpass("Paste your Hugging Face write token (from https://huggingface.co/settings/tokens): ").strip()

if not token:
    print("❌ Error: No token provided.")
    exit(1)

api = HfApi(token=token)

# Create the Space if missing. Free accounts only get ZeroGPU for Gradio Spaces (cpu-basic needs PRO)
api.create_repo(REPO_ID, repo_type="space", space_sdk="gradio", space_hardware="zero-a10g",
                private=True, exist_ok=True)

print("\n📦 Uploading project files to Hugging Face...")
print("   (Uploading directly via Hugging Face API — bypassing all Git CLI file size limits)")

try:
    commit_info = api.upload_folder(
        folder_path=str(BASE_DIR),
        repo_id=REPO_ID,
        repo_type="space",
        commit_message="Deploy SDE Mastery Hub (Qdrant hybrid RAG, MCP, tiered agent)",
        # remove files from earlier deploys that are no longer shipped
        delete_patterns=["backend/storage/**", "backend/ingest.py", "backend/data/**", "Dockerfile", ".dockerignore"],
        ignore_patterns=[
            "backend/venv/**",
            "backend/.env",
            ".git/**",
            "**/__pycache__/**",
            "*.pyc",
            "**/.DS_Store",
            "**/.env",
            "deploy_to_hf.py",
            "Dockerfile",
            ".dockerignore",
            ".github/**",
            "backend/storage/**",          # legacy LlamaIndex index: only used as the eval baseline
            "backend/qdrant_local/**",
            "backend/eval/results/*.json",  # keep the *.md summaries (served as an MCP resource)
            "backend/ingest.py",
            "backend/data/**",
            ".pytest_cache/**",
            "**/.pytest_cache/**"
        ]
    )
    print("\n" + "=" * 65)
    print("✅ DEPLOYMENT UPLOAD COMPLETE!")
    print(f"👉 Space Dashboard: https://huggingface.co/spaces/{REPO_ID}")
    print(f"👉 Live Web App URL: https://arushh1050-sde-mastery-hub.hf.space")
    print("=" * 65)
    print("\n💡 Space secrets needed: GEMINI_API_KEY, QDRANT_URL, QDRANT_API_KEY, TESTFORGE_FOUNDRY_ENDPOINT,")
    print("   TESTFORGE_FOUNDRY_API_KEY, PREMIUM_PASSCODE. Set them in:")
    print(f"   https://huggingface.co/spaces/{REPO_ID}/settings (Variables and secrets)")
except Exception as e:
    print(f"\n❌ Upload failed: {e}")
