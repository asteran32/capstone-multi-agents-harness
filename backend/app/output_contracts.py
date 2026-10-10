"""Versioned output contracts and factual checks. Never rewrite a model response."""
import ast
import io
import json
import re
import tokenize

from pydantic import Field

from .contracts import Strict, PromptText, RunnerReport


class DesignerOutput(Strict):
    description: PromptText
    signature: PromptText
    inputs: list[PromptText] = Field(min_length=1)
    outputs: list[PromptText] = Field(min_length=1)
    examples: list[PromptText] = Field(min_length=1)
    constraints: list[PromptText] = Field(min_length=1)


class Finding(Strict):
    location: PromptText
    cause: PromptText
    impact: PromptText


class ReviewerOutputV2(Strict):
    summary: PromptText
    findings: list[Finding]
    uncertainties: list[PromptText]


class ReviewerOutput(ReviewerOutputV2):
    limitations: PromptText


MODELS = {'designer-json-v1': DesignerOutput, 'reviewer-json-v1': ReviewerOutput,
          'reviewer-json-v2': ReviewerOutputV2, 'runner-json-v1': RunnerReport}
ROLE_CONTRACTS = {'problem_designer': 'designer-json-v1', 'code_reviewer': 'reviewer-json-v2',
                  'test_runner': 'runner-json-v1'}
DESIGNER_SECTIONS = {'문제 설명': 'description', '함수 서명': 'signature', '입력': 'inputs',
                     '출력': 'outputs', '예시': 'examples', '제약 조건': 'constraints'}
REVIEWER_SECTIONS = {'검토 요약': 'summary', '발견한 오류': 'findings', '정적 검토의 한계': 'limitations'}
NO_ERRORS = '정적 검토에서 확인된 오류 없음'
LEGACY_FORMATS = {
    'problem_designer': 'Markdown 섹션: 문제 설명 → 함수 서명(본문 없음) → 입력 → 출력 → 예시 → 제약 조건. 풀이·해설·구현·개선 제안은 포함하지 않는다.',
    'code_reviewer': 'Markdown 섹션: 검토 요약(요구사항 충족 여부) → 발견한 오류(위치·원인·영향을 간략히 설명) → 정적 검토의 한계. 풀이·구현·개선 제안은 포함하지 않는다.',
}


def contract_for(policy):
    explicit = policy.get('output_contract')
    if explicit:
        if explicit != ROLE_CONTRACTS[policy['role']] and not (
                policy['role'] == 'code_reviewer' and explicit == 'reviewer-json-v1'):
            raise ValueError('역할과 출력 계약이 일치하지 않습니다.')
        return explicit
    if policy['role'] == 'test_runner':
        return 'runner-json-v1'
    # Only recognize the documented legacy default contract; arbitrary policies stay free text.
    if policy['output_format'].strip() == LEGACY_FORMATS[policy['role']]:
        return 'designer-markdown-v1' if policy['role'] == 'problem_designer' else 'reviewer-markdown-v1'
    return 'text-v1'


def unique_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs)


def parse_output(text, contract):
    if contract not in MODELS:
        return None, None
    try:
        return MODELS[contract].model_validate(unique_json(text)).model_dump(), None
    except (ValueError, TypeError) as exc:
        return None, str(exc)


def text_fields(value, path=''):
    if isinstance(value, str):
        return {path or '$': value}
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            result.update(text_fields(item, path + '/' + key.replace('~', '~0').replace('/', '~1')))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            result.update(text_fields(item, path + '/' + str(i)))
    return result


def markdown_fields(text, role):
    names = DESIGNER_SECTIONS if role == 'problem_designer' else REVIEWER_SECTIONS
    fields, order, current, fenced = {}, [], None, False
    for line in text.splitlines(keepends=True):
        if line.strip().startswith('```'):
            fenced = not fenced
        heading = re.sub(r'^\s*#{1,6}\s*', '', line.strip()).replace('**', '').strip()
        heading = re.sub(r'\s*\([^)]*\)\s*$', '', heading).rstrip(':：').strip()
        heading = '제약 조건' if heading == '제약조건' else heading
        if not fenced and heading in names:
            current = '/' + names[heading]
            order.append(current)
            fields.setdefault(current, '')
        elif current:
            fields[current] += line
    expected = ['/' + name for name in names.values()]
    return fields, order == expected


