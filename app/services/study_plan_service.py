import json
import logging
import asyncio
from typing import AsyncGenerator, Dict, Any, Optional, List
import hashlib
from datetime import datetime, timezone

from google.genai import types

from app.config import settings
from app.gemini_client import gemini_generate
from app.runtime import run_blocking
from app.security import sanitize_input
from app.models.study_plan import (
    StudyWeek, StudyPlanRequest, CareerGapAnalysis, PortfolioProject
)
from app.services.ai_service import _extract_json, _sanitize_resume_data, _post_process_strings

logger = logging.getLogger(__name__)

# --- Prompts ---

_GAP_ANALYSIS_PROMPT = """
You are an elite career strategist and portfolio architect. Analyze the gap between this candidate and their target role, then design a portfolio project that will be the centerpiece of their preparation.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 1 — CAREER GAP ANALYSIS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Go beyond just skills. Assess:

A. SKILL GAPS: Required/preferred skills from JD that are missing from the resume.

B. PROOF GAPS: Skills the candidate CLAIMS but has NO PROJECT OR EXPERIENCE to prove.
   Example: "Lists Docker in skills but has no containerized project or deployment experience."
   Example: "Claims AWS knowledge but no cloud-deployed application in projects."
   These are the most dangerous gaps — interviewers will probe them.

C. CAREER GAPS:
   - Missing project types (no open source, no production-scale, no team projects)
   - Missing experience patterns (no leadership, no cross-functional, no mentoring)
   - Missing domain knowledge (industry-specific terms, compliance, methodologies)

D. CERTIFICATIONS: Only mention if the JD EXPLICITLY lists them as "required."
   Many excellent engineers have zero certs — never penalize readinessScore for missing
   certifications unless the JD demands them.

E. READINESS SCORE: 0-100 with a TRANSPARENT BREAKDOWN across 5 dimensions.
   Each dimension must include a 1-sentence reason explaining the score.
   Dimensions:
   - technicalSkills (do they have the hard skills?)
   - projectEvidence (do they have PROOF of those skills?)
   - experienceAlignment (does their experience match the JD level/domain?)
   - domainKnowledge (do they understand the industry?)
   - interviewReadiness (could they pass a technical + behavioral interview today?)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 2 — PORTFOLIO PROJECT ARC (THE CENTERPIECE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Design ONE showcase-worthy project that:
- Directly addresses the biggest proof gaps identified above
- Uses the tech stack from the JD
- Can be built incrementally over {total_weeks} weeks
- Would be impressive in a portfolio or interview demo
- Has clear weekly milestones that build on each other

The project should be the thing the user remembers:
"In {total_weeks} weeks, I built [Project Name] and proved I can do [Target Role]."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 3 — PLAN SKELETON
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Week themes should follow this arc (compressed if totalWeeks < 5):
  Week 1: Foundation + Quick Wins + Project Setup
  Week 2-3: Core Skills Deep Dive + Project Core Features
  Week 4: Integration + Advanced Features + Interview Prep Start
  Week 5: Polish + Deploy + Mock Interviews

Output JSON:
{{
  "careerGapAnalysis": {{
    "currentSkills": [], "missingSkills": [], "transferableSkills": [],
    "priorityOrder": [], "missingProjectTypes": [], "missingExperience": [],
    "proofGaps": [],
    "missingCertifications": [],
    "readinessScore": 0,
    "readinessBreakdown": {{
      "technicalSkills": {{ "score": 0, "max": 100, "reason": "..." }},
      "projectEvidence": {{ "score": 0, "max": 100, "reason": "..." }},
      "experienceAlignment": {{ "score": 0, "max": 100, "reason": "..." }},
      "domainKnowledge": {{ "score": 0, "max": 100, "reason": "..." }},
      "interviewReadiness": {{ "score": 0, "max": 100, "reason": "..." }}
    }}
  }},
  "planSkeleton": {{
    "weekThemes": ["..."],
    "portfolioProjectArc": {{
      "finalProjectName": "...",
      "finalProjectDescription": "...",
      "techStack": [],
      "weeklyMilestones": ["Week 1: Setup & scaffolding", "Week 2: Core features"]
    }}
  }}
}}

CANDIDATE RESUME: {resume_json}
TARGET JD: {job_description}
MARKET DEMAND SKILLS: {market_skills}
CONFIG: {total_weeks} weeks, {hours_per_day} hrs/day, {days_per_week} days/week
FOCUS AREAS: {focus_areas}

Output ONLY valid JSON.
"""

