from functools import lru_cache

import anthropic
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .evaluator import EvaluationRefused, evaluate
from .models import Evaluation, EvaluationRequest

load_dotenv()

app = FastAPI(title="Interview Evaluator")


@lru_cache
def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/evaluate", response_model=Evaluation)
def evaluate_candidate(
    req: EvaluationRequest, client: anthropic.Anthropic = Depends(get_client)
) -> Evaluation:
    try:
        return evaluate(req, client)
    except EvaluationRefused as e:
        raise HTTPException(422, f"Model declined to evaluate: {e}")
    except anthropic.RateLimitError:
        raise HTTPException(429, "LLM rate limit hit, retry shortly")
    except anthropic.AuthenticationError:
        raise HTTPException(500, "LLM credentials are missing or invalid")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"LLM API error ({e.status_code})")
    except anthropic.APIConnectionError:
        raise HTTPException(503, "Could not reach the LLM API")