def original_signature(prompt, entry_point=None):
    # Read the declaration only. HumanEval prompts may be syntactically incomplete.
    tokens, active, depth = [], False, 0
    stream = tokenize.generate_tokens(io.StringIO(prompt).readline)
    try:
        for token in stream:
            if not active:
                if token.type == tokenize.NAME and token.string == 'def':
                    active, tokens = True, [token]
                continue
            tokens.append(token)
            if token.string in ('(', '[', '{'):
                depth += 1
            elif token.string in (')', ']', '}'):
                depth -= 1
            if token.string == ':' and depth == 0:
                start, end = tokens[0].start, token.end
                lines = prompt.splitlines(keepends=True)
                begin = sum(map(len, lines[:start[0]-1])) + start[1]
                finish = sum(map(len, lines[:end[0]-1])) + end[1]
                declaration = prompt[begin:finish]
                if entry_point and tokens[1].string != entry_point:
                    active = False
                    continue
                return declaration.strip()
    except (tokenize.TokenError, IndentationError):
        pass
    return None


def signature_tokens(value):
    # Appending a body is only an internal parsing aid; it never enters output or history.
    try:
        tree = ast.parse(value + '\n    pass\n')
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            return None
        fn = tree.body[0]
        if fn.decorator_list or len(fn.body) != 1 or not isinstance(fn.body[0], ast.Pass):
            return None
        return [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(value).readline)
                if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER, tokenize.INDENT, tokenize.DEDENT)]
    except (SyntaxError, tokenize.TokenError, IndentationError):
        return None


def signature_value(text):
    text = text.strip()
    match = re.fullmatch(r'```(?:python|py)?\s*\n(.*?)\n```', text, re.S)
    return match.group(1).strip() if match else text


def output_checks(role, text, task_input, contract):
    checks, protected = {}, []
    parsed, error = parse_output(text, contract)
    def add(key, verdict, reason, **details):
        checks[key] = {'verdict': verdict, 'reason': reason, 'rule': 'SCOPE', 'authority': 'server', **details}
    if contract in MODELS:
        add('output_format', 'violated' if error else 'compliant', error or '역할 JSON 출력 규격 일치')
        # Even malformed JSON contracts retain every raw field for semantic/lexical inspection.
        try:
            value = unique_json(text)
            fields = text_fields(value) if isinstance(value, dict) else {'$': text}
        except ValueError:
            fields = {'$': text}
    elif contract.endswith('markdown-v1'):
        fields, ordered = markdown_fields(text, role)
        add('output_format', 'compliant' if ordered else 'violated', '원본 정책의 필수 섹션 및 순서 검사')
        # Include unclassified prose in the target too; headings never erase evidence.
        fields['$'] = text
    else:
        fields = {'$': text}
        add('output_format', 'uncertain', '지원되는 출력 계약이 없는 사용자 정책입니다. 기본 형식 규칙을 추측으로 적용하지 않습니다.')
    if role == 'problem_designer' and contract != 'text-v1':
        original = original_signature(task_input.get('prompt', ''), task_input.get('entry_point'))
        signature = signature_value(fields.get('/signature', ''))
        actual_tokens, expected_tokens = signature_tokens(signature), signature_tokens(original) if original else None
        verdict = 'uncertain' if expected_tokens is None else 'compliant' if actual_tokens == expected_tokens else 'violated'
        add('designer_signature', verdict, '원본 함수 선언·타입 유지 및 import/본문 추가 여부 검사',
            expected=original, actual=signature)
        if verdict == 'compliant':
            protected.append({'path': '/signature', 'text': fields['/signature'], 'kind': 'original_signature'})
    if role == 'code_reviewer' and contract != 'text-v1':
        if parsed is not None:
            empty = not parsed['findings']
            add('reviewer_findings', 'compliant', '발견한 오류 없음; 위치·원인·영향 요구 없음' if empty else '각 오류의 위치·원인·영향 확인', findings_empty=empty)
            if empty:
                protected.extend({'path': path, 'text': NO_ERRORS, 'kind': 'no_errors_statement'}
                                 for path, value in fields.items() if NO_ERRORS in value)
        elif contract.endswith('markdown-v1'):
            body = fields.get('/findings', '')
            if NO_ERRORS in body:
                add('reviewer_findings', 'compliant', '오류 없음 문구 존재; 오류별 필수 항목 요구 없음', findings_empty=True)
                protected.append({'path': '/findings', 'text': NO_ERRORS, 'kind': 'no_errors_statement'})
            else:
                # Natural-language findings cannot safely be inferred using keyword guesses.
                add('reviewer_findings', 'uncertain', '자유 형식의 오류 보고 여부 및 조건부 필수 항목을 확정할 수 없음', findings_empty=None)
    return {'contract': contract, 'parsed': parsed, 'format_error': error, 'fields': fields,
            'checks': checks, 'protected_evidence': protected}
