"""
Study planner Pydantic models.

The planner is intentionally project-centered: weeks and sessions exist to
move a portfolio project forward while closing career proof gaps.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class StudyResource(BaseModel):
    title: str = ""
    type: str = "article"  # article | video | course | docs | tool
    searchQuery: str = ""


class StudySession(BaseModel):
    sessionId: str = ""
    title: str = ""
    category: str = ""
    durationMinutes: int = 30
    description: str = ""
    rationale: str = ""
    resources: List[StudyResource] = Field(default_factory=list)
    practiceTask: str = ""
    deliverable: str = ""
    projectContribution: str = ""


class StudyDay(BaseModel):
    dayNumber: int = 1
    dayLabel: str = ""
    totalMinutes: int = 0
    sessions: List[StudySession] = Field(default_factory=list)


class PortfolioProject(BaseModel):
    """The showcase project each week builds toward."""

    name: str = ""
    description: str = ""
    weekMilestone: str = ""
    techStack: List[str] = Field(default_factory=list)
    completionCriteria: List[str] = Field(default_factory=list)


class StudyWeek(BaseModel):
    weekNumber: int = 1
    theme: str = ""
    objectives: List[str] = Field(default_factory=list)
    days: List[StudyDay] = Field(default_factory=list)
    weeklyMilestone: str = ""
    weeklyReview: str = ""
    portfolioProject: PortfolioProject = Field(default_factory=PortfolioProject)


class ReadinessDimension(BaseModel):
    score: int = 0
    max: int = 100
    reason: str = ""


class ReadinessBreakdown(BaseModel):
    technicalSkills: ReadinessDimension = Field(default_factory=ReadinessDimension)
    projectEvidence: ReadinessDimension = Field(default_factory=ReadinessDimension)
    experienceAlignment: ReadinessDimension = Field(default_factory=ReadinessDimension)
    domainKnowledge: ReadinessDimension = Field(default_factory=ReadinessDimension)
    interviewReadiness: ReadinessDimension = Field(default_factory=ReadinessDimension)


class CareerGapAnalysis(BaseModel):
    """Career-level gap analysis, including proof gaps."""

    currentSkills: List[str] = Field(default_factory=list)
    missingSkills: List[str] = Field(default_factory=list)
    transferableSkills: List[str] = Field(default_factory=list)
    priorityOrder: List[str] = Field(default_factory=list)
    missingProjectTypes: List[str] = Field(default_factory=list)
    missingExperience: List[str] = Field(default_factory=list)
    proofGaps: List[str] = Field(default_factory=list)
    missingCertifications: List[str] = Field(default_factory=list)
    readinessScore: int = 0
    readinessBreakdown: ReadinessBreakdown = Field(default_factory=ReadinessBreakdown)


class StudyPlanRequest(BaseModel):
    generatedResumeId: Optional[str] = None
    jobDescription: Optional[str] = None
    totalWeeks: int = Field(default=3, ge=1, le=5)
    hoursPerDay: float = Field(default=2.0, ge=0.5, le=8.0)
    daysPerWeek: int = Field(default=5, ge=1, le=7)
    focusAreas: List[str] = Field(
        default_factory=lambda: [
            "technical-skills",
            "system-design",
            "dsa-coding",
            "behavioral",
            "domain-knowledge",
            "projects",
        ]
    )


class StudyProgressUpdate(BaseModel):
    sessionId: str
    completed: bool = True


class RegenerateWeekRequest(BaseModel):
    confirmReset: bool = False
