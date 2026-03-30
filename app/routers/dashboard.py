"""
Dashboard router: list all generated resumes for a user.
"""

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from app.middleware.auth import get_current_user_id
from app.database import get_database
from app.services.storage_service import delete_pdf
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


@router.get("")
async def get_dashboard(user_id: str = Depends(get_current_user_id)):
    """
    Get all generated resumes for the authenticated user.
    Returns metadata (summary, template, date, pdf URL) without full resume data.
    """
    db = get_database()
    generated = await db.generated_resumes.find(
        {"userId": user_id},
        {
            "modifiedData": 0,  # Exclude full resume data for performance
        },
    ).sort("createdAt", -1).to_list(length=50)

    for doc in generated:
        doc["id"] = str(doc.pop("_id"))

    return {"resumes": generated}


@router.delete("/{resume_id}")
async def delete_generated_resume(
    resume_id: str,
    user_id: str = Depends(get_current_user_id),
):
    """Delete a generated (tailored) resume and its PDF from storage."""
    db = get_database()
    try:
        # Fetch the doc first to get pdfUrl before deleting
        doc = await db.generated_resumes.find_one(
            {"_id": ObjectId(resume_id), "userId": user_id}
        )
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid resume ID.")

    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found.")

    # Delete PDF from storage (Supabase or local)
    pdf_url = doc.get("pdfUrl")
    if pdf_url:
        await delete_pdf(pdf_url)

    # Delete the DB record
    await db.generated_resumes.delete_one(
        {"_id": ObjectId(resume_id), "userId": user_id}
    )

    logger.info("generated_resume_deleted", user_id=user_id, resume_id=resume_id)
    return {"message": "Tailored resume deleted."}
