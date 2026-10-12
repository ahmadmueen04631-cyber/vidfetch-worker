# VidFetch Free Extractor Worker

A lightweight Python microservice powered by `yt-dlp` and `FastAPI` that extracts direct downloadable video streams from 1,800+ websites (YouTube, Instagram, Facebook, Reddit, Twitter, TikTok, and more).

## 🚀 100% Free Deployment Options ($0/month)

### Option 1: Deploy Free on Render.com (Recommended)
1. Push this folder to a GitHub repository (or fork).
2. Go to [render.com](https://render.com) (free account).
3. Click **New +** -> **Web Service**.
4. Select your GitHub repository (or choose Docker).
5. Set:
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: **Free ($0/month)**
6. Click **Create Web Service**.
7. Once deployed, copy your Render URL (e.g. `https://vidfetch-worker.onrender.com`).
8. In Vercel (or `.env.local`), add:
   ```env
   EXTRACTOR_SERVICE_URL=https://vidfetch-worker.onrender.com
   ```

---

### Option 2: Deploy Free on Hugging Face Spaces (No Credit Card)
1. Go to [huggingface.co/spaces](https://huggingface.co/spaces).
2. Create a new Space:
   - SDK: **Docker** (Blank)
   - License: open
3. Upload `Dockerfile`, `requirements.txt`, and `main.py`.
4. Hugging Face builds and runs it 24/7 for free with 16GB RAM!
5. In Vercel, set `EXTRACTOR_SERVICE_URL=https://your-username-your-space.hf.space`.
