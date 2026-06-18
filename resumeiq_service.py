from __future__ import annotations

import cgi
import io
import json
import os
from http import HTTPStatus


MAX_UPLOAD_BYTES = 6 * 1024 * 1024
DEFAULT_MODEL = "claude-sonnet-4-20250514"
SYSTEM_PROMPT = """You are an expert technical recruiter and resume coach specializing in software engineering and AI/ML roles.

Analyze the resume provided and return ONLY a JSON object with this exact structure - no preamble, no markdown, no explanation:

{
  "overall_score": <integer 0-100>,
  "summary": "<2-3 sentence honest overall assessment>",
  "strengths": ["<strength 1>", "<strength 2>", "<strength 3>"],
  "weaknesses": ["<weakness 1>", "<weakness 2>", "<weakness 3>"],
  "missing_keywords": ["<keyword 1>", "<keyword 2>", "..."],
  "suggestions": [
    { "section": "<section name>", "issue": "<what's wrong>", "fix": "<specific fix>" },
    { "section": "<section name>", "issue": "<what's wrong>", "fix": "<specific fix>" },
    { "section": "<section name>", "issue": "<what's wrong>", "fix": "<specific fix>" }
  ],
  "ats_score": <integer 0-100>,
  "ats_notes": "<1-2 sentences on ATS compatibility>"
}"""


class ResumeIQError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = int(status)
        self.detail = detail


def configured_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


def dependency_status() -> list[str]:
    missing: list[str] = []

    try:
        import anthropic  # noqa: F401
    except ImportError:
        missing.append("anthropic")

    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        missing.append("pdfplumber")

    return missing


def status_payload() -> tuple[int, dict]:
    missing = dependency_status()
    if missing:
        return 200, {
            "status": "runtime_missing",
            "message": "ResumeIQ is bundled into the site, but the optional demo packages are not installed in this environment yet.",
            "missingPackages": missing,
            "sampleUrl": "/resumeiq/sample-analysis.json",
        }

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return 200, {
            "status": "setup_required",
            "message": "Add ANTHROPIC_API_KEY to enable live analysis. The prototype still works in sample mode.",
            "sampleUrl": "/resumeiq/sample-analysis.json",
            "model": configured_model(),
        }

    return 200, {
        "status": "ready",
        "message": "Live resume analysis is ready.",
        "sampleUrl": "/resumeiq/sample-analysis.json",
        "model": configured_model(),
    }


def _ensure_runtime_ready() -> None:
    missing = dependency_status()
    if missing:
        package_list = ", ".join(missing)
        raise ResumeIQError(
            "ResumeIQ is missing optional Python packages in this environment.",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=f"Install: {package_list}",
        )

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ResumeIQError(
            "ResumeIQ is not configured for live analysis yet.",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Set ANTHROPIC_API_KEY to enable the live analyzer.",
        )


def _parse_upload(environ: dict[str, str]) -> tuple[str, bytes]:
    content_type = environ.get("CONTENT_TYPE", "")
    if "multipart/form-data" not in content_type:
        raise ResumeIQError("Send the resume as multipart form data under the 'resume' field.")

    try:
        form = cgi.FieldStorage(fp=environ["wsgi.input"], environ=environ, keep_blank_values=True)
    except Exception as exc:  # noqa: BLE001
        raise ResumeIQError("Could not parse the upload request.", detail=str(exc)) from exc

    if "resume" not in form:
        raise ResumeIQError("No file uploaded. Add the PDF under the 'resume' field.")

    field = form["resume"]
    if isinstance(field, list):
        field = field[0]

    filename = (getattr(field, "filename", None) or "resume.pdf").strip() or "resume.pdf"
    if not filename.lower().endswith(".pdf"):
        raise ResumeIQError("Only PDF files are supported right now.")

    file_obj = getattr(field, "file", None)
    if file_obj is None:
        raise ResumeIQError("The uploaded file could not be read.")

    file_bytes = file_obj.read()
    if not file_bytes:
        raise ResumeIQError("The uploaded PDF was empty.")

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise ResumeIQError("Keep the PDF under 6 MB for the demo.")

    return filename, file_bytes


def _extract_text(file_bytes: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages).strip()


def _clean_json_block(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _run_model(resume_text: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model=configured_model(),
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Analyze this resume:\n\n{resume_text}"}],
    )

    parts = [getattr(block, "text", "") for block in getattr(message, "content", [])]
    raw = _clean_json_block("\n".join(part for part in parts if part).strip())
    if not raw:
        raise ResumeIQError(
            "The model returned an empty response.",
            status=HTTPStatus.BAD_GATEWAY,
        )

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResumeIQError(
            "The model response was not valid JSON.",
            status=HTTPStatus.BAD_GATEWAY,
            detail=raw,
        ) from exc


def analyze_payload(environ: dict[str, str]) -> tuple[int, dict]:
    try:
        _ensure_runtime_ready()
        filename, file_bytes = _parse_upload(environ)
        resume_text = _extract_text(file_bytes)
        if not resume_text:
            raise ResumeIQError("Could not extract readable text from that PDF.")

        result = _run_model(resume_text)
        result["status"] = "success"
        result["mode"] = "live"
        result["fileName"] = filename
        return 200, result
    except ResumeIQError as exc:
        payload = {"status": "error", "message": exc.message}
        if exc.detail:
            payload["detail"] = exc.detail
        return exc.status, payload
    except Exception as exc:  # noqa: BLE001
        return 500, {
            "status": "error",
            "message": "ResumeIQ hit an unexpected error while analyzing the file.",
            "detail": str(exc),
        }
