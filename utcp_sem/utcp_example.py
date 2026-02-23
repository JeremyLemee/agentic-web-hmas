from fastapi import FastAPI, Body, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from utcp.data.utcp_manual import UtcpManual
from utcp.python_specific_tooling.tool_decorator import utcp_tool
from utcp_http import HttpCallTemplate

import uvicorn

app = FastAPI()


class MultiplyResponse(BaseModel):
    result: int = Field(description="The product of a and b")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    print("\n--- VALIDATION ERROR ---")
    print("method:", request.method)
    print("url:", request.url)
    print("headers:", dict(request.headers))
    print("raw body:", body.decode("utf-8", errors="replace"))
    print("errors:", exc.errors())
    print("------------------------\n")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/utcp")
def utcp_discovery():
    return UtcpManual.create_from_decorators(manual_version="1.0.0")


@utcp_tool(
    tool_call_template=HttpCallTemplate(
        name="multiply",
        call_template_type="http",
        url="http://localhost:8085/multiply",
        http_method="POST",
        content_type="application/json",
    ),
    tags=["math"],
    description="An agent can use this tool to perform multiplication",
)
@app.post("/multiply", response_model=MultiplyResponse)
def multiply(
    a: int = Body(..., description="First integer"),
    b: int = Body(..., description="Second integer"),
) -> MultiplyResponse:
    return MultiplyResponse(result=a * b)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8085)
