"""Stable GUI entry point: python -m TrainingController."""
from .ui import Controller, main, ROOT, PREPARATION_LABELS, PREPARATION_STATES
from .task_catalog import TASKS, TASK_GROUPS, TASK_FIELDS, DEFAULT_CONFIGS

if __name__ == '__main__':
    main()
