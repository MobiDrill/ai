from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="오류 식별 코드", examples=["UNSUPPORTED_FORMAT"])
    message: str = Field(description="원본 문서 내용을 포함하지 않는 오류 메시지", examples=["Unsupported document format."])
    request_id: str = Field(description="서버가 생성한 요청 식별자", examples=["15d36cb827dd4a97b779d161d9372c64"])


class ErrorResponse(BaseModel):
    error: ErrorDetail
