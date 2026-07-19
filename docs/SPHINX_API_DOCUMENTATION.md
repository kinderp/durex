# Sphinx API Documentation

This document explains how Durex generates the current Sphinx HTML API
reference from Python docstrings.

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

## Sphinx structure

Durex now keeps Sphinx source files separate from the existing Markdown guides:

```text
docs/
  sphinx/
    conf.py
    index.rst
    api.rst
    maintenance.rst
  _build/
    html/
```

The generated HTML lives under `docs/_build/html/`. That directory is ignored by
Git because it is a generated artifact.

The Sphinx configuration uses:

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

## API modules

The generated runtime reference includes the application modules:

- `approval_detector`
- `approval_policy`
- `codex_queue`
- `pty_runner`
- `telegram_bridge`
- `telegram_control`
- `voice_commands`
- `voice_transcriber`

`scripts.check_cli_docs` is included in a separate maintenance section because
it is a repository validation tool, not part of the runtime API.

Tests can be left out of the public API reference. Their docstrings still matter
because they document the behavioral contracts protected by each regression
case.

## Build and validation commands

Install development dependencies with:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
```

Generate HTML with:

```bash
.venv/bin/python scripts/build_api_docs.py
```

The script runs Sphinx in warning-as-error mode by default. This is the
recommended pre-merge validation mode because broken imports, malformed
docstrings, and missing references should fail early.

For a non-strict local preview, run:

```bash
.venv/bin/python scripts/build_api_docs.py --no-strict
```

After large docstring restructures, force Sphinx to rebuild its environment:

```bash
.venv/bin/python scripts/build_api_docs.py --fresh-env
```

## Manual HTML generation

The generated HTML entry point is:

```text
docs/_build/html/index.html
```

To generate it manually from a fresh checkout, create the virtualenv, install
the development dependencies, and run the Sphinx build:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python scripts/build_api_docs.py --fresh-env
```

Use the strict default command for normal validation:

```bash
.venv/bin/python scripts/build_api_docs.py
```

Use `--fresh-env` after changing docstrings or Sphinx source files when you want
Sphinx to discard its cached environment and reread every module from scratch.

## Manual module registration

The current Sphinx setup does not automatically discover every new Python file.
The generated API reference includes only modules explicitly listed in the
Sphinx source files.

When adding a new runtime module, add an `automodule` entry to
`docs/sphinx/api.rst`. When adding a repository maintenance script, add it to
`docs/sphinx/maintenance.rst`.

For example, a new `config_loader.py` runtime module should be registered like
this:

```rst
config_loader
-------------

.. automodule:: config_loader
   :members:
   :undoc-members:
   :show-inheritance:
```

If this step is missed, the module can have correct docstrings and the Sphinx
build can still pass, but the module will not appear in the generated HTML API
reference.

## Future automation

A future commit can make API documentation stricter and more automated by
adding:

- optional `sphinx-apidoc` or `autosummary_generate` support to reduce manual
  module-registration risk once the package layout is formalized;
- an optional generated API reference diff check for release branches;
- a package layout so modules can be imported through a stable `durex.*`
  namespace instead of top-level module names.
