.DEFAULT_GOAL := help
PYTHON ?= python
ifeq ($(OS),Windows_NT)
VENV_PYTHON := .venv/Scripts/python.exe
else
VENV_PYTHON := .venv/bin/python
endif
RUN = "$(VENV_PYTHON)" -m kindle_drop
SSH_PORT ?= 2222
WEB_PORT ?= 8765
LAN ?= 0
# Export user inputs directly. Never interpolate a URL or path into a shell recipe.
export KD_INPUT := $(INPUT)
export KD_GOALS := $(MAKECMDGOALS)
export KD_MOUNT := $(MOUNT)
export KD_HOST := $(HOST)
export KD_DESTINATION := $(DESTINATION)
export KD_FINGERPRINT := $(FINGERPRINT)
export KD_SSH_PORT := $(SSH_PORT)
export KD_WEB_PORT := $(WEB_PORT)
export KD_LAN := $(LAN)

.PHONY: help install doctor check verify key pair test send open tests
help:
	@"$(PYTHON)" scripts/help.py
install:
	"$(PYTHON)" -m venv .venv
	"$(VENV_PYTHON)" -m pip install -e .
doctor:
	@$(RUN) doctor
check:
	@$(RUN) doctor
	@$(RUN) check
verify:
	@$(RUN) doctor
	@$(RUN) verify
key:
	@$(RUN) doctor
	@$(RUN) key
pair:
	@$(RUN) pair
test:
	@$(RUN) test
send:
	@$(RUN) send
open:
	@$(RUN) open
tests:
	@"$(VENV_PYTHON)" -m unittest discover -s tests -v

# GNU Make positional goals: make send https://example.org/article books/*.epub
# Only sending enables this rule; typos in other commands remain errors.
ifeq ($(firstword $(MAKECMDGOALS)),send)
%:
	@$(RUN) noop
endif
