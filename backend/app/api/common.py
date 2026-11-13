"""Explicit scaffold responses so missing functionality cannot look successful."""
from fastapi import HTTPException

def not_implemented(feature: str) -> None:
    raise HTTPException(status_code=501, detail={
        "code": "not_implemented", "message": f"{feature} is not implemented in this scaffold."
    })
