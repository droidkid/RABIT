#!/usr/bin/env python3
"""Run Sections 5.1/5.2 from the repository root."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    sys.dont_write_bytecode = True
    directory = Path(__file__).resolve().parent / 'experiment'
    sys.path.insert(0, str(directory))
    runpy.run_path(str(directory / 'run_experiments_all.py'), run_name='__main__')
