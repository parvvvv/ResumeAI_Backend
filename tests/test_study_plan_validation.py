import pytest
from app.models.study_plan import StudyWeek, StudyPlanRequest, StudyDay, StudySession
from app.services.study_plan_service import _validate_week

def test_validate_week_valid():
    config = StudyPlanRequest(daysPerWeek=5, hoursPerDay=2.0)
    
    sessions = [
        StudySession(title="S1", durationMinutes=60, practiceTask="Task 1", deliverable="Del 1", rationale="Rationale 1"),
        StudySession(title="S2", durationMinutes=60, practiceTask="Task 2", deliverable="Del 2", rationale="Rationale 2")
    ]
    days = [StudyDay(dayNumber=i, sessions=sessions) for i in range(1, 6)]
    
    week = StudyWeek(days=days)
    errors = _validate_week(week, config)
    assert not errors

def test_validate_week_wrong_day_count():
    config = StudyPlanRequest(daysPerWeek=5)
    
    sessions = [
        StudySession(title="S1", durationMinutes=120, practiceTask="Task 1", deliverable="Del 1", rationale="Rationale 1"),
    ]
    # Only 4 days instead of 5
    days = [StudyDay(dayNumber=i, sessions=sessions) for i in range(1, 5)]
    
    week = StudyWeek(days=days)
    errors = _validate_week(week, config)
    assert len(errors) == 1
    assert "Expected 5 days, got 4" in errors[0]

def test_validate_week_time_math_off():
    config = StudyPlanRequest(daysPerWeek=1, hoursPerDay=2.0) # Target 120min
    
    # Session sum is 100min (diff > 10min)
    sessions = [
        StudySession(title="S1", durationMinutes=100, practiceTask="Task 1", deliverable="Del 1", rationale="Rationale 1"),
    ]
    days = [StudyDay(dayNumber=1, sessions=sessions)]
    
    week = StudyWeek(days=days)
    errors = _validate_week(week, config)
    assert len(errors) == 1
    assert "got 100min" in errors[0]

def test_validate_week_session_bounds():
    config = StudyPlanRequest(daysPerWeek=1, hoursPerDay=2.0) # Target 120min
    
    # Duration < 15 and > 120
    sessions = [
        StudySession(title="S1", durationMinutes=10, practiceTask="Task 1", deliverable="Del 1", rationale="Rationale 1"),
        StudySession(title="S2", durationMinutes=130, practiceTask="Task 2", deliverable="Del 2", rationale="Rationale 2")
    ]
    days = [StudyDay(dayNumber=1, sessions=sessions)]
    
    week = StudyWeek(days=days)
    errors = _validate_week(week, config)
    assert len(errors) >= 2 # Should catch both bounds errors
    assert any("has invalid duration: 10min" in e for e in errors)
    assert any("has invalid duration: 130min" in e for e in errors)

def test_validate_week_missing_fields():
    config = StudyPlanRequest(daysPerWeek=1, hoursPerDay=1.0)
    
    sessions = [
        # Missing practiceTask
        StudySession(title="S1", durationMinutes=30, deliverable="Del 1", rationale="Rationale 1"),
        # Missing deliverable
        StudySession(title="S2", durationMinutes=30, practiceTask="Task 2", rationale="Rationale 2"),
        # Missing rationale
        StudySession(title="S3", durationMinutes=30, practiceTask="Task 3", deliverable="Del 3")
    ]
    days = [StudyDay(dayNumber=1, sessions=sessions)]
    
    week = StudyWeek(days=days)
    errors = _validate_week(week, config)
    
    assert any("missing practiceTask" in e for e in errors)
    assert any("missing deliverable" in e for e in errors)
    assert any("missing rationale" in e for e in errors)
