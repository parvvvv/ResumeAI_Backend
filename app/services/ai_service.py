"""
AI service: Gemini integration for resume parsing and tailoring.
Uses google-genai SDK with retry logic and strict schema validation.
"""

import html
import json
import re
import asyncio
from pathlib import Path
from google import genai
from google.genai import types
from app.config import settings
from app.models.resume import ResumeData, TailorResponse
from app.runtime import run_blocking
from app.security import sanitize_input
import structlog

logger = structlog.get_logger()

# Initialize Gemini client
_client = genai.Client(api_key=settings.GEMINI_API_KEY)

# Load prompt templates
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PARSE_PROMPT = (_PROMPTS_DIR / "parse.txt").read_text()
_TAILOR_PROMPT = (_PROMPTS_DIR / "tailor.txt").read_text()

_MAX_RETRIES = 2


def _extract_json(text: str) -> str:
    """
    Extract JSON from AI response text.
    Handles cases where the AI wraps JSON in markdown code blocks.
    """
    # Try to find JSON in code blocks first
    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if code_block_match:
        return code_block_match.group(1).strip()

    # Try to find raw JSON (starts with {)
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        return json_match.group(0).strip()

    return text.strip()


from typing import Any

def _sanitize_resume_data(data: Any) -> Any:
    """Sanitize all string fields in resume data to remove any HTML/JS."""
    if isinstance(data, dict):
        return {k: _sanitize_resume_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_sanitize_resume_data(item) for item in data]
    elif isinstance(data, str):
        return sanitize_input(data)
    return data


