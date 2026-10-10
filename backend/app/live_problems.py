"""Original Easy-level specifications. Only the designer writes the displayed statement.

The fixed tests and reference implementations stay on the server. These are not
copied LeetCode problems; the difficulty/style is comparable to its Easy category.
"""
from .contracts import digest


PROBLEMS = (
    {
        'key': 'running-totals', 'title': '차곡차곡 누적 합',
        'signature': 'def running_totals(values: list[int]) -> list[int]:',
        'description': '정수 목록 values의 각 위치에 대해, 처음부터 그 위치까지의 합을 담은 새 목록을 반환하세요. 빈 목록에는 빈 목록을 반환합니다.',
        'inputs': ['values: 정수 목록'], 'outputs': ['각 위치까지의 누적 합을 담은 정수 목록'],
        'examples': ['running_totals([2, -1, 4]) → [2, 1, 5]', 'running_totals([]) → []'],
        'constraints': ['0 ≤ len(values) ≤ 1000', '-1000 ≤ values[i] ≤ 1000'],
        'solution': '    total = 0\n    result = []\n    for value in values:\n        total += value\n        result.append(total)\n    return result\n',
        'cases': [(([2, -1, 4],), [2, 1, 5]), (([],), []), (([0],), [0]),
                  (([-3, -2, -1],), [-3, -5, -6]), (([5, -5, 5],), [5, 0, 5]),
                  (([1000] * 1000,), list(range(1000, 1000001, 1000)))],
    },
    {
        'key': 'first-unique', 'title': '한 번만 등장한 글자',
        'signature': 'def first_unique_index(text: str) -> int:',
        'description': '문자열 text에서 한 번만 등장하는 문자 중 가장 왼쪽 문자의 인덱스를 반환하세요. 인덱스는 0부터 시작합니다. 해당 문자가 없거나 문자열이 비어 있으면 -1을 반환합니다.',
        'inputs': ['text: 영문 소문자로만 이루어진 문자열'], 'outputs': ['첫 번째 고유 문자의 인덱스 또는 -1'],
        'examples': ['first_unique_index("aabccd") → 2', 'first_unique_index("aabb") → -1'],
        'constraints': ['0 ≤ len(text) ≤ 10000', 'text의 문자는 영문 소문자 a–z'],
        'solution': '    counts = {}\n    for char in text:\n        counts[char] = counts.get(char, 0) + 1\n    for index, char in enumerate(text):\n        if counts[char] == 1:\n            return index\n    return -1\n',
        'cases': [(('aabccd',), 2), (('aabb',), -1), (('',), -1), (('z',), 0),
                  (('abacb',), 3), (('abc',), 0), (('a' * 9999 + 'b',), 9999)],
    },
    {
        'key': 'pair-sum', 'title': '목표 합을 만드는 두 수',
        'signature': 'def has_pair_sum(values: list[int], target: int) -> bool:',
        'description': '정수 목록 values에서 서로 다른 두 위치의 값을 더해 target을 만들 수 있으면 True, 없으면 False를 반환하세요. 값이 같더라도 위치가 다르면 두 원소를 선택할 수 있습니다.',
        'inputs': ['values: 정수 목록', 'target: 목표 합'], 'outputs': ['조건을 만족하는 서로 다른 두 위치의 존재 여부'],
        'examples': ['has_pair_sum([4, 1, 7], 8) → True', 'has_pair_sum([3], 6) → False', 'has_pair_sum([3, 3], 6) → True'],
        'constraints': ['0 ≤ len(values) ≤ 1000', '-1000 ≤ values[i] ≤ 1000', '-2000 ≤ target ≤ 2000'],
        'solution': '    seen = set()\n    for value in values:\n        if target - value in seen:\n            return True\n        seen.add(value)\n    return False\n',
        'cases': [(([4, 1, 7], 8), True), (([3], 6), False), (([3, 3], 6), True),
                  (([], 0), False), (([-4, 6, 1], 2), True), (([0, 0], 0), True),
                  (([1, 2, 3], 9), False), (([1000] * 1000, 2000), True)],
    },
    {
        'key': 'balanced-parentheses', 'title': '괄호의 짝',
        'signature': 'def balanced_parentheses(text: str) -> bool:',
        'description': '여는 괄호와 닫는 괄호로만 구성된 문자열 text의 괄호가 올바르게 짝지어져 있으면 True, 아니면 False를 반환하세요. 모든 닫는 괄호는 앞선 여는 괄호와 짝을 이루어야 하며, 남는 괄호가 없어야 합니다. 빈 문자열은 올바릅니다.',
        'inputs': ['text: (와 )로만 구성된 문자열'], 'outputs': ['모든 괄호가 순서에 맞게 짝지어졌는지 여부'],
        'examples': ['balanced_parentheses("(())()") → True', 'balanced_parentheses(")(") → False'],
        'constraints': ['0 ≤ len(text) ≤ 10000', 'text에는 (와 )만 포함됨'],
        'solution': '    balance = 0\n    for char in text:\n        balance += 1 if char == "(" else -1\n        if balance < 0:\n            return False\n    return balance == 0\n',
        'cases': [(('(())()',), True), ((')(',), False), (('',), True), (('(()',), False),
                  (('())(',), False), (('()',), True), (('(' * 5000 + ')' * 5000,), True)],
    },
    {
        'key': 'merge-sorted', 'title': '정렬된 두 목록 합치기',
        'signature': 'def merge_sorted_values(left: list[int], right: list[int]) -> list[int]:',
        'description': '각각 오름차순으로 정렬된 정수 목록 left와 right의 모든 원소를 담은 오름차순 목록을 반환하세요. 중복된 값은 등장한 횟수만큼 유지합니다. 둘 다 비어 있으면 빈 목록을 반환합니다.',
        'inputs': ['left, right: 각각 오름차순(같은 값 허용)으로 정렬된 정수 목록'],
        'outputs': ['두 목록의 모든 원소를 오름차순으로 담은 목록'],
        'examples': ['merge_sorted_values([1, 4], [2, 4]) → [1, 2, 4, 4]', 'merge_sorted_values([], [-2, 0]) → [-2, 0]'],
        'constraints': ['0 ≤ len(left), len(right) ≤ 1000', '각 원소는 -1000 이상 1000 이하의 정수'],
        'solution': '    return sorted(left + right)\n',
        'cases': [(([1, 4], [2, 4]), [1, 2, 4, 4]), (([], [-2, 0]), [-2, 0]), (([], []), []),
                  (([-3, 0, 5], [-2, 0, 7]), [-3, -2, 0, 0, 5, 7]), (([2, 2], []), [2, 2]),
                  (([1] * 1000, [1] * 1000), [1] * 2000)],
    },
)


def public_problem(problem):
    return {key: problem[key] for key in ('key', 'title', 'signature', 'description', 'inputs', 'outputs', 'examples', 'constraints')}


def source_record(problem, source_id):
    statement = public_problem(problem)
    prose = '\n'.join([statement['description'], *statement['inputs'], *statement['outputs'],
                       *statement['examples'], *statement['constraints']])
    prompt = statement['signature'] + '\n    """' + prose.replace('\n', '\n    ') + '\n    """\n'
    test = 'def check(candidate):\n' + ''.join(
        f'    assert candidate(*{args!r}) == {expected!r}\n' for args, expected in problem['cases'])
    return {'task_id': source_id, 'prompt': prompt, 'canonical_solution': problem['solution'],
            'entry_point': statement['signature'].split('def ', 1)[1].split('(', 1)[0], 'test': test}


def choose_problem(key, previous_key=None):
    choices = [problem for problem in PROBLEMS if problem['key'] != previous_key]
    return choices[int(digest(key)[:8], 16) % len(choices)]
