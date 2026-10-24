"""Trusted standalone module; this example reads materials through host policy.

Python itself retains host privileges. Keep state in returned artifacts, not
module globals, which are not restored on task resume.
"""


def count_words(arguments, services):
    result = services['call_tool']('read_file', {'path': arguments['path'], 'max_bytes': 16000})
    if not result.ok:
        raise ValueError(result.error)
    data = result.data
    return {'path': arguments['path'], 'words': len(data['text'].split()),
            'sha256': data.get('sha256'), 'coverage': data,
            'scope': 'Only the returned host text window; inspect coverage before claiming completeness'}