_WEEK_GENERATION_PROMPT = """
Generate ONLY Week {week_number} of a {total_weeks}-week study plan.

THE PROJECT: "{project_name}" — {project_description}
THIS WEEK'S PROJECT MILESTONE: {milestone_from_skeleton}

CONTEXT (do not repeat content from these):
- Prior week themes: {prior_themes}
- Prior week topics covered: {prior_topics_flat_list}
- Proof gaps to address: {proof_gaps}
- Priority skills remaining: {remaining_priority_skills}

CONSTRAINTS:
- Exactly {days_per_week} days (labeled "Day 1", "Day 2", etc. — NOT calendar days)
- Each day totals {hours_per_day} hours (±10 minutes tolerance)
- Sessions between 15-120 minutes each
- Only include these focus areas: {focus_areas}
- Alternate theory and practice — every day has ≥30% hands-on time
- At least 40% of sessions must directly advance the portfolio project

THIS WEEK'S THEME: {theme_from_skeleton}

FOR EVERY SESSION, you MUST include:
1. rationale — WHY this task for THIS user. Reference specific gaps.
   Example: "Learning Docker because it appears in 78% of matching Backend roles and is missing from your resume."
   Example: "Addressing proof gap: you list Redis in skills but have no caching project."
2. practiceTask — specific, completable during the session
3. deliverable — tangible output (committed code, written notes, quiz score)
4. projectContribution — how this feeds into "{project_name}" this week
   (can be "" for pure theory sessions, but at least 40% of sessions must have this)

Do NOT include resumeBulletSuggestion — bullets are generated separately at end-of-week.

RESOURCES: Do NOT output URLs. Output a searchQuery string instead.
  Example: {{ "title": "React Official Docs — Hooks Reference", "type": "docs", "searchQuery": "react.dev hooks reference" }}

Output a single StudyWeek JSON object with this structure:
{{
  "weekNumber": {week_number},
  "theme": "...",
  "objectives": ["3-5 measurable objectives"],
  "portfolioProject": {{
    "name": "{project_name}",
    "weekMilestone": "{milestone_from_skeleton}",
    "techStack": [],
    "completionCriteria": ["specific checkable items"]
  }},
  "days": [
    {{
      "dayNumber": 1,
      "dayLabel": "Day 1",
      "totalMinutes": {target_minutes},
      "sessions": [
        {{
          "title": "specific actionable title",
          "category": "one of {focus_areas}",
          "durationMinutes": 30,
          "description": "2-3 sentences",
          "rationale": "Why this task for this user — reference gap analysis",
          "resources": [{{"title": "...", "type": "docs|video|course|article|tool", "searchQuery": "..."}}],
          "practiceTask": "specific hands-on task",
          "deliverable": "tangible output",
          "projectContribution": "how this feeds into the project (or empty)"
        }}
      ]
    }}
  ],
  "weeklyMilestone": "specific, measurable",
  "weeklyReview": "self-assessment question"
}}

Output ONLY valid JSON.
"""

_RESUME_BULLETS_PROMPT = """
You are a resume writing expert. Based on the study tasks this candidate ACTUALLY COMPLETED
this week, generate 2-3 resume bullet points they can add to their resume.

Rules:
- Only reference skills/tasks they completed (not the ones they skipped)
- Use strong action verbs + specific deliverables + quantifiable placeholders
- Each bullet should be 1 line, max 15 words
- Format: "[Action verb] [specific thing] using [tech], achieving [result placeholder]"

Completed tasks: {completed_sessions_json}
Portfolio project milestone: {project_milestone}
Target role: {target_role}

Output JSON: {{ "bullets": ["...", "...", "..."] }}
"""

