import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import swiglpk as glp
from wholecell.utils._netflow.nf_glpk import NetworkFlowGLPK
from wholecell.tests.utils.test_nf_glpk import Test_NetworkFlowGLPK


class RecoveryTests(unittest.TestCase):
    def model(self, bounded=True):
        model = NetworkFlowGLPK()
        model._maximize = True
        glp.glp_add_rows(model._lp, 1)
        glp.glp_set_row_bnds(model._lp, 1, glp.GLP_FR, 0, 0)
        glp.glp_add_cols(model._lp, 1)
        glp.glp_set_col_bnds(model._lp, 1, glp.GLP_DB if bounded else glp.GLP_LO, 0, 2)
        glp.glp_set_obj_coef(model._lp, 1, 1)
        return model

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, YGGDRISIL_SOLVER_DIAGNOSTICS=self.temp.name)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_ordinary_optimum_does_not_retry(self):
        model = self.model()
        model._solve()
        self.assertEqual(glp.glp_get_obj_val(model._lp), 2)
        self.assertFalse(list(Path(self.temp.name).iterdir()))

    def test_reported_unbounded_status_rebuilds_same_bounded_lp(self):
        model = self.model()
        actual = glp.glp_get_status
        statuses = iter([glp.GLP_UNBND])
        with patch.object(glp, 'glp_get_status', side_effect=lambda lp: next(statuses, actual(lp))):
            model._solve()
        self.assertEqual(glp.glp_get_obj_val(model._lp), 2)
        self.assertEqual(model._smcp.presolve, glp.GLP_OFF)
        record = json.loads(next(Path(self.temp.name).glob('*.json')).read_text())
        self.assertTrue(record['model_unchanged'])
        self.assertEqual(len(record['kkt']), 4)

    def test_truly_unbounded_model_still_fails(self):
        model = self.model(False)
        with self.assertRaisesRegex(RuntimeError, 'Fresh-basis retry failed'):
            model._solve()
        self.assertFalse(model._solved)
        self.assertEqual(model._smcp.presolve, glp.GLP_OFF)

    def test_nonfinite_residual_is_rejected(self):
        model = self.model()
        actual = glp.glp_get_status
        statuses = iter([glp.GLP_UNBND])
        def invalid_kkt(lp, sol, condition, absolute, abs_index, relative, rel_index):
            relative[0] = float('nan')
        with patch.object(glp, 'glp_get_status', side_effect=lambda lp: next(statuses, actual(lp))), \
             patch.object(glp, 'glp_check_kkt', side_effect=invalid_kkt):
            with self.assertRaisesRegex(RuntimeError, 'KKT residual'):
                model._solve()
        self.assertFalse(model._solved)


    def test_failed_recovery_cannot_be_bypassed_by_outer_retry(self):
        model = self.model(False)
        with self.assertRaises(RuntimeError):
            model._solve()
        with patch.object(glp, 'glp_simplex', side_effect=AssertionError('Rejected recovery was retried')):
            with self.assertRaisesRegex(RuntimeError, 'Previous recovery failed validation'):
                model._solve()

    def test_captured_failed_cell_lp_passes_strict_validation(self):
        source = next(Path('/home/ubuntu/yggdrisil-vecoli-20261105/runs/seed-101-frozen-dossier-recovery/cells/generation-7/solver-diagnostics').glob('*.before.glp'))
        model = NetworkFlowGLPK()
        self.assertEqual(glp.glp_read_prob(model._lp, 0, str(source)), 0)
        model._maximize = glp.glp_get_obj_dir(model._lp) == glp.GLP_MAX
        actual = glp.glp_get_status
        statuses = iter([glp.GLP_UNBND])
        with patch.object(glp, 'glp_get_status', side_effect=lambda lp: next(statuses, actual(lp))):
            model._solve()
        record = json.loads(next(Path(self.temp.name).glob('*.json')).read_text())
        self.assertTrue(record['model_unchanged'])
        self.assertTrue(all(c['relative_error'] <= 1e-7 for c in record['kkt'].values()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
