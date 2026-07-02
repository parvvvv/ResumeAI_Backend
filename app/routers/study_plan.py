from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
from fastapi.responses import StreamingResponse
from typing import List, Optional
import json
from datetime import datetime, timezone
from bson import ObjectId
from pymongo import ASCENDING, DESCENDING

from app.database import get_database
from app.models.study_plan import StudyPlanRequest, StudyProgressUpdate, RegenerateWeekRequest
from app.services.study_plan_service import (
    generate_study_plan_stream, generate_resume_bullets,
    _WEEK_GENERATION_PROMPT, _validate_week, _generate_session_id,
    _difficulty_guidance,
)
from app.gemini_client import gemini_generate
from app.runtime import run_blocking
from app.services.ai_service import _extract_json, _sanitize_resume_data, _post_process_strings
from app.services.llm_usage_service import bind_llm_context
from app.middleware.auth import get_current_user_id
from app.middleware.rate_limit import limiter
from app.config import settings
from app.security import sanitize_input
from google.genai import types
import structlog

logger = structlog.get_logger()
router = APIRouter(prefix="/api/study-plan", tags=["study-plan"])

@router.post("/generate")
@limiter.limit(settings.RATE_LIMIT_STUDY_PLAN)
async def generate_study_plan(
    request: Request,
    body: StudyPlanRequest,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_database)
):
    """
    Generate a study plan based on target job description and resume.
    Streams progress and generated weeks via Server-Sent Events (SSE).
    """
    # 1. Enforce Quota
    active_plans_count = await db.study_plans.count_documents({
        "userId": user_id, 
        "status": "active"
    })
    
    # Check if admin to bypass quota
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    is_admin = user and user.get("role") == "admin"
    
    if not is_admin and active_plans_count >= settings.MAX_STUDY_PLANS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You have reached the maximum of {settings.MAX_STUDY_PLANS_PER_USER} active study plans. Archive an existing plan to create a new one."
        )

    # 2. Get the resume data
    resume_data = {}
    if body.generatedResumeId:
        generated_resume = await db.generated_resumes.find_one({
            "_id": ObjectId(body.generatedResumeId),
            "userId": user_id
        })
        if not generated_resume:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
        resume_data = generated_resume.get("modifiedData", {})
    else:
        # Fallback to the latest base resume
        base_resume = await db.base_resumes.find_one(
            {"userId": user_id},
            sort=[("createdAt", DESCENDING)]
        )
        if not base_resume:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No resume found to generate plan")
        resume_data = base_resume.get("resumeData", {})

    # 3. Market demand skills (aggregate from jobs collection)
    pipeline = [
        {"$match": {"userId": user_id}},
        {"$unwind": "$highlights.Qualifications"},
        {"$group": {"_id": "$highlights.Qualifications", "count": {"$sum": 1}}},
        {"$sort": {"count": DESCENDING}},
        {"$limit": 20}
    ]
    market_skills_cursor = db.jobs.aggregate(pipeline)
    market_skills_docs = await market_skills_cursor.to_list(length=20)
    market_skills = [doc["_id"] for doc in market_skills_docs if isinstance(doc.get("_id"), str)]
    
    # 4. Stream Response
    async def sse_generator():
        try:
            bind_llm_context(user_id=user_id, feature="study_plan_generate")
            # We will collect the data to save to the database
            plan_doc = {
                "userId": user_id,
                "generatedResumeId": body.generatedResumeId,
                "jobDescription": sanitize_input(body.jobDescription) if body.jobDescription else "",
                "version": settings.STUDY_PLAN_SCHEMA_VERSION,
                "status": "active",
                "startedAt": None, # Set when user explicitly starts
                "config": body.model_dump(),
                "plan": {
                    "metadata": {"generatedAt": datetime.now(timezone.utc).isoformat(), "targetRole": "Target Role"},
                    "projectArc": {},
                    "careerGapAnalysis": {},
                    "weeklyPlan": []
                },
                "progress": {},
                "generatedBullets": {},
                "streak": {
                    "current": 0,
                    "longest": 0,
                    "lastCompletionDay": 0,
                    "history": []
                },
                "metrics": {
                    "completionPercent": 0,
                    "sessionsCompleted": 0,
                    "sessionsTotal": 0,
                    "lastAccessedAt": datetime.now(timezone.utc)
                },
                "createdAt": datetime.now(timezone.utc),
                "updatedAt": datetime.now(timezone.utc)
            }
            
            async for chunk in generate_study_plan_stream(user_id, body, resume_data, market_skills):
                # The service yields JSON strings that we send as SSE
                try:
                    parsed_chunk = json.loads(chunk)
                    chunk_type = parsed_chunk.get("type")
                    
                    if chunk_type == "gap_analysis":
                        plan_doc["plan"]["careerGapAnalysis"] = parsed_chunk.get("data", {})
                        plan_doc["plan"]["projectArc"] = parsed_chunk.get("projectArc", {})
                        # Extract targetRole from planSkeleton if available
                        skeleton = parsed_chunk.get("planSkeleton", {})
                        if skeleton.get("targetRole"):
                            plan_doc["plan"]["metadata"]["targetRole"] = skeleton["targetRole"]
                    elif chunk_type == "week_generated":
                        plan_doc["plan"]["weeklyPlan"].append(parsed_chunk.get("data", {}))
                        
                        # Add session tracking
                        for day in parsed_chunk.get("data", {}).get("days", []):
                            for session in day.get("sessions", []):
                                session_id = session.get("sessionId")
                                if session_id:
                                    plan_doc["progress"][session_id] = False
                                    plan_doc["metrics"]["sessionsTotal"] += 1
                                    
                    elif chunk_type == "complete":
                        # Save to database
                        result = await db.study_plans.insert_one(plan_doc)
                        parsed_chunk["planId"] = str(result.inserted_id)
                        chunk = json.dumps(parsed_chunk)
                        
                except json.JSONDecodeError:
                    pass
                
                yield f"data: {chunk}\n\n"
        except Exception as e:
            logger.exception("SSE generation failed")
            yield f"data: {json.dumps({'type': 'error', 'message': 'Internal Server Error'})}\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")