# --- Helpers ---

def _generate_session_id(week_num: int, day_num: int, session_idx: int, title: str) -> str:
    """Generate a stable session ID."""
    hash_str = hashlib.md5(title.encode('utf-8')).hexdigest()[:4]
    return f"w{week_num}_d{day_num}_s{session_idx}_{hash_str}"


def _validate_week(week: StudyWeek, config: StudyPlanRequest) -> list[str]:
    """Server-side validation — catches what LLMs miss."""
    errors = []
    
    # 1. Day count
    if len(week.days) != config.daysPerWeek:
        errors.append(f"Expected {config.daysPerWeek} days, got {len(week.days)}")
    
    # 2. Time math per day (±10 min tolerance)
    target_minutes = int(config.hoursPerDay * 60)
    for day in week.days:
        actual = sum(s.durationMinutes for s in day.sessions)
        if abs(actual - target_minutes) > 10:
            errors.append(f"Day {day.dayNumber}: expected ~{target_minutes}min, got {actual}min")
    
    # 3. Session duration bounds
    for day in week.days:
        for s in day.sessions:
            if s.durationMinutes < 15 or s.durationMinutes > 120:
                errors.append(f"Session '{s.title}' has invalid duration: {s.durationMinutes}min")
    
    # 4. Required fields
    for day in week.days:
        for s in day.sessions:
            if not s.practiceTask:
                errors.append(f"Session '{s.title}' missing practiceTask")
            if not s.deliverable:
                errors.append(f"Session '{s.title}' missing deliverable")
            if not s.rationale:
                errors.append(f"Session '{s.title}' missing rationale")
                
    return errors


# --- Core Generation Logic ---

