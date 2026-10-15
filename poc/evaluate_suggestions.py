"""Measure proposed fixes after model calls; never execute proposed Go code."""
import argparse
import json
from pathlib import Path

from .build_request import checked_path, read_regular, write_json
from .score import CASES
from .suggestions import lf_lines, validate_edit
from .suggestion_compile import compile_suggestion


def verify(out):
    """Require six exact-source, compile-only checked edits and twelve quiet cases.

    This supplements the unchanged detection scorecard. Compilation establishes
    neither semantic correctness nor a calibrated confidence score. The model
    never receives the answer key or these post-call measurements.
    """
    out = checked_path(out)
    rows = []
    for case in CASES:
        for number in range(1, 4):
            trial = out / 'results' / case / f'run-{number}'
            row = {'case': case, 'run': f'run-{number}', 'valid': False,
                   'reason': 'missing_or_invalid_result', 'compile_only': True}
            try:
                report = json.loads(read_regular(trial, 'result.json'))
                diagnostic = json.loads(read_regular(trial, 'result.json.diagnostics.json'))
                findings = report.get('findings')
                if diagnostic.get('status') != 'completed' or not isinstance(findings, list):
                    raise ValueError('unfinished')
                if not case.startswith('bug_'):
                    row.update(valid=not findings, reason='quiet' if not findings else 'unexpected_finding')
                elif len(findings) != 1 or type(diagnostic.get('suggestions_rejected')) is not int or diagnostic['suggestions_rejected'] != 0:
                    row['reason'] = 'missing_or_rejected_edit'
                else:
                    finding = findings[0]
                    if finding['location']['path'] != f'{case}.go':
                        raise ValueError('wrong source')
                    source = read_regular(trial / 'data', f'{case}.go')
                    suggestion = finding['suggestion']
                    edit, reason = validate_edit(suggestion.get('edit'), suggestion.get('replacement'), lf_lines(source))
                    if edit is None:
                        row['reason'] = reason
                    else:
                        passed = compile_suggestion(source, edit, suggestion['replacement'])
                        row.update(valid=passed, reason='compiled_not_executed' if passed else 'compile_guard_refused',
                                   edit_start=edit['start_line'], edit_end=edit['end_line'])
            except (OSError, ValueError, TypeError, KeyError):
                pass
            rows.append(row)
    bugs = sum(row['valid'] for row in rows if row['case'].startswith('bug_'))
    clean = sum(row['valid'] for row in rows if row['case'].startswith('ok_'))
    card = {'scope': 'post-call exact-source and compile-only edit checks; not semantic proof',
            'proposed_code_executed': False, 'per_run': rows,
            'valid_bug_edits': bugs, 'clean_trials_without_findings': clean,
            'met': bugs == 6 and clean == 12}
    write_json(out / 'suggestions.json', card)
    print(json.dumps({'valid_bug_edits': bugs, 'clean_trials_without_findings': clean, 'met': card['met']}))
    return card


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    return 0 if verify(args.out)['met'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
