"""The unattended coding loop: generator, trusted verification, evaluator.

Layered so the decisions are testable without a container or a model:

    verdict.py     what the evaluator returned, and a tolerant parser for it
    budget.py      the caps a run may not exceed
    controller.py  pure continue-vs-stop rules
    evaluator.py   the independent grader's prompt and verdict contract
    receipts.py    commands THIS SERVICE ran, so a pass is not self-reported
    loop.py        the driver that performs the I/O and ties the above together

Mechanisms borrowed in shape from agent_team's ``features/board/runtime/loop``.
Kept as this service's own copy, never an import: the service must boot with
BAM absent (see the package docstring and
``tests/test_bloy_dev_agent.py::test_the_service_imports_nothing_from_bam``).
"""
