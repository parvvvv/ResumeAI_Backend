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


class Skills(BaseModel):
    languages: List[str] = Field(default_factory=list)
    frameworks: List[str] = Field(default_factory=list)
    databases: List[str] = Field(default_factory=list)
    tools: List[str] = Field(default_factory=list)
    cloud: List[str] = Field(default_factory=list)
    other: List[str] = Field(default_factory=list)


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


class ResumeData(BaseModel):
    """Top-level resume schema. This is the single source of truth."""
    personalInfo: PersonalInfo = Field(default_factory=PersonalInfo)
    workExperience: List[WorkExperience] = Field(default_factory=list)
    skills: Skills = Field(default_factory=Skills)
    projects: List[Project] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)


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
