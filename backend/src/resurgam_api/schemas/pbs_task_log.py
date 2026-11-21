from pydantic import BaseModel


class TaskLogLineOut(BaseModel):
    n: int
    text: str


class TaskLogOut(BaseModel):
    lines: list[TaskLogLineOut]
    truncated: bool
