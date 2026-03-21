from pydantic import BaseModel


class FenikConfigIn(BaseModel):
    start_date: str = "2026-02-05"
    target_days: int = 90
    big_why: str = ""


class FeniksJournalIn(BaseModel):
    date: str
    emotions: str = ""
    triggers: str = ""
    thoughts: str = ""
    desired_feelings: str = ""
    coping_strategies: str = ""


class FeniksPleasureIn(BaseModel):
    date: str
    pleasure_1: str = ""
    pleasure_2: str = ""


class PmoEventIn(BaseModel):
    date: str
    event_type: str = "relapse"
    notes: str = ""


class MilestoneToggle(BaseModel):
    day_number: int
    completed: bool
