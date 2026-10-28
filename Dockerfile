FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml /app/
COPY bank_agent /app/bank_agent
RUN pip install --no-cache-dir . && useradd --create-home bankapp && mkdir /app/.data && chown bankapp /app/.data
USER bankapp
ENV BANK_DATA_DIR=/app/.data
EXPOSE 8000
CMD ["uvicorn", "bank_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
