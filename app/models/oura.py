from pydantic import BaseModel


class OuraMonthlyIn(BaseModel):
    month: str
    sleep_score: float | None = None
    readiness: float | None = None
    activity: float | None = None
    steps: int | None = None
    sleep_duration: float | None = None
    deep_sleep: float | None = None
    rem_sleep: float | None = None
    rhr: float | None = None
    lowest_hr: float | None = None
    hrv: float | None = None
    cardiovascular_age: int | None = None
    stress_normal: int | None = None
    stress_stressful: int | None = None
    stress_restored: int | None = None
    notes: str = ""
