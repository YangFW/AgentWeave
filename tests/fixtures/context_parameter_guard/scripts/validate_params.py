"""Reference validator shipped with the Skill package."""

REQUIRED_BY_INTENT = {
    "project_analysis": {"project_code"},
    "weather": {"city", "day"},
    "document_generation": {"format"},
}


def missing(intent: str, parameters: dict) -> list[str]:
    return sorted(key for key in REQUIRED_BY_INTENT.get(intent, set()) if parameters.get(key) in (None, ""))
