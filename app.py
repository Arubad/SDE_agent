import os
import gradio as gr
from backend.server import app as fastapi_app

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

# Mount Gradio onto our FastAPI application
app = gr.mount_gradio_app(fastapi_app, demo, path="/gradio")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
