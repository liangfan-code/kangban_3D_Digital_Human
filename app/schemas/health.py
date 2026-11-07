"""健康检查响应结构。"""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    app_env: str
    trace_id: str | None = None
