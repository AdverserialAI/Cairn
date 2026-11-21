FROM ghcr.io/astral-sh/uv:python3.13-trixie

COPY ./cairn/pyproject.toml /cairn/pyproject.toml
COPY ./cairn/uv.lock /cairn/uv.lock
WORKDIR /cairn
RUN uv sync --frozen --no-install-project -i https://pypi.org/simple

COPY ./cairn /cairn
RUN uv sync --frozen -i https://pypi.org/simple

ENV TZ=UTC