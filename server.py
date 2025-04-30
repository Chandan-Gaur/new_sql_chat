# file: api.py

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any
import os
from sql_agent_employees import SQLAgent

# Define Request Model
class QueryRequest(BaseModel):
    user_query: str

# Define Response Model
class QueryResponse(BaseModel):
    user_query: str
    sql_query: str
    explanation: str
    results: list
    result_count: int

# Initialize FastAPI app
app = FastAPI()

# CORS settings (adjust origins as needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: Replace with your frontend domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global SQLAgent instance
agent: SQLAgent = None

@app.on_event("startup")
async def startup_event():
    global agent
    database_url = os.getenv("DATABASE_URL", "your_employees_db_url_here")
    google_api_key = os.getenv("GOOGLE_API_KEY", "your_google_api_key_here")
    agent = SQLAgent(database_url, google_api_key)

@app.on_event("shutdown")
async def shutdown_event():
    if agent:
        agent.close()

@app.post("/query", response_model=QueryResponse)
async def process_query(request: QueryRequest):
    try:
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

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/schema", response_model=Dict[str, Any])
async def get_schema():
    if not agent:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    return agent.schema_info

# Run server with: `uvicorn api:app --reload`
# (Make sure you install fastapi and uvicorn)
# pip install fastapi uvicorn
