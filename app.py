import os

# HF ZeroGPU: `spaces` must be imported before torch / gradio / backend.server
try:
    import spaces
except ImportError:  # running locally
    spaces = None

import gradio as gr
from backend.server import app as fastapi_app


def health(text: str) -> str:
    return text


# ZeroGPU refuses to start a Space that registers no @spaces.GPU function.
# The app itself runs on CPU (local embeddings + Gemini API); this is just the required registration.
if spaces is not None:
    health = spaces.GPU(health)


# Minimal Gradio dashboard mounted at /gradio so Hugging Face Gradio SDK is fully satisfied
with gr.Blocks(title="SDE Mastery Hub Backend") as demo:
    gr.Markdown(
        """
        # 🚀 SDE Mastery Hub Backend Running
        
        The interactive agentic tutor is live at the root dashboard:
        👉 **[Open Interactive SDE Hub Dashboard](/)**
        
        * FastAPI SSE Endpoint: `/chat`
        * API Docs: `/docs`
        """
    )
    status = gr.Textbox(value="Backend & Vector Store Active", label="Status", interactive=False)
    gr.Button("Health check").click(health, inputs=status, outputs=status)

# Mount Gradio onto our FastAPI application (SSR off: it spawns a Node proxy on another port)
app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio", ssr_mode=False)

if __name__ == "__main__":
    # ZeroGPU only reports startup from inside gr.Blocks.launch(), which we never call because
    # uvicorn serves the combined FastAPI app. Run that startup hook ourselves.
    zero = getattr(spaces, "zero", None) if spaces is not None else None
    if zero is not None and hasattr(zero, "startup"):
        zero.startup()

    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
