FROM python:3.9

ENV POETRY_VERSION=1.3.2
ARG OCABOX_ROUTER_PORT

# System deps:
RUN pip install "poetry==$POETRY_VERSION"


WORKDIR ./
COPY . /.
COPY pyproject.toml ./

EXPOSE $OCABOX_ROUTER_PORT

RUN ["poetry", "install"]
STOPSIGNAL SIGINT
ENTRYPOINT ["poetry", "run", "server"]