@router.get("/list")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def list_study_plans(
    request: Request,
    status_filter: str = Query("active", alias="status"),
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_database)
):
    """Get all study plans for the user, filtered by status."""
    cursor = db.study_plans.find(
        {"userId": user_id, "status": status_filter},
        {"plan.weeklyPlan": 0} # Don't return the full plan content to save bandwidth
    ).sort("createdAt", DESCENDING)
    
    plans = await cursor.to_list(length=100)
    for p in plans:
        p["id"] = str(p.pop("_id"))
        
    return plans

@router.get("/{plan_id}")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_study_plan(
    request: Request,
    plan_id: str,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_database)
):
    """Get a specific study plan by ID."""
    plan = await db.study_plans.find_one({"_id": ObjectId(plan_id), "userId": user_id})
    if not plan:
        raise HTTPException(status_code=404, detail="Study plan not found")
        
    plan["id"] = str(plan.pop("_id"))
    
    # Update lastAccessedAt
    await db.study_plans.update_one(
        {"_id": ObjectId(plan_id)},
        {"$set": {
            "metrics.lastAccessedAt": datetime.now(timezone.utc),
            "updatedAt": datetime.now(timezone.utc)
        }}
    )
    
    return plan

@router.patch("/{plan_id}/progress")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def update_progress(
    request: Request,
    plan_id: str,
    update: StudyProgressUpdate,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_database)
):
    """Toggle completion status of a specific session and calculate streaks."""
    plan = await db.study_plans.find_one({"_id": ObjectId(plan_id), "userId": user_id})
    if not plan:
        raise HTTPException(status_code=404, detail="Study plan not found")
        
    days_per_week = plan.get("config", {}).get("daysPerWeek", 5)

    # Compute global day index (sequential across weeks, not resetting per week)
    def _get_global_day(session_id):
        for week_idx, week in enumerate(plan.get("plan", {}).get("weeklyPlan", [])):
            for day in week.get("days", []):
                for session in day.get("sessions", []):
                    if session.get("sessionId") == session_id:
                        return week_idx * days_per_week + day.get("dayNumber", 0)
        return 0

    session_global_day = _get_global_day(update.sessionId)
    if session_global_day == 0:
        raise HTTPException(status_code=400, detail="Session not found in plan")

    current_val = plan.get("progress", {}).get(update.sessionId, False)
    
    if current_val == update.completed:
        return {"status": "unchanged"}
        
    update_ops = {
        f"progress.{update.sessionId}": update.completed,
        "updatedAt": datetime.now(timezone.utc)
    }
    
    streak_data = plan.get("streak", {"current": 0, "longest": 0, "lastCompletionDay": 0, "history": []})
    
    if plan.get("startedAt") is None and update.completed:
        update_ops["startedAt"] = datetime.now(timezone.utc)

    sessions_completed = plan.get("metrics", {}).get("sessionsCompleted", 0)
    sessions_completed += 1 if update.completed else -1
        
    total = plan.get("metrics", {}).get("sessionsTotal", 1)
    completion_percent = max(0, min(100, int((sessions_completed / total) * 100)))
    
    update_ops["metrics.sessionsCompleted"] = sessions_completed
    update_ops["metrics.completionPercent"] = completion_percent

    await db.study_plans.update_one(
        {"_id": ObjectId(plan_id)},
        {"$set": update_ops}
    )
    
    # Recalculate streaks using global day indices
    plan = await db.study_plans.find_one({"_id": ObjectId(plan_id), "userId": user_id})
    history = set()
    for sid, is_completed in plan.get("progress", {}).items():
        if is_completed:
            gd = _get_global_day(sid)
            if gd > 0:
                history.add(gd)

    sorted_history = sorted(history)
    current_streak = 0
    longest_streak = streak_data.get("longest", 0)
    last_completion_day = 0
    
    if sorted_history:
        current_streak = 1
        last_completion_day = sorted_history[-1]
        for i in range(len(sorted_history) - 1, 0, -1):
            if sorted_history[i] - sorted_history[i-1] == 1:
                current_streak += 1
            else:
                break
        
        longest_streak = max(longest_streak, current_streak)

    streak_update = {
        "streak.current": current_streak,
        "streak.longest": longest_streak,
        "streak.lastCompletionDay": last_completion_day,
        "streak.history": sorted_history
    }
    
    await db.study_plans.update_one(
        {"_id": ObjectId(plan_id)},
        {"$set": streak_update}
    )

    return {"status": "success", "sessionsCompleted": sessions_completed, "completionPercent": completion_percent, "streak": current_streak}

