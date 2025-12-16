from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="BumbleBee Bot", version="0.1.0")


@app.get("/")
def root():
    return {
        "status": "online",
        "service": "bumblebee",
        "message": "BumbleBee is running 🐝"
    }


@app.get("/health")
def health():
    return JSONResponse(
        {
            "ok": True
        }
    )
