"""
AI-powered template generation service.
Handles: HTML upload → sanitize → detect hardcoded values → templatize → draft.
Also: Image upload → Gemini Vision → HTML → same pipeline.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from google import genai
from google.genai import types

from app.config import settings
from app.database import get_database
from app.models.template import TemplateSchema, TemplateFieldDefinition
from app.runtime import run_blocking
from app.security import sanitize_template_html, validate_jinja_safety
import structlog

logger = structlog.get_logger()

_client = genai.Client(api_key=settings.GEMINI_API_KEY)


# ---------------------------------------------------------------------------
# Default preview seed data for AI-generated templates
# ---------------------------------------------------------------------------

_DEFAULT_PREVIEW_SEED = {
    "resume": {
        "personalInfo": {
            "fullName": "Avery Patel",
            "email": "avery.patel@example.com",
            "phone": "+1 (555) 010-2244",
            "linkedin": "linkedin.com/in/averypatel",
            "github": "github.com/averypatel",
        },
        "workExperience": [
            {
                "company": "Northstar Labs",
                "role": "Senior Software Engineer",
                "location": "San Francisco, CA",
                "startDate": "Jan 2022",
                "endDate": "Present",
                "description": "Full-stack engineering for the core platform.",
                "points": [
                    "Led a cross-functional team of 5 engineers to ship a real-time collaboration feature used by 10K+ users.",
                    "Reduced API latency by 40% through query optimization and Redis caching.",
                    "Built a CI/CD pipeline that cut deployment time from 45 min to 8 min.",
                ],
                "techStack": ["React", "Node.js", "PostgreSQL", "Redis"],
            },
            {
                "company": "BlueShift Inc.",
                "role": "Software Engineer",
                "location": "Austin, TX",
                "startDate": "Jun 2019",
                "endDate": "Dec 2021",
                "description": "Backend services and data pipeline development.",
                "points": [
                    "Designed and implemented RESTful APIs serving 2M+ requests per day.",
                    "Migrated legacy monolith to microservices architecture, improving scalability by 3x.",
                ],
                "techStack": ["Python", "FastAPI", "MongoDB", "Docker"],
            },
        ],
        "skills": [
            {"name": "Languages", "items": ["Python", "JavaScript", "TypeScript", "Go"]},
            {"name": "Frameworks", "items": ["React", "FastAPI", "Node.js", "Next.js"]},
            {"name": "Tools", "items": ["Docker", "Kubernetes", "AWS", "Git"]},
        ],
        "projects": [
            {
                "name": "ResumeAI Platform",
                "description": "An AI-powered resume builder with template marketplace and ATS scoring.",
                "points": [
                    "Built a Jinja2-based template engine with sandboxed rendering for security.",
                    "Integrated Gemini AI for automatic template generation from uploaded screenshots.",
                ],
                "techStack": ["React", "FastAPI", "MongoDB", "Gemini AI"],
            },
            {
                "name": "DevSync",
                "description": "Real-time code collaboration tool for remote teams.",
                "points": [
                    "Implemented WebSocket-based live editing with conflict resolution.",
                    "Achieved 99.9% uptime with automated failover and health monitoring.",
                ],
                "techStack": ["TypeScript", "WebSockets", "Redis", "AWS"],
            },
        ],
        "education": [
            {
                "institution": "Stanford University",
                "degree": "Master of Science",
                "field": "Computer Science",
                "startYear": "2017",
                "endYear": "2019",
                "score": "3.9 GPA",
            },
            {
                "institution": "UC Berkeley",
                "degree": "Bachelor of Science",
                "field": "Electrical Engineering & Computer Science",
                "startYear": "2013",
                "endYear": "2017",
                "score": "3.7 GPA",
            },
        ],
    },
    "extras": {
        "summary": "Results-driven software engineer with 5+ years of experience building scalable web applications and leading cross-functional teams. Passionate about developer tools, AI, and clean architecture.",
        "tagline": "Building the future, one commit at a time.",
    },
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_DETECT_AND_TEMPLATIZE_PROMPT = """\
You are an expert resume template engineer. You have been given an HTML resume document that \
likely contains HARDCODED personal data (real names, emails, phone numbers, company names, \
dates, skills, education details, etc.).

Your job is to:
1. IDENTIFY all hardcoded resume data in the HTML.
2. REPLACE each piece of hardcoded data with the correct Jinja2 placeholder.
3. PRESERVE all HTML structure, CSS styling, and layout exactly as-is.
4. GENERATE a schema describing what fields the template expects.

