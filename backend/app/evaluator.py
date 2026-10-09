"""Dispatch by the frozen version, shared by online, offline and stored-response evaluation."""
from .evaluation import check_judge
from .evaluation_v2 import runner_checks, resolve_judgment


def is_v3(config):
    profile = config.get('evaluation_profile', {})
    if profile.get('version') == 3:
        if profile.get('logic') not in ('guardrail-3.0', 'guardrail-3.1', 'guardrail-3.2'):
            raise ValueError('지원되지 않는 평가 로직 버전입니다. 다른 버전으로 대체하지 않습니다.')
        return True
    return False


def server_checks(config, role, text, task, tool, contract=None):
    if is_v3(config):
        from .evaluation_v3 import server_checks as checks
        return checks(config, role, text, task, tool, contract)
    return runner_checks(role, text, task, tool)


def validate_judgment(config, raw, text, role, execution):
    if is_v3(config):
        from .evaluation_v3 import validate
        return validate(raw, role, execution, collect_schema_errors=(
            config['evaluation_profile'].get('validation_protocol') == 'all-schema-errors-1'))
    return check_judge(raw, text, role, bool(config.get('evaluation_profile')))


def resolve_evaluation(config, lex, judge, execution, validation):
    if is_v3(config):
        from .evaluation_v3 import resolve
        return resolve(lex, judge, execution, validation)
    return resolve_judgment(lex, judge, execution, validation['valid'])
