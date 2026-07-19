from pydantic import BaseModel


class MetricIn(BaseModel):
    """One tracked metric of an experiment (stored in experiment_activity_types)."""

    name: str
    color: str = "#3b82f6"
    kind: str = "duration"  # duration | count | boolean | scale
    target_value: int = 0  # 0 = no target; only meaningful for count/boolean
    target_period: str = "week"  # day | week | total
    source_match: str = ""  # Oura activity names (duration metrics only)


class ExperimentIn(BaseModel):
    title: str
    description: str = ""
    start_date: str
    num_weeks: int
    metrics: list[MetricIn] = []
    target_min: int = 0  # weekly minutes window (duration metrics)
    target_max: int = 0


class EntryIn(BaseModel):
    """One logged entry; `value` meaning depends on the metric's kind:
    duration=minutes, count=events, boolean=1/0 (one per day), scale=0-10."""

    date: str
    metric_id: int
    value: int = 1
    notes: str = ""
