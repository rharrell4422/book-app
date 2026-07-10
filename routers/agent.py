import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import crud
import schemas
from agents.book_agent import BookAgent
from routers.deps import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/run")
def run_agent(payload: schemas.AgentRunRequest):
    agent = BookAgent()
    result = agent.run(payload.title, payload.author)
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="BookAgent.run must return a metadata dict")

    found = bool(result.get("found"))
    metadata = {key: value for key, value in result.items() if key != "found"}

    return {
        "found": found,
        "metadata": metadata,
    }


@router.post("/approve", response_model=schemas.BookResponse)
def approve_agent(payload: schemas.AgentApproveRequest, db: Session = Depends(get_db)):
    found_flag = payload.found if payload.found is not None else payload.metadata.get("found")
    if found_flag is False:
        logger.warning("Manual override: creating book from /agent/approve with found=false")

    try:
        approved_book = schemas.BookBase.model_validate(payload.metadata)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid approved metadata: {exc}")

    return crud.create_book(db=db, book=approved_book)
