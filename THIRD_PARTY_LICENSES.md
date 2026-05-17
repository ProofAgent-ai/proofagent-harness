# Third-Party Licenses

`proofagent-harness` (Copyright 2025-2026 ProofAI LLC, Apache-2.0) depends on
the open-source libraries listed below. Each is governed by its own license,
linked from its project homepage. None of the libraries below are *bundled*
into our wheel — they are declared as runtime dependencies and installed by
pip on the user's machine — but we list them here as good open-source
hygiene and to make compliance review easy.

If you redistribute a binary that includes any of these libraries, retain
their original LICENSE / NOTICE files alongside ours.

Last reviewed: 2026-05-17.

---

## Runtime dependencies

| Package | Version pin | License | Project |
|---|---|---|---|
| **langgraph** | `>=0.2.0` | MIT | <https://github.com/langchain-ai/langgraph> |
| **langchain-core** | `>=0.3.0` | MIT | <https://github.com/langchain-ai/langchain> |
| **langchain** | `>=0.3.0` | MIT | <https://github.com/langchain-ai/langchain> |
| **litellm** | `>=1.50.0` | MIT | <https://github.com/BerriAI/litellm> |
| **pydantic** | `>=2.0` | MIT | <https://github.com/pydantic/pydantic> |
| **python-frontmatter** | `>=1.0` | MIT | <https://github.com/eyeseast/python-frontmatter> |
| **typer** | `>=0.12` | MIT | <https://github.com/tiangolo/typer> |
| **rich** | `>=13.0` | MIT | <https://github.com/Textualize/rich> |
| **jinja2** | `>=3.0` | BSD-3-Clause | <https://github.com/pallets/jinja> |
| **pyyaml** | `>=6.0` | MIT | <https://github.com/yaml/pyyaml> |

## Optional / dev-only dependencies (not installed by default)

These ship only when users install with `pip install "proofagent-harness[dev]"`
or `[notebooks]` / `[docs]`. They are not part of the runtime path.

### `[dev]`

| Package | License | Project |
|---|---|---|
| **pytest** | MIT | <https://github.com/pytest-dev/pytest> |
| **pytest-asyncio** | Apache-2.0 | <https://github.com/pytest-dev/pytest-asyncio> |
| **pytest-cov** | MIT | <https://github.com/pytest-dev/pytest-cov> |
| **ruff** | MIT | <https://github.com/astral-sh/ruff> |
| **pyright** | MIT | <https://github.com/microsoft/pyright> |
| **pre-commit** | MIT | <https://github.com/pre-commit/pre-commit> |
| **build** | MIT | <https://github.com/pypa/build> |
| **twine** | Apache-2.0 | <https://github.com/pypa/twine> |

### `[notebooks]`

| Package | License | Project |
|---|---|---|
| **jupyter** | BSD-3-Clause | <https://github.com/jupyter/jupyter> |
| **ipykernel** | BSD-3-Clause | <https://github.com/ipython/ipykernel> |

### `[docs]`

| Package | License | Project |
|---|---|---|
| **mkdocs-material** | MIT | <https://github.com/squidfunk/mkdocs-material> |
| **mkdocstrings[python]** | ISC | <https://github.com/mkdocstrings/mkdocstrings> |

---

## License summary

All runtime dependencies are MIT or BSD-3-Clause — both are permissive
licenses fully compatible with our Apache-2.0 license. There are no
copyleft (GPL / AGPL / LGPL) or "source available" dependencies in
the runtime path.

If you spot a missing or incorrect license attribution, please open
an issue or PR: <https://github.com/ProofAgent-ai/proofagent-harness/issues>.
