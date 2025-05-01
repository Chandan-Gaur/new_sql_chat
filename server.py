
# file: api.py

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel
from typing import Dict, Any
import os
import logging
from sql_agent_employees import SQLAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class QueryRequest(BaseModel):
    user_query: str

class QueryResponse(BaseModel):
    user_query: str
    sql_query: str
    explanation: str
    results: list
    result_count: int

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

agent: SQLAgent = None

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"Incoming request: {request.method} {request.url}")
    response = await call_next(request)
    logger.info(f"Response status: {response.status_code}")
    return response

@app.post("/query", response_model=QueryResponse)
async def process_query(request: QueryRequest):
    # Respond to greetings before any DB logic
    if request.user_query.strip().lower() in ["hi", "hello" , "HELLO" , "HI"]:
        return QueryResponse(
            user_query=request.user_query,
            sql_query="",
            explanation="Hello! I am your SQL Agent. Ask me any question about your database in natural language.",
            results=[{"": "Hello! I am your SQL Agent. Ask me any question about your database in natural language."}],
            result_count=0
        )
    database_url = os.getenv("DATABASE_URL", "your_employees_db_url_here")
    google_api_key = os.getenv("GOOGLE_API_KEY", "your_google_api_key_here")
    agent = SQLAgent(database_url, google_api_key)
    try:
        # Retry Gemini parsing once on failure
        result = agent.process_natural_language_query(request.user_query)
        if result.get("status") == "failed" and "error" in result and "JSONDecodeError" in result["error"]:
            logger.warning("Retrying Gemini query due to JSONDecodeError...")
            result = agent.process_natural_language_query(request.user_query)

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        return QueryResponse(
            user_query=result["user_query"],
            sql_query=result["sql_query"],
            explanation=result["explanation"],
            results=result["results"],
            result_count=result["result_count"]
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
    finally:
        agent.close()

@app.get("/schema", response_model=Dict[str, Any])
async def get_schema():
    database_url = os.getenv("DATABASE_URL", "your_employees_db_url_here")
    google_api_key = os.getenv("GOOGLE_API_KEY", "your_google_api_key_here")
    agent = SQLAgent(database_url, google_api_key)
    try:
        return agent.schema_info
    finally:
        agent.close()
# Endpoint to clear the schema cache
@app.post("/clear_schema_cache")
async def clear_schema_cache():
    SQLAgent.clear_schema_cache()
    return {"detail": "Schema cache cleared. It will be reloaded on the next request."}

@app.get("/health")
async def health_check():
    database_url = os.getenv("DATABASE_URL", "your_employees_db_url_here")
    google_api_key = os.getenv("GOOGLE_API_KEY", "your_google_api_key_here")
    agent = SQLAgent(database_url, google_api_key)
    try:
        agent._ensure_connection()
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Health check failed: {str(e)}")
    finally:
        agent.close()

# Run server with: `uvicorn server:app --reload`
# (Make sure you install fastapi and uvicorn)
# pip install fastapi uvicorn