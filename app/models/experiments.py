from pydantic import BaseModel


class ActivityTypeIn(BaseModel):
    name: str
    color: str = "#3b82f6"


class ExperimentIn(BaseModel):
    title: str
    description: str = ""
    start_date: str
    num_weeks: int
    activity_types: list[ActivityTypeIn] = []
    target_min: int = 0
    target_max: int = 0


class EntryIn(BaseModel):
    date: str
    activity_type_id: int
    duration_minutes: int
    notes: str = ""
