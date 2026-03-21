from pydantic import BaseModel


class BloodMarkerIn(BaseModel):
    name: str
    category: str
    unit: str
    ref_low: float | None = None
    ref_high: float | None = None
    display_order: int = 0


class BloodResultIn(BaseModel):
    marker_id: int
    date: str
    value: float
    value_text: str = ""
    flag: str = ""
