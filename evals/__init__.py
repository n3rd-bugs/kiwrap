"""Agent evals for kiwrap (docs/ARCHITECTURE.md section 9 "Evals", docs/DECISIONS.md D5 / D15).

`evals.reference_board` holds the D5 reference ESP32-S3 board: the natural-language spec an
agent gets (`spec.md`), the design plan the scripted conversation follows (`plan.py` ->
`design_plan.json`), the KiCad-graded rubric (`grader.py`) and the manual `claude -p` runner
(`runner_claude.py`). The scripted end-to-end conversation is `tests/test_e2e_reference_board.py`.
"""