def _post_process_strings(data: Any) -> Any:
    """Decode HTML entities and normalize dashes in all string fields."""
    if isinstance(data, dict):
        return {k: _post_process_strings(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_post_process_strings(item) for item in data]
    elif isinstance(data, str):
        # Decode HTML entities (e.g. &amp; -> &) left by bleach
        text = html.unescape(data)
        # Replace em dashes with en dashes for cleaner PDF rendering
        text = text.replace("\u2014", "\u2013")  # — → –
        return text
    return data


async def parse_resume(raw_text: str) -> ResumeData:
    """
    Parse raw resume text into structured ResumeData using Gemini AI.
    Retries up to MAX_RETRIES times if the response is not valid JSON.
    """
    prompt = _PARSE_PROMPT.format(resume_text=raw_text)

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info("ai_parse_attempt", attempt=attempt)

            response = await run_blocking(
                _client.models.generate_content,
                model=settings.GEMINI_MODEL,
                contents=prompt,
                timeout=settings.GEMINI_TIMEOUT_SECONDS,
            )

            raw_json = _extract_json(response.text)
            parsed_dict = json.loads(raw_json)

            # Sanitize AI output
            sanitized = _sanitize_resume_data(parsed_dict)
            sanitized = _post_process_strings(sanitized)

            # Validate against strict schema
            resume_data = ResumeData.model_validate(sanitized)

            logger.info("ai_parse_success", attempt=attempt)
            return resume_data

        except (asyncio.TimeoutError, json.JSONDecodeError, Exception) as e:
            last_error = e
            logger.warning("ai_parse_failed", attempt=attempt, error=str(e))

            # Add correction hint to prompt for retry
            if attempt < _MAX_RETRIES:
                prompt = (
                    f"{prompt}\n\n"
                    f"IMPORTANT: Your previous response was not valid JSON. "
                    f"Error: {str(e)}. Please output ONLY valid JSON this time."
                )

    raise ValueError(f"Failed to parse resume after {_MAX_RETRIES} attempts: {last_error}")


# ─── Alignment Analysis Prompt ────────────────────────────────────
_ALIGN_PROMPT_TEMPLATE = """
You are a senior resume strategist. Analyze the alignment between this resume and the job description.

Output a JSON object with exactly these keys:
- "atsScore": integer 0-100, how well this resume would score in an ATS for this JD
- "similarityToOriginal": integer 0-100 (will be 100 since resume is unchanged at this point)
- "rewriteIntensity": one of "enhancement", "reframe", "transform"
- "keyChanges": array of 4-6 short strings describing the most impactful changes to make
- "matchedKeywords": array of JD keywords already present in the resume
- "missingKeywords": array of important JD keywords missing from the resume
- "domainMatch": string — "same", "adjacent", or "different"
- "skillOverlapPercent": integer 0-100

RESUME JSON:
{resume_json}

JOB DESCRIPTION:
{job_description}

Output ONLY valid JSON. No markdown, no explanation.
"""

# ─── Step 2: Skills Optimization Prompt ───────────────────────────
_SKILLS_PROMPT_TEMPLATE = """
You are an elite resume strategist. Using the alignment analysis below, optimize ONLY the skills section of this resume for the target job description.

ALIGNMENT ANALYSIS:
{alignment_json}

RULES based on rewriteIntensity:
- "enhancement": Reorder categories so JD-priority skills come first. Add missing JD skills the candidate plausibly has. Keep all original skills.
- "reframe": Rename/regroup categories to match JD domain. Reorder by relevance. Add plausible JD skills. You may drop clearly irrelevant niche skills.
- "transform": DROP irrelevant technical categories entirely. Create new domain-relevant categories (e.g., for HR: "Talent Acquisition", "Communication & Collaboration"). Only keep transferable/overlapping skills.

Output a JSON object with exactly one key:
- "skills": array of objects, each with "name" (category name) and "items" (array of skill strings)

ORIGINAL SKILLS JSON:
{skills_json}

JOB DESCRIPTION:
{job_description}

Output ONLY valid JSON. No markdown, no explanation.
"""

# ─── Step 3: Experience & Projects Rewrite Prompt ─────────────────
_EXPERIENCE_PROMPT_TEMPLATE = """
You are an elite resume writer. Using the alignment analysis below, rewrite the workExperience and projects sections.

ALIGNMENT ANALYSIS:
{alignment_json}

OPTIMIZED SKILLS (already rewritten — use these as context for keyword consistency):
{skills_json}

RULES based on rewriteIntensity:
- "enhancement": Rewrite 30-40%% of bullet text. Add metrics, stronger verbs, JD keywords. Keep core structure.
- "reframe": Rewrite 50-65%% of bullets. Restructure to emphasize transferable skills. Reorder bullets by JD relevance.
- "transform": Rewrite 80-95%% of bullets. Completely reframe through the lens of the target role. Drop technical framing entirely.

NEVER change: company names, institution names, dates, number of entries.
ALWAYS: quantify bullets, upgrade action verbs, weave in JD keywords naturally.
Each work experience: 3-5 bullets max. Each project: 2-3 bullets max.

Output a JSON object with exactly two keys:
- "workExperience": array of work experience objects (same schema as input)
- "projects": array of project objects (same schema as input)

ORIGINAL RESUME JSON:
{resume_json}

JOB DESCRIPTION:
{job_description}

Output ONLY valid JSON. No markdown, no explanation.
"""

# ─── Step 4: Final Polish & Analytics Prompt ──────────────────────
_POLISH_PROMPT_TEMPLATE = """
You are an elite resume quality auditor. Review the assembled tailored resume below and perform a final polish pass.

ALIGNMENT ANALYSIS:
{alignment_json}

ASSEMBLED RESUME (skills already optimized, experience already rewritten):
{assembled_json}

Your tasks:
1. Ensure keyword consistency across all sections (skills mentioned should appear in bullets where natural).
2. Fix any awkward phrasing or redundancy introduced during rewriting.
3. Ensure personalInfo is UNCHANGED from the original.
4. Verify bullet ordering — most JD-relevant achievements first within each role/project.
5. Ensure length stays within ±15%% of {char_budget} characters.
6. Produce final analytics.

Output a JSON object with exactly two keys:
- "resume": the final polished resume JSON (same schema: personalInfo, workExperience, skills, projects, education)
- "analytics": object with these keys:
  - "atsScore": integer 0-100
  - "similarityToOriginal": integer 0-100 (reflect rewrite intensity: enhancement ~60-70, reframe ~40-55, transform ~25-40)
  - "keyChanges": array of 4-6 short strings describing the most significant changes made
  - "matchedKeywords": array of JD keywords now present in the resume
  - "missingKeywords": array of important JD keywords that could NOT be naturally incorporated

ORIGINAL RESUME (for similarity comparison):
{original_json}

JOB DESCRIPTION:
{job_description}

Output ONLY valid JSON. No markdown, no explanation.
"""


async def analyze_alignment(base_data: dict, job_description: str):
    """
    Step 1 of 4: Analyze the gap between the resume and the JD.
    Returns an analytics/alignment dict including rewriteIntensity, keywords, ATS score.
    This is a fast, targeted call — no rewriting happens here.
    """
    prompt = _ALIGN_PROMPT_TEMPLATE.format(
        resume_json=json.dumps(base_data, indent=2),
        job_description=job_description,
    )

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info("ai_align_attempt", attempt=attempt)

            # Use asynchronous streaming
            response_stream = await asyncio.wait_for(
                _client.aio.models.generate_content_stream(
                    model=settings.GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3,
                    ),
                ),
                timeout=settings.GEMINI_TIMEOUT_SECONDS,
            )

            full_text = ""
            async for chunk in response_stream:
                if chunk.text:
                    full_text += chunk.text
                    yield ("chunk", {"chars": len(chunk.text)})

            raw_json = _extract_json(full_text)
            alignment = json.loads(raw_json)
            alignment = _sanitize_resume_data(alignment)
            alignment = _post_process_strings(alignment)

            # Ensure minimal keys exist
            alignment.setdefault("rewriteIntensity", "enhancement")
            alignment.setdefault("atsScore", 50)
            alignment.setdefault("matchedKeywords", [])
            alignment.setdefault("missingKeywords", [])

            yield ("result", alignment)
            return

        except Exception as e:
            last_error = e
            logger.warning("ai_align_attempt_failed", attempt=attempt, error=str(e))

    logger.error("ai_align_exhausted_retries", error=str(last_error))
    raise ValueError(f"Failed to analyze alignment after {_MAX_RETRIES} attempts: {last_error}")


