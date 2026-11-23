"""Windows source trees can exceed MAX_PATH after a directory is uploaded."""
import os
from pathlib import Path


def fs_path(path):
    path = Path(path)
    if os.name != 'nt':
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith('\\\\?\\'):
        return Path(absolute)
    if absolute.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + absolute[2:])
    return Path('\\\\?\\' + absolute)
