from pathlib import Path
import runpy


def test_original_tests_can_use_candidate_helpers_and_globals():
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'sandbox' / 'runner.py'))
    # Only this trusted regression fixture executes on the host; dataset code
    # is validated separately by scripts/verify_dataset.py inside Docker.
    code = '''_offset = 2
def helper(value):
    return value + _offset
def solve(value):
    return helper(value)
'''
    original_test = '''def check(candidate):
    assert candidate(3) == helper(3) == 5
    assert candidate(0) == _offset
'''
    program = runner['test_program']({'code': code, 'test': original_test, 'entry_point': 'solve'})
    namespace = {}
    exec(program, namespace)
    namespace['test_original_humaneval']()
