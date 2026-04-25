import time
import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services.chat_service import chat_service
from app.middleware.rate_limit import limiter
from app.config import settings

logger = structlog.get_logger()
router = APIRouter(prefix="/api/chat", tags=["Chat"])

class ChatRequest(BaseModel):
    query: str

class ChatResponse(BaseModel):
    answer: str

@router.post("", response_model=ChatResponse)
@limiter.limit(settings.RATE_LIMIT_AI)
async def chat_endpoint(request: Request, body: ChatRequest):
    """
    RAG Chatbot API using Supabase Vector Store
    Retrieves knowledge context locally and responds via Gemini.
    """
    start_time = time.time()
    query = body.query.strip()
    
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")
        
    logger.info("rag_chat_request", query=query)
    
    try:
        if not chat_service.initialized:
            raise HTTPException(status_code=500, detail="Database connection currently offline.")

        answer = await chat_service.retrieve_and_answer(query)
        
        total_time = round(time.time() - start_time, 2)
        logger.info("rag_chat_success", query=query, time_secs=total_time, answer_snippet=answer[:50])
        
        return ChatResponse(answer=answer)
        
    except Exception as e:
        import traceback
        full_traceback = traceback.format_exc()
        logger.error("rag_chat_error", query=query, error=str(e), traceback=full_traceback)
        raise HTTPException(status_code=500, detail="An error occurred while generating the response.")