PLACEHOLDER MAPPING RULES:
- Person's name → {{ resume.personalInfo.fullName }}
- Email address → {{ resume.personalInfo.email }}
- Phone number → {{ resume.personalInfo.phone }}
- LinkedIn URL → {{ resume.personalInfo.linkedin }}
- GitHub URL → {{ resume.personalInfo.github }}
- Company names in work experience → {{ exp.company }}  (inside {% for exp in resume.workExperience %})
- Job titles/roles → {{ exp.role }}
- Work locations → {{ exp.location }}
- Work start dates → {{ exp.startDate }}
- Work end dates → {{ exp.endDate }}
- Work bullet points → {% for point in exp.points %}<li>{{ point }}</li>{% endfor %}
- Tech stack items → {{ exp.techStack | join(", ") }}
- Project names → {{ proj.name }}  (inside {% for proj in resume.projects %})
- Project descriptions → {{ proj.description }}
- Project bullets → {% for point in proj.points %}<li>{{ point }}</li>{% endfor %}
- Project tech → {{ proj.techStack | join(", ") }}
- School/university names → {{ edu.institution }}  (inside {% for edu in resume.education %})
- Degree → {{ edu.degree }}
- Field of study → {{ edu.field }}
- Education years → {{ edu.startYear }}, {{ edu.endYear }}
- GPA/scores → {{ edu.score }}
- Skill categories → {% for cat in resume.skills %} {{ cat.name }}: {{ cat['items'] | join(", ") }} {% endfor %}

CRITICAL RULES:
- When you encounter a SECTION with MULTIPLE items (multiple jobs, multiple projects, etc.), \
wrap them in a Jinja {% for %} loop and keep ONE template iteration.
- Use {% if %} guards for optional sections (e.g., {% if resume.workExperience %}).
- DO NOT change any CSS, class names, IDs, or structural HTML.
- DO NOT add new HTML elements.
- DO NOT remove any styling.
- If something looks like a placeholder already (e.g., "John Doe" or "example@email.com"), still replace it.
- For items you're unsure about (e.g., a tagline, portfolio URL), map to extras: {{ extras.fieldname }}

OUTPUT FORMAT (JSON):
{
  "htmlContent": "... the full HTML with all hardcoded values replaced by Jinja placeholders ...",
  "fieldMappings": {
    "Original Value 1": "{{ resume.personalInfo.fullName }}",
    "Original Value 2": "{{ resume.personalInfo.email }}",
    ...
  },
  "detectedSections": ["personalInfo", "workExperience", "skills", "projects", "education"],
  "extraFields": [
    {"key": "tagline", "label": "Tagline", "type": "text"},
    {"key": "profilePhoto", "label": "Profile Photo", "type": "image"}
  ],
  "warnings": [
    "Could not determine if 'Acme Corp' is a real company or placeholder — replaced anyway.",
    ...
  ],
  "confidence": 0.85
}

HTML DOCUMENT TO ANALYZE:
{html_content}

Output ONLY valid JSON. No markdown, no explanation.
"""


_IMAGE_TO_HTML_PROMPT = """\
You are an expert web developer specializing in resume templates. You have been given \
a screenshot/image of a resume. Your job is to recreate it as clean, professional HTML+CSS.

RULES:
1. Use inline <style> block for all CSS (no external stylesheets).
2. Use semantic HTML elements (div, section, h1-h6, p, ul/li, table).
3. Match the layout, typography, colors, and spacing as closely as possible.
4. Use placeholder text that looks like real resume data (real-sounding names, companies, etc.).
5. Make it print-friendly (A4/letter size, proper margins).
6. DO NOT use JavaScript.
7. DO NOT use external images, fonts, or resources.
8. Use system fonts or specify @font-face with common font names.
9. The HTML should be complete and self-contained (<!DOCTYPE html> to </html>).