@router.patch("/{plan_id}/archive")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def archive_plan(
    request: Request,
    plan_id: str,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_database)
):
    """Soft delete a plan by moving it to archived status."""
    result = await db.study_plans.update_one(
        {"_id": ObjectId(plan_id), "userId": user_id},
        {"$set": {"status": "archived", "updatedAt": datetime.now(timezone.utc)}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Study plan not found")
        
    return {"status": "archived"}

@router.patch("/{plan_id}/restore")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def restore_plan(
    request: Request,
    plan_id: str,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_database)
):
    """Restore an archived plan back to active status."""
    # Check quota first
    active_plans_count = await db.study_plans.count_documents({
        "userId": user_id, 
        "status": "active"
    })
    
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    is_admin = user and user.get("role") == "admin"
    
    if not is_admin and active_plans_count >= settings.MAX_STUDY_PLANS_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"You have reached the maximum of {settings.MAX_STUDY_PLANS_PER_USER} active study plans. You must archive one before restoring another."
        )

    result = await db.study_plans.update_one(
        {"_id": ObjectId(plan_id), "userId": user_id},
        {"$set": {"status": "active", "updatedAt": datetime.now(timezone.utc)}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=404, detail="Study plan not found")
        
    return {"status": "active"}

@router.post("/{plan_id}/regenerate-week/{week_num}")
@limiter.limit(settings.RATE_LIMIT_STUDY_PLAN)
async def regenerate_week(
    request: Request,
    plan_id: str,
    week_num: int,
    body: RegenerateWeekRequest,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_database)
):
    """Regenerate a single week of the plan, preserving progress on other weeks."""
    bind_llm_context(user_id=user_id, feature="study_plan_regenerate_week")
    plan = await db.study_plans.find_one({"_id": ObjectId(plan_id), "userId": user_id})
    if not plan:
        raise HTTPException(status_code=404, detail="Study plan not found")

    # Check if sessions in this week have progress
    has_progress = False
    for w in plan.get("plan", {}).get("weeklyPlan", []):
        if w.get("weekNumber") == week_num:
            for day in w.get("days", []):
                for session in day.get("sessions", []):
                    if plan.get("progress", {}).get(session.get("sessionId"), False):
                        has_progress = True
                    if has_progress:
                        break
            break

    if has_progress and not body.confirmReset:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This week has completed sessions. Set confirmReset=true to proceed."
        )

    # Get the config and gap analysis for context
    config = StudyPlanRequest(**plan.get("config", {}))
    gap_analysis = plan.get("plan", {}).get("careerGapAnalysis", {})
    project_arc = plan.get("plan", {}).get("projectArc", {})
    week_themes = [w.get("theme", "") for w in plan.get("plan", {}).get("weeklyPlan", [])]
    prior_themes = [w.get("theme", "") for w in plan.get("plan", {}).get("weeklyPlan", []) if w.get("weekNumber", 0) < week_num]

    # Collect prior topics from earlier weeks
    prior_topics = []
    for w in plan.get("plan", {}).get("weeklyPlan", []):
        if w.get("weekNumber", 0) < week_num:
            for day in w.get("days", []):
                for session in day.get("sessions", []):
                    prior_topics.append(session.get("title", ""))

    # Generate just this week
    theme = week_themes[week_num - 1] if week_num - 1 < len(week_themes) else f"Advanced Topics for Week {week_num}"
    milestones = project_arc.get("weeklyMilestones", [])
    milestone = milestones[week_num - 1] if week_num - 1 < len(milestones) else "Continue project development"

    total_weeks = plan.get("config", {}).get("totalWeeks", 3)

    week_prompt = _WEEK_GENERATION_PROMPT.format(
        week_number=week_num,
        total_weeks=total_weeks,
        difficulty_guidance=_difficulty_guidance(week_num, total_weeks),
        project_name=project_arc.get("finalProjectName", "Portfolio Project"),
        project_description=project_arc.get("finalProjectDescription", ""),
        milestone_from_skeleton=milestone,
        prior_themes=json.dumps(prior_themes),
        prior_topics_flat_list=json.dumps(prior_topics),
        proof_gaps=json.dumps(gap_analysis.get("proofGaps", [])),
        remaining_priority_skills=json.dumps(gap_analysis.get("priorityOrder", [])),
        days_per_week=plan.get("config", {}).get("daysPerWeek", 5),
        hours_per_day=plan.get("config", {}).get("hoursPerDay", 2.0),
        target_minutes=int(plan.get("config", {}).get("hoursPerDay", 2.0) * 60),
        focus_areas=json.dumps(plan.get("config", {}).get("focusAreas", [])),
        theme_from_skeleton=theme
    )

    week_data = None
    validation_errors = []

    for attempt in range(2):
        if attempt == 1 and validation_errors:
            week_prompt += f"\n\nIMPORTANT PREVIOUS ERRORS TO FIX:\n" + "\n".join(validation_errors)

        response = await run_blocking(
            gemini_generate,
            contents=week_prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4),
            timeout=settings.GEMINI_TIMEOUT_SECONDS,
        )

        try:
            raw_json = _extract_json(response.text)
            parsed = json.loads(raw_json)
            parsed = _sanitize_resume_data(parsed)
            parsed = _post_process_strings(parsed)

            from app.models.study_plan import StudyWeek
            week_obj = StudyWeek.model_validate(parsed)
            week_obj.weekNumber = week_num
            validation_errors = _validate_week(week_obj, config)

            if not validation_errors:
                week_data = week_obj
                break
        except Exception as e:
            validation_errors = [f"JSON/Schema parsing error: {str(e)}"]

    if not week_data:
        raise HTTPException(status_code=500, detail=f"Failed to generate valid Week {week_num} after retries.")

    # Assign new stable session IDs
    for d_idx, day in enumerate(week_data.days):
        for s_idx, session in enumerate(day.sessions):
            session.sessionId = _generate_session_id(week_num, day.dayNumber, s_idx, session.title)

    # Remove old progress entries for this week
    old_progress_keys = []
    for w in plan.get("plan", {}).get("weeklyPlan", []):
        if w.get("weekNumber") == week_num:
            for day in w.get("days", []):
                for session in day.get("sessions", []):
                    old_sid = session.get("sessionId")
                    if old_sid:
                        old_progress_keys.append(f"progress.{old_sid}")
            break

    week_data_dict = week_data.model_dump()
    week_update_key = f"plan.weeklyPlan.{week_num - 1}"

    unset_ops = {k: "" for k in old_progress_keys}
    set_ops = {
        week_update_key: week_data_dict,
        "updatedAt": datetime.now(timezone.utc)
    }

    if unset_ops:
        await db.study_plans.update_one(
            {"_id": ObjectId(plan_id)},
            {"$unset": unset_ops}
        )

    await db.study_plans.update_one(
        {"_id": ObjectId(plan_id)},
        {"$set": set_ops}
    )

    # Recalculate totals and completion percent
    fresh_plan = await db.study_plans.find_one({"_id": ObjectId(plan_id)})
    total_sessions = 0
    completed_sessions = 0
    for w in fresh_plan.get("plan", {}).get("weeklyPlan", []):
        for day in w.get("days", []):
            for session in day.get("sessions", []):
                total_sessions += 1
                if fresh_plan.get("progress", {}).get(session.get("sessionId"), False):
                    completed_sessions += 1

    comp_pct = int((completed_sessions / max(total_sessions, 1)) * 100)
    await db.study_plans.update_one(
        {"_id": ObjectId(plan_id)},
        {"$set": {
            "metrics.sessionsTotal": total_sessions,
            "metrics.sessionsCompleted": completed_sessions,
            "metrics.completionPercent": comp_pct
        }}
    )

    return {"status": "success", "week": week_data_dict}


