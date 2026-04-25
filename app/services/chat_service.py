from __future__ import annotations

import asyncio
import logging

from supabase import create_client, Client

from langchain_google_genai import GoogleGenerativeAIEmbeddings, ChatGoogleGenerativeAI
from langchain.prompts import PromptTemplate

from app.config import settings
from app.runtime import get_runtime, run_blocking

STRICT_RAG_PROMPT = """You are a helpful assistant for this website.
Answer ONLY using the context below.
If the answer is not in the context, say you don’t know.

Context:
{context}

User Question:
{query}"""

class ChatService:
    def __init__(self):
        # We wrap in a try-except to avoid crashing the server if keys are missing
        try:
            self.supabase: Client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
            self.embeddings = GoogleGenerativeAIEmbeddings(
                model="models/gemini-embedding-001",
                google_api_key=settings.GEMINI_API_KEY
            )
            # We bypass Langchain's SupabaseVectorStore entirely here due to version incompatibilities 
            # with the supabase python client. We will natively query the RPC instead.
            

            # Use fast model for streaming/rapid RAG
            self.llm = ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                google_api_key=settings.GEMINI_API_KEY,
                temperature=0.0
            )
            self.prompt = PromptTemplate.from_template(STRICT_RAG_PROMPT)
            
            self.initialized = True
        except Exception as e:
            logging.error(f"Failed to initialize ChatService: {e}")
            self.initialized = False

    async def retrieve_and_answer(self, query: str) -> str:
        if not self.initialized:
            raise Exception("ChatService is not initialized correctly with DB credentials.")

        runtime = get_runtime()
        async with runtime.chat_semaphore:
            # 1. Retrieve Docs using Native RPC
            query_embedding = await run_blocking(
                self.embeddings.embed_query,
                query,
                timeout=settings.GEMINI_TIMEOUT_SECONDS,
            )
            try:
                res = await run_blocking(
                    lambda: self.supabase.rpc(
                        "match_documents",
                        {
                            "query_embedding": query_embedding,
                            "match_count": 4,
                            "filter": {},
                        },
                    ).execute(),
                    timeout=settings.SUPABASE_RPC_TIMEOUT_SECONDS,
                )
                docs = res.data if res and res.data else []
            except asyncio.TimeoutError:
                logging.error("Supabase RPC timed out during chat retrieval.")
                docs = []
            except Exception as e:
                logging.error(f"Supabase RPC Error: {e}")
                docs = []

            if not docs:
                logging.warning("No relevant chunks found in Supabase.")
                return "I don't know."

            # 2. Format Context & Log snippets
            context_text = ""
            for i, doc in enumerate(docs):
                content = doc.get("content", "")
                metadata = doc.get("metadata", {})
                logging.info(f"--- Retrieved Chunk {i+1} ---")
                logging.info(f"Source metadata: {metadata}")
                logging.info(f"Content snippet: {content[:120]}...\n")
                context_text += f"{content}\n\n"

            final_prompt = self.prompt.format(context=context_text.strip(), query=query)

            # 3. Predict answer
            response = await run_blocking(
                self.llm.invoke,
                final_prompt,
                timeout=settings.GEMINI_TIMEOUT_SECONDS,
            )
            return response.content

chat_service = ChatService()
