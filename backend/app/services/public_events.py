"""Public progress excludes raw prompts, tool arguments, command output and reasoning."""


def public_event(record):
    payload = record.payload or {}
    allowed = {
        k: payload[k]
        for k in (
            "artifact_id",
            "report_id",
            "review_id",
            "candidate_model_id",
            "upload_id",
            "sha256",
            "decision",
            "conditions",
        )
        if k in payload
    }
    if record.action == "agent_event":
        allowed = {"type": payload.get("type"), "task_id": payload.get("task_id")}
    elif record.action in ("tool_call", "tool_output", "tool_result"):
        allowed = {
            "tool": payload.get("name"),
            "status": payload.get("status", payload.get("result", {}).get("status")),
        }
    return dict(
        id=record.id,
        resource_id=record.resource_id,
        action=record.action,
        created_at=record.created_at,
        payload=allowed,
    )
