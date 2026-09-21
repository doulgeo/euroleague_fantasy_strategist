"""Projected-minutes and team-strength features, additive to engine/.

See reports/data_audit.md for the data audit this was built against, and
the milestone checkpoint reports in reports/ for what's validated so far.
Nothing here is wired into engine/ or app.py yet (see config.yaml's
integration.use_new_features - stays false until §5 integration is done
and the regression test in tests/test_integration.py passes).
"""
