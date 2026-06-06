# Sphinx API Documentation Roadmap

This document explains how the current Python docstrings are prepared for a
future Sphinx-generated HTML API reference.

## Current documentation contract

Every Python module in the repository has a module-level docstring that explains
the role of the file in the Durex system. Every class and function has a
docstring so Sphinx can extract a complete API surface without leaving unnamed
objects in the generated reference.

The codebase uses Google-style docstring sections because they are readable in
source code and can be rendered by Sphinx through the `sphinx.ext.napoleon`
extension:

- `Args` describes each input parameter and the concept behind it.
- `Returns` describes the value returned by the function, including `None`
  where the function mutates state, prints output, or drives a long-running
  process.
- `Raises` documents exceptions that are part of the public contract.
- `Attributes` documents dataclass fields and other state that callers or tests
  are expected to understand.

Inline comments should be used sparingly. They should explain a concept,
boundary, invariant, or non-obvious design choice. They should not narrate the
mechanical line-by-line behavior that Python already makes clear.

## Recommended Sphinx structure

When the API reference is generated, keep the source documentation separate from
the existing Markdown guides:

```text
docs/
  sphinx/
    conf.py
    index.rst
    api.rst
  _build/
    html/
```

The recommended initial Sphinx extensions are:

```python
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
]
```

`autodoc` extracts modules, classes, and functions. `autosummary` can generate
compact API tables. `napoleon` renders the Google-style docstrings currently
used in the codebase. `viewcode` links generated documentation back to source
code.

## Initial API modules

The first generated reference should include the application modules:

- `approval_detector`
- `approval_policy`
- `codex_queue`
- `pty_runner`
- `telegram_bridge`
- `telegram_control`

`scripts.check_cli_docs` can be included in a separate maintenance section
because it is a repository validation tool, not part of the runtime API.

Tests can be left out of the public API reference. Their docstrings still matter
because they document the behavioral contracts protected by each regression
case.

## Build and validation commands

After adding Sphinx dependencies and scaffolding, generate HTML with:

```bash
python -m sphinx -b html docs/sphinx docs/_build/html
```

For CI or pre-merge validation, use warning-as-error mode:

```bash
python -m sphinx -W -b html docs/sphinx docs/_build/html
```

The stricter command should be the target for future automation because broken
imports, malformed docstrings, and missing references should fail the
documentation build early.

## Future automation

A future commit can make API documentation reproducible by adding:

- a development dependency group containing Sphinx and optional docstring
  helpers;
- `docs/sphinx/conf.py` with the extensions above;
- an `api.rst` file with `automodule` directives for each application module;
- a CI check or local script that runs the warning-as-error Sphinx build;
- optional `sphinx-apidoc` or `autosummary_generate` support once the package
  layout is formalized.
