from pydantic import BaseModel


class DailyLogIn(BaseModel):
    date: str
    energy: int | None = None
    morning_routine: str = "pending"
    evening_routine: str = "pending"
    water: str = "pending"
    andy_body_status: str = "pending"
    andy_body_desc: str = ""
    andy_spirit_status: str = "pending"
    andy_spirit_desc: str = ""
    andy_account_status: str = "pending"
    andy_account_desc: str = ""
    andy_relations_status: str = "pending"
    andy_relations_desc: str = ""
    notes: str = ""


class BodyMeasurementIn(BaseModel):
    date: str
    weight: float | None = None
    arm: float | None = None
    waist: float | None = None
    hips: float | None = None
    thighs: float | None = None