@router.post("/{plan_id}/resume-bullets/{week_num}")
@limiter.limit(settings.RATE_LIMIT_RESUME_BULLETS)
async def get_resume_bullets(
    request: Request,
    plan_id: str,
    week_num: int,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_database)
):
    """Generate resume bullets for completed tasks in a given week."""
    bind_llm_context(user_id=user_id, feature="study_plan_resume_bullets")
    plan = await db.study_plans.find_one({"_id": ObjectId(plan_id), "userId": user_id})
    if not plan:
        raise HTTPException(status_code=404, detail="Study plan not found")
        
    # Find the week and its completed sessions
    week_data = None
    completed_sessions = []
    
    for w in plan.get("plan", {}).get("weeklyPlan", []):
        if w.get("weekNumber") == week_num:
            week_data = w
            for day in w.get("days", []):
                for session in day.get("sessions", []):
                    # Check if session is completed
                    if plan.get("progress", {}).get(session.get("sessionId"), False):
                        completed_sessions.append({
                            "title": session.get("title"),
                            "practiceTask": session.get("practiceTask"),
                            "deliverable": session.get("deliverable"),
                            "projectContribution": session.get("projectContribution")
                        })
            break
            
    if not week_data:
        raise HTTPException(status_code=404, detail=f"Week {week_num} not found in plan")
        
    # We require at least 1 completed task to generate bullets
    if not completed_sessions:
        raise HTTPException(status_code=400, detail="Cannot generate bullets: no tasks completed in this week.")
        
    # Actually, verify that 50% of the tasks are complete (optional but good practice per PRD)
    total_week_sessions = sum(len(d.get("sessions", [])) for d in week_data.get("days", []))
    if len(completed_sessions) / max(total_week_sessions, 1) < 0.5:
        # We'll log a warning but still generate them if the user asks explicitly
        logger.warning(f"Generating bullets for week {week_num} with < 50% completion")

    project_milestone = week_data.get("portfolioProject", {}).get("weekMilestone", "")
    target_role = plan.get("plan", {}).get("metadata", {}).get("targetRole", "Target Role")
    
    bullets = await generate_resume_bullets(completed_sessions, project_milestone, target_role)
    
    # Save the generated bullets to the DB
    bullet_key = f"week_{week_num}"
    await db.study_plans.update_one(
        {"_id": ObjectId(plan_id)},
        {"$set": {
            f"generatedBullets.{bullet_key}": bullets,
            "updatedAt": datetime.now(timezone.utc)
        }}
    )
    
    return {"bullets": bullets}

@router.get("/{plan_id}/streak")
@limiter.limit(settings.RATE_LIMIT_GENERAL)
async def get_streak(
    request: Request,
    plan_id: str,
    user_id: str = Depends(get_current_user_id),
    db=Depends(get_database)
):
    """Get the current streak and a daily quote."""
    plan = await db.study_plans.find_one(
        {"_id": ObjectId(plan_id), "userId": user_id},
        {"streak": 1}
    )
    if not plan:
        raise HTTPException(status_code=404, detail="Study plan not found")
        
    streak = plan.get("streak", {}).get("current", 0)
    
    # We'll just return the streak here; the frontend will determine the quote deterministically based on dayOfYear
    return {"streak": streak}
