from types import SimpleNamespace

import numpy as np
import pytest

from yggdrisil_ecoli.data.errors import DataValidationError
from yggdrisil_ecoli.scorers.rba import RBAScorer


@pytest.fixture
def scorer() -> RBAScorer:
    pytest.importorskip("scipy")
    from scipy.optimize import OptimizeWarning, linprog
    from scipy.sparse import csr_matrix, vstack

    # Exercise the numerical boundary without downloading the biological model.
    scorer = RBAScorer.__new__(RBAScorer)
    scorer._numpy = np
    scorer._linprog = linprog
    scorer._optimize_warning = OptimizeWarning
    scorer._vstack = vstack
    scorer._session = SimpleNamespace(
        Problem=SimpleNamespace(
            LP=SimpleNamespace(
                A=csr_matrix([[1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]),
                b=np.array([1.0, 3.0, 2.0]),
                row_signs=["G", "E", "L"],
                f=np.zeros(2),
                LB=np.zeros(2),
                UB=np.full(2, 3.0),
                col_names=["required", "alternative"],
            )
        )
    )
    scorer._prepare_highs_problem()
    return scorer


def test_row_conversion_and_knockouts_do_not_contaminate_siblings(
    scorer: RBAScorer,
) -> None:
    original_bounds = scorer._base_bounds.copy()

    assert scorer._solve_with_knockouts([])[0] == "optimal"
    assert scorer._solve_with_knockouts(["required"])[0] == "infeasible"
    assert scorer._solve_with_knockouts(["alternative"])[0] == "optimal"
    assert scorer._solve_with_knockouts([])[0] == "optimal"
    np.testing.assert_array_equal(scorer._base_bounds, original_bounds)


def test_indeterminate_status_is_retried_and_recorded(
    scorer: RBAScorer, monkeypatch: pytest.MonkeyPatch
) -> None:
    results = iter(
        [
            SimpleNamespace(status=4, message="unknown"),
            SimpleNamespace(status=2, message="infeasible"),
        ]
    )
    monkeypatch.setattr(scorer, "_linprog", lambda *args, **kwargs: next(results))

    status, solution_type, attempts = scorer._solve_with_knockouts([])

    assert (status, solution_type) == ("infeasible", "HiGHS")
    assert attempts == [
        {"method": "highs", "presolve": True, "status_code": 4},
        {"method": "highs-ipm", "presolve": True, "status_code": 2},
    ]


@pytest.mark.parametrize("status", [1, 3, 4])
def test_unresolved_solver_status_raises_instead_of_becoming_nonviability(
    scorer: RBAScorer, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(
        scorer,
        "_linprog",
        lambda *args, **kwargs: SimpleNamespace(status=status, message="unresolved"),
    )

    with pytest.raises(DataValidationError, match="did not establish feasibility"):
        scorer._solve_with_knockouts([])


def test_unknown_constraint_sense_is_rejected(scorer: RBAScorer) -> None:
    scorer._session.Problem.LP.row_signs = ["G", "E", "unknown"]

    with pytest.raises(DataValidationError, match="unknown row signs"):
        scorer._prepare_highs_problem()
