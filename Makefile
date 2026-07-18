PROJECT_NAME := $(shell basename $CURDIR)
VIRTUAL_ENVIRONMENT := $(CURDIR)/.venv
LOCAL_PYTHON := $(VIRTUAL_ENVIRONMENT)/bin/python
LOCAL_PYTHON_ACTIVATE := $(VIRTUAL_ENVIRONMENT)/bin/activate

define HELP
Manage $(PROJECT_NAME). Usage:

make install    - Create local virtualenv & install dependencies.
make kill       - Kill running instance of the bot.
make deploy     - Set up project & run locally.
make update     - Update dependencies via uv and refresh `uv.lock`.
make format     - Run Python code formatter & sort dependencies.
make lint       - Check code formatting with flake8.
make clean      - Remove extraneous compiled files, caches, logs, etc.

endef
export HELP

.PHONY: install deploy update format lint clean help


all help:
	@echo "$$HELP"

env: $(VIRTUAL_ENVIRONMENT)

$(VIRTUAL_ENVIRONMENT):
	if [ ! -d $(VIRTUAL_ENVIRONMENT) ]; then \
		echo "Creating Python virtual environment..."; \
		python3 -m venv $(VIRTUAL_ENVIRONMENT); \
	fi

.PHONY: install
install:
	uv sync && \
	echo "Installed dependencies in virtualenv \`${VIRTUAL_ENVIRONMENT}\`";

.PHONY: kill
kill:
	@if pgrep -f broiestbot > /dev/null; then \
		echo "Killing running instances of $(PROJECT_NAME)..."; \
		pkill -f broiestbot || true; \
	else \
		echo "No running instances of $(PROJECT_NAME) found."; \
	fi

.PHONY: deploy
deploy:
	make clean \
	make install \
	make run

.PHONY: test
test: env
	$(LOCAL_PYTHON) -m \
		coverage run -m pytest -v \
		--disable-pytest-warnings && \
		coverage html --title='Coverage Report' -d .reports && \
		open .reports/index.html

.PHONY: update
update:
	uv sync --upgrade && \
	echo "Updated dependencies in virtualenv \`${VIRTUAL_ENVIRONMENT}\`";

.PHONY: format
format: env
	$(LOCAL_PYTHON) -m isort --multi-line=3 . && \
	$(LOCAL_PYTHON) -m black --target-version py312 .

.PHONY: lint
lint: env
	$(LOCAL_PYTHON) -m flake8 . --count \
			--select=E9,F63,F7,F82 \
			--exclude .git,.github,__pycache__,.pytest_cache,.venv,logs,creds,.venv,docs,logs,.reports \
			--show-source \
			--statistics

.PHONY: clean
clean:
	find . -name '.coverage' -delete && \
	find . -wholename '**/*.pyc' -delete && \
	find . -wholename '**/logs/*.json' -exec rm -rf {} + && \
	find . -wholename '**/logs/*.log' -exec rm -rf {} + && \
	find . -type d -wholename '__pycache__' -exec rm -rf {} + && \
	find . -type d -wholename '.venv' -exec rm -rf {} + && \
	find . -type d -wholename '.pytest_cache' -exec rm -rf {} + && \
	find . -type d -wholename '**/.pytest_cache' -exec rm -rf {} + && \
	find . -type d -wholename './.reports/*' -exec rm -rf {} +