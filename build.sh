#!/bin/sh
pipenv clean
pipenv update
pipenv run pip freeze > requirements.txt
