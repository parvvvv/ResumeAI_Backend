"""
AI service: Gemini integration for resume parsing and tailoring.
Uses google-genai SDK with retry logic and strict schema validation.
"""

import html
import json
import re
from pathlib import Path
from google import genai
from google.genai import types
from app.config import settings
from app.models.resume import ResumeData, TailorResponse
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

            response = _client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
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

        except (json.JSONDecodeError, Exception) as e:
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

# ─── Content Rewrite Prompt ───────────────────────────────────────
_REWRITE_PROMPT_TEMPLATE = """
You are an elite resume writer. Using the alignment analysis below, rewrite the resume.

ALIGNMENT ANALYSIS:
{alignment_json}

Apply these rules based on rewriteIntensity:
- "enhancement": Rewrite 30-40%% of bullet text. Add metrics, stronger verbs, JD keywords.
- "reframe": Rewrite 50-65%% of bullets. Restructure to emphasize transferable skills.
- "transform": Rewrite 80-95%% of bullets. Completely reframe through the lens of the target role.

NEVER change: personalInfo, company names, institution names, dates, number of entries.
ALWAYS: quantify bullets, upgrade action verbs, weave in JD keywords naturally.
Length: Stay within ±15%% of {char_budget} characters.

Output a JSON object with exactly two keys:
- "resume": the tailored resume JSON (same schema as input)
- "analytics": same object as the alignment analysis but update atsScore and similarityToOriginal for final output

ORIGINAL RESUME JSON:
{resume_json}

JOB DESCRIPTION:
{job_description}

Output ONLY valid JSON. No markdown, no explanation.
"""


async def analyze_alignment(base_data: dict, job_description: str) -> dict:
    """
    Step 1 of 2: Analyze the gap between the resume and the JD.
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

            response = _client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.3,
                ),
            )

            raw_json = _extract_json(response.text)
            alignment = json.loads(raw_json)
            alignment = _sanitize_resume_data(alignment)
            alignment = _post_process_strings(alignment)

            logger.info("ai_align_success", attempt=attempt, ats_score=alignment.get("atsScore"))
            return alignment

        except (json.JSONDecodeError, Exception) as e:
            last_error = e
            logger.warning("ai_align_failed", attempt=attempt, error=str(e))
            if attempt < _MAX_RETRIES:
                prompt = (
                    f"{prompt}\n\nIMPORTANT: Your previous response was not valid JSON. "
                    f"Error: {str(e)}. Output ONLY valid JSON this time."
                )

    raise ValueError(f"Failed to analyze alignment after {_MAX_RETRIES} attempts: {last_error}")


async def tailor_content(base_data: dict, job_description: str, alignment: dict, raw_text_length: int = 0) -> tuple:
    """
    Step 2 of 2: Rewrite the resume using the alignment analysis from step 1.
    Returns (ResumeData, analytics_dict) — same interface as the old tailor_resume.
    """
    prompt = _REWRITE_PROMPT_TEMPLATE.format(
        alignment_json=json.dumps(alignment, indent=2),
        resume_json=json.dumps(base_data, indent=2),
        job_description=job_description,
        char_budget=raw_text_length if raw_text_length > 0 else "not specified",
    )

    last_error = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            logger.info("ai_tailor_attempt", attempt=attempt)

            response = _client.models.generate_content(
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TailorResponse,
                    temperature=0.85,
                    top_p=0.92,
                ),
            )

            raw_json = response.text
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

            logger.info("ai_tailor_success", attempt=attempt, ats_score=analytics.get("atsScore"))
            return resume_data, analytics

        except (json.JSONDecodeError, Exception) as e:
            last_error = e
            logger.warning("ai_tailor_failed", attempt=attempt, error=str(e))
            if attempt < _MAX_RETRIES:
                prompt = (
                    f"{prompt}\n\nIMPORTANT: Your previous response was not valid JSON. "
                    f"Error: {str(e)}. Output ONLY valid JSON this time."
                )

    raise ValueError(f"Failed to tailor resume content after {_MAX_RETRIES} attempts: {last_error}")


async def tailor_resume(base_data: dict, job_description: str, raw_text_length: int = 0) -> tuple:
    """
    Legacy wrapper — runs analyze_alignment then tailor_content in one shot.
    Kept for backward compatibility. Prefer calling the two steps separately
    when you need to emit SSE progress between them.
    """
    alignment = await analyze_alignment(base_data, job_description)
    return await tailor_content(base_data, job_description, alignment, raw_text_length)


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
        response = _client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=prompt,
        )
        return _post_process_strings(sanitize_input(response.text.strip()))
    except Exception as e:
        logger.warning("ai_summary_failed", error=str(e))
        return f"Tailored for: {job_description[:100]}..."
