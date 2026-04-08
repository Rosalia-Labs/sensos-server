FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN python3 -m venv venv
ENV PATH="/app/venv/bin:$PATH"

RUN pip install --upgrade pip && \
    pip install fastapi uvicorn psycopg[binary]

ARG VERSION_MAJOR
ARG VERSION_MINOR
ARG VERSION_PATCH
ARG VERSION_SUFFIX
ARG GIT_COMMIT
ARG GIT_BRANCH
ARG GIT_TAG
ARG GIT_DIRTY

ENV VERSION_MAJOR=${VERSION_MAJOR:-"unknown"}
ENV VERSION_MINOR=${VERSION_MINOR:-"unknown"}
ENV VERSION_PATCH=${VERSION_PATCH:-"unknown"}
ENV VERSION_SUFFIX=${VERSION_SUFFIX:-""}
ENV GIT_COMMIT=${GIT_COMMIT:-"unknown"}
ENV GIT_BRANCH=${GIT_BRANCH:-"unknown"}
ENV GIT_TAG=${GIT_TAG:-"unknown"}
ENV GIT_DIRTY=${GIT_DIRTY:-"false"}

COPY . ./

CMD uvicorn main:app --host 0.0.0.0 --port 8780
