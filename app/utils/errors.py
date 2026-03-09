from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ErrorBody:
    code: str
    message: str


class AppError(Exception):
    code: str = "APP_ERROR"
    status_code: int = 500
    message: str = "application error"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.message
        super().__init__(self.message)

    def to_body(self) -> dict[str, dict[str, str]]:
        body = ErrorBody(code=self.code, message=self.message)
        return {"error": {"code": body.code, "message": body.message}}


class InvalidInputError(AppError):
    code = "INVALID_INPUT"
    status_code = 400
    message = "invalid input"


class UnsupportedFormatError(AppError):
    code = "UNSUPPORTED_FORMAT"
    status_code = 400
    message = "unsupported file format"


class PayloadTooLargeError(AppError):
    code = "PAYLOAD_TOO_LARGE"
    status_code = 413
    message = "uploaded file is too large"


class WpsStartupError(AppError):
    code = "WPS_STARTUP_FAILED"
    status_code = 503
    message = "failed to start wps runtime"


class WpsOpenDocumentError(AppError):
    code = "WPS_OPEN_DOCUMENT_FAILED"
    status_code = 422
    message = "failed to open input document"


class WpsConversionError(AppError):
    code = "WPS_CONVERSION_FAILED"
    status_code = 500
    message = "failed to convert document to pdf"


class ConversionTimeoutError(AppError):
    code = "CONVERSION_TIMEOUT"
    status_code = 504
    message = "conversion timed out"