async def optimize_skills(base_data: dict, job_description: str, alignment: dict):
    """
    Step 2 of 4: Optimize ONLY the skills section based on gaps and rewrite intensity.
    """
    prompt = _SKILLS_PROMPT_TEMPLATE.format(
        skills_json=json.dumps(base_data.get("skills", []), indent=2),
        alignment_json=json.dumps(alignment, indent=2),
        job_description=job_description,
    )

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info("ai_skills_attempt", attempt=attempt)

            response_stream = await asyncio.wait_for(
                _client.aio.models.generate_content_stream(
                    model=settings.GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.4,
                    ),
                ),
                timeout=settings.GEMINI_TIMEOUT_SECONDS,
            )

            full_text = ""
            async for chunk in response_stream:
                if chunk.text:
                    full_text += chunk.text
                    yield ("chunk", {"chars": len(chunk.text)})

            raw_json = _extract_json(full_text)
            parsed = json.loads(raw_json)
            parsed = _sanitize_resume_data(parsed)
            parsed = _post_process_strings(parsed)

            if "skills" not in parsed:
                raise ValueError("Missing 'skills' key in response.")

            yield ("result", parsed["skills"])
            return

        except Exception as e:
            last_error = e
            logger.warning("ai_skills_attempt_failed", attempt=attempt, error=str(e))

    logger.error("ai_skills_exhausted_retries", error=str(last_error))
    # Fallback to original skills
    yield ("result", base_data.get("skills", []))
    return


async def rewrite_experience(base_data: dict, job_description: str, alignment: dict, optimized_skills: list):
    """
    Step 3 of 4: Rewrite workExperience and projects based on the JS/Alignment logic.
    Provides the AI with the newly optimized skills to ensure keyword consistency.
    """
    # Reduce payload size to keep context tight
    limited_base_data = {
        "workExperience": base_data.get("workExperience", []),
        "projects": base_data.get("projects", [])
    }

    prompt = _EXPERIENCE_PROMPT_TEMPLATE.format(
        resume_json=json.dumps(limited_base_data, indent=2),
        alignment_json=json.dumps(alignment, indent=2),
        skills_json=json.dumps(optimized_skills, indent=2),
        job_description=job_description,
    )

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info("ai_experience_attempt", attempt=attempt)

            response_stream = await asyncio.wait_for(
                _client.aio.models.generate_content_stream(
                    model=settings.GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.5,
                    ),
                ),
                timeout=settings.GEMINI_TIMEOUT_SECONDS,
            )

            full_text = ""
            async for chunk in response_stream:
                if chunk.text:
                    full_text += chunk.text
                    yield ("chunk", {"chars": len(chunk.text)})

            raw_json = _extract_json(full_text)
            parsed = json.loads(raw_json)
            parsed = _sanitize_resume_data(parsed)
            parsed = _post_process_strings(parsed)

            if "workExperience" not in parsed:
                raise ValueError("Missing 'workExperience' key in response.")

            yield ("result", parsed)
            return

        except Exception as e:
            last_error = e
            logger.warning("ai_experience_attempt_failed", attempt=attempt, error=str(e))

    logger.error("ai_experience_exhausted_retries", error=str(last_error))
    # Fallback
    yield ("result", limited_base_data)
    return


