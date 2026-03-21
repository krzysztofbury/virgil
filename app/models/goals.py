from pydantic import BaseModel


class GoalIn(BaseModel):
    area_id: int
    horizon: str
    content: str
    display_order: int = 0
