# Contributing to Virgil

Thank you for your interest in contributing! This document provides guidelines to make the process smooth for everyone.

## Getting Started

1. Fork the repository
2. Clone your fork and set up the development environment:
    ```bash
    git clone https://github.com/YOUR_USERNAME/virgil.git
    cd virgil
    uv sync
    ```
3. Install pre-commit hooks:
    ```bash
    uv run pre-commit install
    ```
4. Create a branch for your change:
    ```bash
    git checkout -b your-branch-name
    ```

## Development Workflow

### Running Locally

```bash
# Start the development server (hot reload enabled)
VIRGIL_SECOND_BRAIN_PATH="/path/to/second-brain" uv run python -m app
```

Navigate to `http://localhost:8123`. On first launch you'll be redirected to `/setup` to create your account.

### Code Style

- **Line length**: 120 columns
- **Python target**: 3.12+
- **Formatter/Linter**: [ruff](https://github.com/astral-sh/ruff)
- **Security scanner**: [bandit](https://github.com/PyCQA/bandit)
- Comments explain "why", not "what"
- Server-side input validation on all form endpoints
- All secrets (API keys, OAuth tokens) must be Fernet-encrypted at rest

### Linting and Formatting

Before submitting, ensure your code passes:

```bash
ruff check app/ scripts/ --fix
ruff format app/ scripts/
bandit -c pyproject.toml -r app/
```

### Database Migrations

If your change modifies the database schema:

1. Create a new migration file in `app/migrations/` with the next sequence number
2. Expose an `async def up(db)` function
3. Each migration must be idempotent (safe to run on existing databases)
4. Document what the migration does in a module-level docstring

See existing migrations for examples.

### Testing

Manual integration testing via browser. When testing:

- Verify both dark and light themes
- Test on mobile viewport (PWA)
- Check keyboard shortcuts still work
- Verify HTMX partial responses render correctly

## Submitting Changes

1. Commit your changes with a clear, descriptive message
2. Push to your fork
3. Open a Pull Request against `main`
4. Describe what your change does and **why**

### What Makes a Good PR

- **Focused**: One logical change per PR
- **Documented**: Update README.md if you add/change features
- **Linted**: All ruff and bandit checks pass with zero warnings
- **Tested**: You have verified the change works in the browser

## Project Structure

See [SPEC.md](SPEC.md) for architecture details, key design decisions, and conventions.

## Reporting Bugs

Open a GitHub issue with:

- What you expected to happen
- What actually happened
- Steps to reproduce
- Your Python version, OS, and browser

## Security Issues

**Do not open public issues for security vulnerabilities.** See [SECURITY.md](SECURITY.md) for responsible disclosure instructions.

## Questions?

Open a GitHub issue or reach out at [contact@datacraze.io](mailto:contact@datacraze.io).
