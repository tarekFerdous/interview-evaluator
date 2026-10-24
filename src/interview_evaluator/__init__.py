def main() -> None:
    import uvicorn

    uvicorn.run("interview_evaluator.api:app", reload=True)
