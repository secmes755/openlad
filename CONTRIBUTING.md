# Contributing to OpenLAD

Thank you for your interest in contributing to OpenLAD!

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/your-org/OpenLAD.git`
3. Create a virtual environment: `python3 -m venv .venv`
4. Install dependencies: `pip install -r requirements.txt`
5. Set the admin password: `export OPENLAD_ADMIN_PASSWORD=your_secure_password`
6. Start the service: `./start.sh`

## Development Guidelines

- **No hardcoding in core/**: All industry-specific logic belongs in `industries/`
- **Environment variables**: Use `OPENLAD_*` prefix for all configurable parameters
- **Code style**: Follow PEP 8, use type hints where practical

## Submitting Changes

1. Create a feature branch: `git checkout -b feature/your-feature`
2. Make your changes with clear commit messages
3. Verify your changes work locally
4. Submit a pull request with a description of the changes

## Questions?

Open an issue or reach out to the maintainers.
