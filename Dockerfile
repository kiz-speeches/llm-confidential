FROM vllm/vllm-openai:v0.31.0@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3 AS weights
WORKDIR /build
COPY model.py model.lock.json ./
RUN python3 model.py /model --download

FROM vllm/vllm-openai:v0.31.0@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3 AS checked
WORKDIR /app
COPY api.py run.py model.py model.lock.json ./
COPY tests ./tests
RUN python3 -m unittest discover -s tests -v
# Only parse CLI options here; a GitHub-hosted builder has no GPU.
RUN VLLM_TARGET_DEVICE=cpu python3 -c 'import subprocess, run; subprocess.run(run.engine_command() + ["--help"], check=True, stdout=subprocess.DEVNULL)'

FROM vllm/vllm-openai:v0.31.0@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3
LABEL org.opencontainers.image.source="https://github.com/kiz-speeches/llm-confidential"
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 VLLM_NO_USAGE_STATS=1 \
    DO_NOT_TRACK=1 HOME=/tmp HF_HOME=/tmp/huggingface \
    VLLM_CACHE_ROOT=/tmp/vllm XDG_CACHE_HOME=/tmp/cache \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=weights /model /model
COPY --from=checked /app/api.py /app/run.py /app/model.py /app/model.lock.json ./
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python3", "/app/run.py"]
