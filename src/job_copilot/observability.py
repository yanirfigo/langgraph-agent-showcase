"""LangSmith tracing, switched on in code only.

Nothing is read from or written to os.environ: the client gets the key, region URL and workspace
explicitly, and tracing is enabled per run with tracing_context(). Without a LangSmith key the
agent runs normally with tracing off. `hidden=True` keeps run structure, timing, tokens and errors
but sends no inputs or outputs: used wherever the CV or a draft is in play (the bot process).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from langsmith import Client, tracing_context

from .config import Config
from .secrets import Secrets


def make_client(cfg: Config, secrets: Secrets, *, hidden: bool = False) -> Client | None:
    if not secrets.langsmith_api_key:
        return None
    return Client(
        api_key=secrets.langsmith_api_key,
        api_url=cfg.langsmith.api_url,
        workspace_id=cfg.langsmith.workspace_id,  # org-scoped service keys must name the workspace
        hide_inputs=hidden,
        hide_outputs=hidden,
    )


@contextlib.contextmanager
def tracing(cfg: Config, secrets: Secrets, *, hidden: bool = False) -> Iterator[Client | None]:
    client = make_client(cfg, secrets, hidden=hidden)
    if client is None:
        with tracing_context(enabled=False):
            yield None
        return
    try:
        with tracing_context(enabled=True, client=client, project_name=cfg.langsmith.project):
            yield client
    finally:
        client.flush()
