# ResumeAI - Backend

The core intelligence and engine behind ResumeAI. Built heavily for speed and scale using **FastAPI** (asynchronous Python), **MongoDB** (Motor), and real-time Server-Sent Events (SSE).

## 🚀 Tech Stack

- **Framework**: [FastAPI](https://fastapi.tiangolo.com/) (Python 3.9+)
- **Database**: MongoDB (motor async driver)
- **AI Brain**: Google Gemini 3.1 Pro via `google-genai`
- **PDF Generation**: WeasyPrint & Jinja2
- **File Storage**: Supabase Storage (with local disk fallback)
- **Background Jobs**: FastAPI BackgroundTasks

## ⚙️ Architecture Highlights

1. **AI Agents**: Uses Google Gemini to intelligently tailor base resumes for specific job descriptions, extracting keywords and generating fresh, tailored executive summaries.
2. **Real-time PubSub**: Instead of polling, tailors happen in background tasks and the backend pushes completion events down to the React frontend via SSE.
3. **Storage Abstraction**: Uses Supabase for scalable production cloud storage, but gracefully falls back to local disk structure for developers without Supabase configured.
4. **Resilient PDF Engine**: Renders dynamic semantic HTML templates combining static assets and injecting JSON payload data before orchestrating WeasyPrint to stamp a pixel-perfect PDF.

## 🛠️ Setup Instructions

### 1. Requirements
* Python 3.9+
* MongoDB URI (Local or Atlas)
* Google Gemini API Key
* (Optional) Supabase Project

### 2. Environment
Copy the example environment configuration:
```bash
cp .env.example .env
```
Fill in `.env`:
```env
GEMINI_API_KEY=your_gemini_key
MONGO_URI=mongodb+srv://...
JWT_SECRET=super_strong_secret
# JWT_ALGORITHM=HS256
FRONTEND_URL=http://localhost:5173
```
*(Optional: add `SUPABASE_URL` and `SUPABASE_KEY` if using cloud storage).*

### 3. Installation
Create a virtual environment, activate it, and install dependencies:
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Running the Server
Run the FastAPI application via Uvicorn hot-reloader:
```bash
uvicorn app.main:app --reload --port 8000
```
API Documentation will be instantly available at: `http://localhost:8000/docs`

## 📁 Project Structure

```text
app/
├── main.py             # App entry, CORS, and globals
├── config.py           # Typed environment configurations
├── database.py         # MongoDB async connection manager
├── middleware/         # Auth verification and rate-limiters
├── routers/            # HTTP Endpoints (Auth, Resume, PDF, Dashboard, Notifications)
├── services/           # Business logic (AI tailoring, PDF WeasyPrint, Storage)
├── models/             # Pydantic schemas for request/response validation
├── prompts/            # Raw text structures passed into the LLM context
└── templates/          # Jinja2 HTML templates applied to PDFs
```
