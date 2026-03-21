from pydantic import BaseModel


class LifeScoreIn(BaseModel):
    date: str
    planning: int | None = None
    spirituality: int | None = None
    health: int | None = None
    work: int | None = None
    social: int | None = None
    growth: int | None = None
    relaxation: int | None = None
    family: int | None = None
    power_level: float | None = None
    weight: float | None = None
    waist: float | None = None
    pmo_status: str = ""
    energy_avg: float | None = None
    linkedin_followers: int | None = None
    youtube_views: int | None = None
    revenue: float | None = None
    diagnostic: str = ""
    priorities: str = ""