async def generate_study_plan_stream(
    user_id: str,
    request: StudyPlanRequest,
    resume_data: dict,
    market_skills: list[str]
) -> AsyncGenerator[str, None]:
    """
    Main orchestrator — generates plan week-by-week with validation.
    Yields SSE events (JSON strings).
    """
    try:
        jd_safe = sanitize_input(request.jobDescription) if request.jobDescription else ""
        
        # 1. Generate Gap Analysis + Skeleton
        yield json.dumps({"type": "progress", "message": "Analyzing career gaps and designing portfolio project..."})
        
        gap_prompt = _GAP_ANALYSIS_PROMPT.format(
            total_weeks=request.totalWeeks,
            resume_json=json.dumps(resume_data),
            job_description=jd_safe,
            market_skills=json.dumps(market_skills),
            hours_per_day=request.hoursPerDay,
            days_per_week=request.daysPerWeek,
            focus_areas=json.dumps(request.focusAreas)
        )
        
        gap_response = await run_blocking(
            gemini_generate,
            contents=gap_prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.3),
            timeout=settings.GEMINI_TIMEOUT_SECONDS,
        )
        
        raw_gap_json = _extract_json(gap_response.text)
        gap_data = json.loads(raw_gap_json)
        gap_data = _sanitize_resume_data(gap_data)
        gap_data = _post_process_strings(gap_data)
        
        career_gap_analysis = CareerGapAnalysis.model_validate(gap_data.get("careerGapAnalysis", {}))
        skeleton = gap_data.get("planSkeleton", {})
        project_arc = skeleton.get("portfolioProjectArc", {})
        week_themes = skeleton.get("weekThemes", [])
        
        yield json.dumps({
            "type": "gap_analysis", 
            "data": career_gap_analysis.model_dump(),
            "projectArc": project_arc,
            "planSkeleton": skeleton
        })
        
        # 2. Generate each week
        all_weeks = []
        prior_themes = []
        prior_topics = []
        
        for w in range(1, request.totalWeeks + 1):
            yield json.dumps({"type": "progress", "message": f"Generating Week {w}..."})
            
            theme = week_themes[w-1] if w-1 < len(week_themes) else f"Advanced Topics for Week {w}"
            milestones = project_arc.get("weeklyMilestones", [])
            milestone = milestones[w-1] if w-1 < len(milestones) else "Continue project development"
            
            week_prompt = _WEEK_GENERATION_PROMPT.format(
                week_number=w,
                total_weeks=request.totalWeeks,
                project_name=project_arc.get("finalProjectName", "Portfolio Project"),
                project_description=project_arc.get("finalProjectDescription", ""),
                milestone_from_skeleton=milestone,
                prior_themes=json.dumps(prior_themes),
                prior_topics_flat_list=json.dumps(prior_topics),
                proof_gaps=json.dumps(career_gap_analysis.proofGaps),
                remaining_priority_skills=json.dumps(career_gap_analysis.priorityOrder),
                days_per_week=request.daysPerWeek,
                hours_per_day=request.hoursPerDay,
                target_minutes=int(request.hoursPerDay * 60),
                focus_areas=json.dumps(request.focusAreas),
                theme_from_skeleton=theme
            )
            
            # Try generation with 1 retry for validation
            week_data = None
            validation_errors = []
            
            for attempt in range(2):
                if attempt == 1 and validation_errors:
                    logger.info("Retrying week generation due to validation errors", extra={"errors": validation_errors})
                    week_prompt += f"\n\nIMPORTANT PREVIOUS ERRORS TO FIX:\n" + "\n".join(validation_errors)
                    
                    week_response = await run_blocking(
                        gemini_generate,
                        contents=week_prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.4),
                        timeout=settings.GEMINI_TIMEOUT_SECONDS,
                    )
                
                try:
                    raw_week_json = _extract_json(week_response.text)
                    parsed_week = json.loads(raw_week_json)
                    parsed_week = _sanitize_resume_data(parsed_week)
                    parsed_week = _post_process_strings(parsed_week)
                    
                    week_obj = StudyWeek.model_validate(parsed_week)
                    week_obj.weekNumber = w  # force correct week number (LLM often returns 1 for all)
                    validation_errors = _validate_week(week_obj, request)
                    
                    if not validation_errors:
                        week_data = week_obj
                        break
                except Exception as e:
                    validation_errors = [f"JSON/Schema parsing error: {str(e)}"]
            
            if not week_data:
                # If it still fails, use the last parsed one if available, otherwise skip or error
                yield json.dumps({"type": "error", "message": f"Failed to generate valid Week {w} after retries."})
                if 'week_obj' in locals():
                    week_data = week_obj
                else:
                    raise ValueError(f"Could not generate valid Week {w}")
            
            # Assign stable session IDs
            for d_idx, day in enumerate(week_data.days):
                for s_idx, session in enumerate(day.sessions):
                    session.sessionId = _generate_session_id(w, day.dayNumber, s_idx, session.title)
                    prior_topics.append(session.title)
            
            prior_themes.append(week_data.theme)
            all_weeks.append(week_data.model_dump())
            
            yield json.dumps({
                "type": "week_generated",
                "weekNumber": w,
                "data": week_data.model_dump()
            })
            
        # 3. Finalize
        yield json.dumps({
            "type": "complete",
            "message": "Study plan generated successfully",
            "projectArc": project_arc,
            "weeks": all_weeks,
            "careerGapAnalysis": career_gap_analysis.model_dump()
        })
        
    except Exception as e:
        logger.exception("Error generating study plan")
        yield json.dumps({"type": "error", "message": str(e)})


async def generate_resume_bullets(completed_sessions: list[dict], project_milestone: str, target_role: str) -> list[str]:
    """Generate 2-3 resume bullets based on completed tasks."""
    prompt = _RESUME_BULLETS_PROMPT.format(
        completed_sessions_json=json.dumps(completed_sessions),
        project_milestone=project_milestone,
        target_role=target_role
    )
    
    response = await run_blocking(
        gemini_generate,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0.3),
        timeout=settings.GEMINI_TIMEOUT_SECONDS,
    )
    
    try:
        raw_json = _extract_json(response.text)
        data = json.loads(raw_json)
        data = _sanitize_resume_data(data)
        data = _post_process_strings(data)
        return data.get("bullets", [])
    except Exception as e:
        logger.error(f"Failed to generate resume bullets: {str(e)}")
        return []