async def final_polish(base_data: dict, assembled_data: dict, job_description: str, alignment: dict, raw_text_length: int):
    """
    Step 4 of 4: Final quality check, length optimization, and analytics generation.
    Returns (TailorResponse (the final resume wrapper), analytics object).
    """
    # ... logic for char_budget remains the same ...
    char_budget = max(4000, raw_text_length)
    if alignment.get("rewriteIntensity") == "transform":
        char_budget = int(char_budget * 1.1)

    prompt = _POLISH_PROMPT_TEMPLATE.format(
        original_json=json.dumps(base_data, indent=2),
        assembled_json=json.dumps(assembled_data, indent=2),
        alignment_json=json.dumps(alignment, indent=2),
        job_description=job_description,
        char_budget=char_budget,
    )

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info("ai_polish_attempt", attempt=attempt)

            response_stream = await asyncio.wait_for(
                _client.aio.models.generate_content_stream(
                    model=settings.GEMINI_MODEL,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3,
                    ),
                ),
                timeout=settings.GEMINI_TIMEOUT_SECONDS,
            )

            full_text = ""
            async for chunk in response_stream:
                if chunk.text:
                    full_text += chunk.text
                    yield ("chunk", {"chars": len(chunk.text)})

            raw_json = _extract_json(full_text)
            parsed_dict = json.loads(raw_json)

            resume_part = parsed_dict.get("resume", {})
            analytics = parsed_dict.get("analytics", {})

            # If analytics came back sparse, merge with alignment data
            if not analytics.get("keyChanges"):
                analytics["keyChanges"] = alignment.get("keyChanges", [])
            if not analytics.get("matchedKeywords"):
                analytics["matchedKeywords"] = alignment.get("matchedKeywords", [])
            if not analytics.get("missingKeywords"):
                analytics["missingKeywords"] = alignment.get("missingKeywords", [])

            analytics = _sanitize_resume_data(analytics)
            resume_part = _sanitize_resume_data(resume_part)
            analytics = _post_process_strings(analytics)
            resume_part = _post_process_strings(resume_part)

            resume_data = ResumeData.model_validate(resume_part)

            logger.info("ai_polish_success", attempt=attempt, ats_score=analytics.get("atsScore"))
            yield ("result", (resume_data, analytics))
            return

        except Exception as e:
            last_error = e
            logger.warning("ai_polish_failed", attempt=attempt, error=str(e))

    logger.error("ai_polish_exhausted_retries", error=str(last_error))
    
    fallback_resume = TailorResponse(**assembled_data)
    fallback_analytics = {
        "atsScore": alignment.get("atsScore", 60),
        "similarityToOriginal": 80,
        "keyChanges": ["Optimized skills and experience for role."],
        "matchedKeywords": alignment.get("matchedKeywords", []),
        "missingKeywords": alignment.get("missingKeywords", []),
    }
    yield ("result", (fallback_resume, fallback_analytics))
    return


async def tailor_resume(base_data: dict, job_description: str, raw_text_length: int = 0) -> tuple:
    """
    Legacy wrapper — runs the full 4-step pipeline in one shot.
    Prefer calling the steps individually when you need to emit SSE progress between them.
    """
    alignment = await analyze_alignment(base_data, job_description)
    skills = await optimize_skills(base_data, job_description, alignment)
    experience = await rewrite_experience(base_data, job_description, alignment, skills)

    assembled = {
        "personalInfo": base_data.get("personalInfo", {}),
        "workExperience": experience.get("workExperience", []),
        "skills": skills,
        "projects": experience.get("projects", []),
        "education": base_data.get("education", []),
    }

    return await final_polish(base_data, assembled, job_description, alignment, raw_text_length)


async def generate_summary(resume_data: dict, job_description: str) -> str:
    """Generate a short summary label for the dashboard card."""
    prompt = (
        "Write a SHORT dashboard label (max 8-10 words) for this tailored resume. "
        "Format: '[Role] at [Company]' or '[Role] – [Industry]'. "
        "Examples: 'DevOps Engineer at Netflix', 'HR Intern – Recruitment Focus', "
        "'Full Stack Developer – Fintech'. No quotes, no markdown, no punctuation at end.\n\n"
        f"Job Description: {job_description[:300]}\n"
        f"Candidate: {resume_data.get('personalInfo', {}).get('fullName', 'Unknown')}"
    )

    try:
        response = await run_blocking(
            _client.models.generate_content,
            model=settings.GEMINI_MODEL,
            contents=prompt,
            timeout=settings.GEMINI_TIMEOUT_SECONDS,
        )
        return _post_process_strings(sanitize_input(response.text.strip()))
    except Exception as e:
        logger.warning("ai_summary_failed", error=str(e))
        return f"Tailored for: {job_description[:100]}..."
