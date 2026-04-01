"""
Strict resume schema matching TRD §4.
All fields default to empty so partial data is accepted without error.
"""

from __future__ import annotations
from typing import List
from pydantic import BaseModel, Field


class PersonalInfo(BaseModel):
    fullName: str = ""
    phone: str = ""
    email: str = ""
    linkedin: str = ""
    github: str = ""


class WorkExperience(BaseModel):
    company: str = ""
    role: str = ""
    location: str = ""
    startDate: str = ""
    endDate: str = ""
    description: str = ""
    points: List[str] = Field(default_factory=list)
    techStack: List[str] = Field(default_factory=list)


class SkillCategory(BaseModel):
    name: str = ""
    items: List[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str = ""
    description: str = ""
    points: List[str] = Field(default_factory=list)
    techStack: List[str] = Field(default_factory=list)


class Education(BaseModel):
    institution: str = ""
    degree: str = ""
    field: str = ""
    startYear: str = ""
    endYear: str = ""
    score: str = ""


from typing import Any, Union
from pydantic import model_validator

class ResumeData(BaseModel):
    """Top-level resume schema. This is the single source of truth."""
    personalInfo: PersonalInfo = Field(default_factory=PersonalInfo)
    workExperience: List[WorkExperience] = Field(default_factory=list)
    skills: List[SkillCategory] = Field(default_factory=list)
    projects: List[Project] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def convert_legacy_skills(cls, data: Any) -> Any:
        if isinstance(data, dict):
            skills = data.get("skills")
            if isinstance(skills, dict):
                # Convert old dictionary of generic structures to list of SkillCategory
                new_skills = []
                for k, v in skills.items():
                    if isinstance(v, list) and v: # Only migrate non-empty categories
                        category_name = k.capitalize()
                        if k == "cloud": category_name = "Cloud Platforms"
                        elif k == "other": category_name = "Other"
                        new_skills.append({"name": category_name, "items": v})
                data["skills"] = new_skills
        return data


class AnalyticsData(BaseModel):
    """Analytics generated specifically for the job description match."""
    atsScore: int = Field(default=0, description="Score 0-100 indicating ATS match.")
    similarityToOriginal: int = Field(default=0, description="Similarity to base resume 0-100.")
    keyChanges: List[str] = Field(default_factory=list, description="Array of key changes made.")
    matchedKeywords: List[str] = Field(default_factory=list, description="Keywords from JD matched.")
    missingKeywords: List[str] = Field(default_factory=list, description="Keywords from JD missed.")


class TailorResponse(BaseModel):
    """The root object for AI tailoring response containing resume and analytics."""
    resume: ResumeData = Field(default_factory=ResumeData)
    analytics: AnalyticsData = Field(default_factory=AnalyticsData)