Output ONLY the complete HTML document. No markdown code blocks, no explanation.
"""


def _extract_json(text: str) -> str:
    """Extract JSON from AI response text."""
    code_block_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if code_block_match:
        return code_block_match.group(1).strip()
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        return json_match.group(0).strip()
    return text.strip()


def _extract_html(text: str) -> str:
    """Extract HTML from AI response text."""
    # Try to find HTML in code blocks first
    code_block_match = re.search(r"```(?:html)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if code_block_match:
        return code_block_match.group(1).strip()
    # Look for complete HTML document
    html_match = re.search(r"<!DOCTYPE.*?</html>", text, re.DOTALL | re.IGNORECASE)
    if html_match:
        return html_match.group(0).strip()
    return text.strip()


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

async def detect_and_templatize_html(
    raw_html: str,
) -> dict[str, Any]:
    """
    Takes raw HTML (potentially with hardcoded resume data) and uses AI to:
    1. Detect all hardcoded personal/professional data
    2. Replace with Jinja2 template placeholders
    3. Generate a schema of expected fields
    4. Return warnings about uncertain replacements

    Returns dict with: htmlContent, schema, fieldMappings, warnings
    """
    prompt = _DETECT_AND_TEMPLATIZE_PROMPT.replace("{html_content}", raw_html)

    last_error = None
    for attempt in range(1, 3):
        try:
            logger.info("template_gen_detect_attempt", attempt=attempt)

            response = await run_blocking(
                _client.models.generate_content,
                model=settings.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
                timeout=settings.GEMINI_TIMEOUT_SECONDS * 2,  # Double timeout for large HTML
            )

            raw_json = _extract_json(response.text)
            result = json.loads(raw_json)

            html_content = result.get("htmlContent", "")
            field_mappings = result.get("fieldMappings", {})
            detected_sections = result.get("detectedSections", [])
            extra_fields = result.get("extraFields", [])
            warnings = result.get("warnings", [])
            confidence = result.get("confidence", 0.5)

            # Build template schema from detected sections and extra fields
            schema_fields = []
            for ef in extra_fields:
                schema_fields.append(TemplateFieldDefinition(
                    key=ef.get("key", ""),
                    label=ef.get("label", ""),
                    type=ef.get("type", "text"),
                    required=ef.get("required", False),
                    description=ef.get("description", ""),
                ))

            schema = TemplateSchema(
                sections=detected_sections,
                fields=schema_fields,
            )

            if confidence < 0.5:
                warnings.append(
                    f"Low confidence ({confidence:.0%}) in hardcoded value detection. "
                    "Please review all placeholders carefully."
                )

            logger.info(
                "template_gen_detect_success",
                attempt=attempt,
                mappings_count=len(field_mappings),
                sections=detected_sections,
                confidence=confidence,
            )

            return {
                "htmlContent": html_content,
                "schema": schema,
                "fieldMappings": field_mappings,
                "warnings": warnings,
                "confidence": confidence,
            }

        except Exception as e:
            last_error = e
            logger.warning("template_gen_detect_failed", attempt=attempt, error=str(e))

    raise ValueError(f"Failed to detect/templatize HTML after 2 attempts: {last_error}")


async def image_to_html(image_bytes: bytes, mime_type: str) -> str:
    """
    Convert a resume screenshot/image into HTML using Gemini Vision.
    Returns raw HTML string (with hardcoded placeholder data).
    """
    last_error = None
    for attempt in range(1, 3):
        try:
            logger.info("template_gen_image_attempt", attempt=attempt)

            response = await run_blocking(
                _client.models.generate_content,
                model=settings.GEMINI_MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    _IMAGE_TO_HTML_PROMPT,
                ],
                config=types.GenerateContentConfig(
                    temperature=0.3,
                ),
                timeout=settings.GEMINI_TIMEOUT_SECONDS * 2,
            )

            html = _extract_html(response.text)
            if not html or len(html) < 100:
                raise ValueError("Generated HTML is too short or empty.")

            logger.info("template_gen_image_success", attempt=attempt, html_length=len(html))
            return html

        except Exception as e:
            last_error = e
            logger.warning("template_gen_image_failed", attempt=attempt, error=str(e))

    raise ValueError(f"Failed to convert image to HTML after 2 attempts: {last_error}")


# ---------------------------------------------------------------------------
# Job queue management
# ---------------------------------------------------------------------------

async def create_template_job(
    owner_user_id: str,
    source_type: str,  # "html_upload" or "image_upload"
    raw_content: str | bytes,
    mime_type: str = "text/html",
) -> str:
    """
    Create a template generation job in the queue.
    Returns the job ID.
    """
    db = get_database()
    job_id = uuid4().hex
    now = datetime.now(timezone.utc)

    doc: dict[str, Any] = {
        "_id": job_id,
        "ownerUserId": owner_user_id,
        "sourceType": source_type,
        "status": "queued",
        "rawContent": raw_content if isinstance(raw_content, str) else None,
        "rawContentBytes": raw_content if isinstance(raw_content, bytes) else None,
        "mimeType": mime_type,
        "templateId": None,
        "htmlContent": None,
        "schemaResult": None,
        "fieldMappings": None,
        "warnings": [],
        "errorMessage": None,
        "createdAt": now,
        "updatedAt": now,
    }

    await db.template_jobs.insert_one(doc)
    logger.info("template_job_created", job_id=job_id, source_type=source_type)

    # Fire and forget the processing task
    asyncio.create_task(_process_template_job(job_id))

    return job_id


async def get_template_job(job_id: str, user_id: str) -> Optional[dict]:
    """Fetch a template job by ID, scoped to the owner."""
    db = get_database()
    doc = await db.template_jobs.find_one({"_id": job_id, "ownerUserId": user_id})
    if not doc:
        return None
    return _serialize_job(doc)


async def _update_job_status(
    job_id: str,
    status_value: str,
    extra_fields: Optional[dict] = None,
) -> None:
    db = get_database()
    update = {"status": status_value, "updatedAt": datetime.now(timezone.utc)}
    if extra_fields:
        update.update(extra_fields)
    await db.template_jobs.update_one({"_id": job_id}, {"$set": update})


def _serialize_job(doc: dict) -> dict:
    """Serialize a job document for API response."""
    result = dict(doc)
    result["id"] = str(result.pop("_id"))
    # Remove binary content from response
    result.pop("rawContent", None)
    result.pop("rawContentBytes", None)
    result.pop("mimeType", None)
    return result


async def _process_template_job(job_id: str) -> None:
    """
    Background task: process a template generation job through the full pipeline.
    Steps: sanitize HTML → detect hardcoded values → templatize → save as draft template.
    """
    db = get_database()

    try:
        job = await db.template_jobs.find_one({"_id": job_id})
        if not job:
            logger.error("template_job_not_found", job_id=job_id)
            return

        await _update_job_status(job_id, "processing")

        source_type = job["sourceType"]
        raw_html = ""

        # Step 1: Get raw HTML (from upload or image conversion)
        if source_type == "image_upload":
            image_bytes = job.get("rawContentBytes")
            mime_type = job.get("mimeType", "image/png")
            if not image_bytes:
                raise ValueError("No image content found in job.")
            raw_html = await image_to_html(image_bytes, mime_type)
        else:
            raw_html = job.get("rawContent", "")
            if not raw_html:
                raise ValueError("No HTML content found in job.")

        # Step 2: Sanitize HTML (security layer)
        sanitized_html, sanitize_warnings = sanitize_template_html(raw_html)

        # Step 3: Detect hardcoded values and templatize
        result = await detect_and_templatize_html(sanitized_html)

        templatized_html = result["htmlContent"]
        schema = result["schema"]
        field_mappings = result["fieldMappings"]
        ai_warnings = result["warnings"]

        # Step 4: Validate Jinja safety
        is_safe, jinja_violations = validate_jinja_safety(templatized_html)
        all_warnings = sanitize_warnings + ai_warnings
        if not is_safe:
            all_warnings.extend([f"Jinja safety: {v}" for v in jinja_violations])

        # Step 5: Create draft template with rich preview seed data
        now = datetime.now(timezone.utc)
        template_id = uuid4().hex
        template_doc = {
            "_id": template_id,
            "ownerUserId": job["ownerUserId"],
            "ownerEmail": None,
            "title": f"AI Generated Template",
            "description": "Auto-generated from uploaded content. Review and edit before publishing.",
            "htmlContent": templatized_html,
            "templateSchema": schema.model_dump(),
            "previewSeedData": _DEFAULT_PREVIEW_SEED,
            "tags": ["ai-generated"],
            "visibility": "private",
            "sourceType": source_type.replace("_upload", "_generated") if "upload" in source_type else "ai_generated",
            "status": "draft",  # ALWAYS draft
            "isEditable": True,
            "templateKey": f"ai_{template_id}",
            "usageCount": 0,
            "favoriteCount": 0,
            "viewCount": 0,
            "downloadCount": 0,
            "basedOnTemplateId": None,
            "version": 1,
            "sharedWithUserIds": [],
            "shareTokens": [],
            "createdAt": now,
            "updatedAt": now,
        }

        await db.templates.insert_one(template_doc)

        # Step 6: Update job to needs_review with results
        await _update_job_status(job_id, "needs_review", {
            "templateId": template_id,
            "htmlContent": templatized_html,
            "schemaResult": schema.model_dump(),
            "fieldMappings": field_mappings,
            "warnings": all_warnings,
        })

        logger.info(
            "template_job_completed",
            job_id=job_id,
            template_id=template_id,
            warnings_count=len(all_warnings),
        )

    except Exception as e:
        logger.error("template_job_failed", job_id=job_id, error=str(e))
        await _update_job_status(job_id, "failed", {
            "errorMessage": str(e),
        })
