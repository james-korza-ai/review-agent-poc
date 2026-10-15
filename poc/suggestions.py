"""Validate bounded, exact full-line edits; never execute or apply proposed code."""
MAX_LINES = 50
MAX_BYTES = 8192


def lf_lines(text):
    """Git lines use LF only; preserve Unicode separators inside a line."""
    if not text:
        return []
    lines = text.split('\n')
    return lines[:-1] if text.endswith('\n') else lines


def validate_edit(edit, replacement, source_lines):
    """Return (copied edit, None), or (None, safe reason); never mutate inputs.

    Inclusive source lines are joined with LF without a final line terminator.
    An empty replacement is a deletion. A final LF may terminate a suggestion
    fence, so adding only that terminator does not constitute a meaningful edit.
    """
    if edit is None:
        return None, 'missing_edit'
    if not isinstance(edit, dict) or set(edit) != {'start_line', 'end_line', 'original'}:
        return None, 'invalid_edit_shape'
    if not isinstance(source_lines, (list, tuple)):
        return None, 'invalid_source'
    start, end = edit['start_line'], edit['end_line']
    if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(source_lines):
        return None, 'invalid_edit_range'
    if end - start + 1 > MAX_LINES:
        return None, 'oversized_edit'
    original = edit['original']
    if not isinstance(original, str) or not isinstance(replacement, str):
        return None, 'invalid_edit_text'
    for text in (original, replacement):
        if '```' in text or '~~~' in text or any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127 for c in text):
            return None, 'unsafe_edit_text'
        try:
            if len(text.encode('utf-8')) > MAX_BYTES:
                return None, 'oversized_edit'
        except UnicodeError:
            return None, 'invalid_edit_text'
    if len(lf_lines(replacement)) > MAX_LINES:
        return None, 'oversized_edit'
    selected = source_lines[start - 1:end]
    if any(not isinstance(line, str) or '\n' in line or '\r' in line for line in selected):
        return None, 'invalid_source'
    if original != '\n'.join(selected):
        return None, 'original_mismatch'
    if replacement == original or replacement.removesuffix('\n') == original:
        return None, 'unchanged_edit'
    return dict(edit), None
